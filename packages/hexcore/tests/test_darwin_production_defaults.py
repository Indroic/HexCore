"""
Los defaults que servían para desarrollo y no para producción.

Dos, y los dos fallaban en silencio: la app arrancaba, la suite pasaba en verde, y el agujero
aparecía recién con más de un proceso.

1. **El almacén de claves era siempre efímero.** La tabla `darwin_jwks` existía desde el
   principio —`JwksMixin`, `JwksModel`, `DEFAULT_JWKS_TABLE`— y `jwks_document()` sabía armar
   el documento del endpoint JWKS. Lo que no existía era el almacén que leyera la tabla, así
   que el default era `StaticKeyStore` con una clave generada al arrancar: un reload
   deslogueaba a todo el mundo, y con varios workers cada uno firmaba con una clave que los
   otros no verificaban.
2. **La denylist vivía en un `MemoryCache`.** `CacheRevocationList` cae a
   `ServerConfig.cache_backend`, cuyo default es `MemoryCache()`. Un cache en memoria es por
   proceso, así que un `sign-out` cortaba la sesión en el worker que atendió el pedido y la
   dejaba viva en todos los demás.

Se arreglan resolviendo un almacén persistido por backend, y haciendo que `IdentityStep` se
niegue a arrancar con cualquiera de los dos si `debug=False`.
"""
from __future__ import annotations

import pytest

pytest.importorskip("joserfc")
pytest.importorskip("argon2")
pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

from hexcore.config import LazyConfig, ServerConfig  # noqa: E402
from hexcore.infrastructure.cache import ICache  # noqa: E402
from hexcore.darwin import IdentityConfig, StaticKeyStore  # noqa: E402
from hexcore.darwin.application.container import reset_identity  # noqa: E402
from hexcore.darwin.infrastructure.keys import generate_signing_key  # noqa: E402
from hexcore.darwin.infrastructure.lifespan import IdentityStep  # noqa: E402
from hexcore.darwin.testing import TEST_SECRET_KEY  # noqa: E402


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _limpiar():
    """
    Restaura la config del proceso. `LazyConfig._imported_config` es el mismo asidero que usa
    `test_darwin_container`: no hay setter público, y el módulo lo cachea a nivel de clase.
    """
    previo = LazyConfig._imported_config
    yield
    LazyConfig._imported_config = previo
    reset_identity()


def _config(**campos) -> None:
    LazyConfig._imported_config = ServerConfig(**campos)


def _produccion(**extra) -> None:
    """Producción, y con un cache compartido salvo que el test pida lo contrario."""
    extra.setdefault("cache_backend", _CacheCompartido())
    _config(debug=False, **extra)


class _CacheCompartido(ICache):
    """
    Un doble que **no** es `MemoryCache`, para los tests que no están probando el cache.

    Sin esto, todo test de producción chocaría primero con el chequeo del cache y nunca
    llegaría al que quiere ejercitar. Hereda `ICache` porque `ServerConfig.cache_backend`
    valida el tipo.
    """

    async def get(self, key: str):
        return None

    async def set(self, key: str, value, expire: int = 3600):
        return None

    async def delete(self, key: str):
        return None


class TestElAlmacenDeClavesEfimero:
    def test_esta_marcado_como_efimero(self) -> None:
        """
        El flag es lo que permite distinguirlo. Un `isinstance(StaticKeyStore)` sería
        incorrecto: un `StaticKeyStore` **sembrado** desde un gestor de secretos es válido en
        producción, y lo que no sirve es que la clave cambie en cada arranque.
        """
        efimero = StaticKeyStore([generate_signing_key()], ephemeral=True)
        sembrado = StaticKeyStore([generate_signing_key()])

        assert efimero.ephemeral is True
        assert sembrado.ephemeral is False, (
            "Un almacén sembrado a mano no puede quedar marcado como efímero: sería un falso "
            "positivo que impediría arrancar a quien hizo lo correcto."
        )

    def test_no_arranca_en_produccion_con_clave_efimera(self) -> None:
        _produccion()
        paso = IdentityStep(
            IdentityConfig(secret_key=TEST_SECRET_KEY), verify_schema=False
        )

        import asyncio

        with pytest.raises(ValueError, match="generada en cada arranque"):
            asyncio.run(paso.start())

    def test_en_debug_la_clave_efimera_esta_permitida(self) -> None:
        """Si no, no se podría levantar la app local ni correr un test sin sembrar claves."""
        _config(debug=True)
        paso = IdentityStep(
            IdentityConfig(secret_key=TEST_SECRET_KEY), verify_schema=False
        )

        import asyncio

        asyncio.run(paso.start())  # no levanta

    def test_un_almacen_inyectado_deja_arrancar(self) -> None:
        """El camino correcto: la clave viene del gestor de secretos del despliegue."""
        _produccion()
        paso = IdentityStep(
            IdentityConfig(secret_key=TEST_SECRET_KEY),
            components={"key_store": StaticKeyStore([generate_signing_key()])},
            verify_schema=False,
        )

        import asyncio

        asyncio.run(paso.start())  # no levanta


class TestElCacheDeProceso:
    def test_no_arranca_en_produccion_con_memorycache(self) -> None:
        """
        La denylist en memoria es la falla más traicionera de las dos: con un solo proceso
        —desarrollo, CI— funciona perfecto, y en producción el sign-out corta la sesión en uno
        de N workers.
        """
        _config(debug=False)  # con el MemoryCache por defecto
        paso = IdentityStep(
            IdentityConfig(secret_key=TEST_SECRET_KEY),
            components={"key_store": StaticKeyStore([generate_signing_key()])},
            verify_schema=False,
        )

        import asyncio

        with pytest.raises(ValueError, match="MemoryCache"):
            asyncio.run(paso.start())


class TestElAlmacenPersistido:
    def test_los_dos_backends_exponen_KeyStore(self) -> None:
        """
        El contrato de nombre neutro, igual que los cinco repositorios: el contenedor resuelve
        por nombre y no por backend, así que los dos tienen que exponerlo o el que falte se
        rompe recién en runtime.
        """
        from hexcore.darwin.infrastructure.orms.sqlalchemy import (
            repositories as sql_repos,
        )

        assert hasattr(sql_repos, "KeyStore")
        assert "KeyStore" in sql_repos.__all__

        beanie_repos = pytest.importorskip(
            "hexcore.darwin.infrastructure.orms.beanie.repositories"
        )
        assert hasattr(beanie_repos, "KeyStore")
        assert "KeyStore" in beanie_repos.__all__

    @pytest.mark.anyio
    async def test_guarda_y_devuelve_la_clave_activa(self) -> None:
        """Ida y vuelta contra SQLite real: sin esto el almacén no prueba nada."""
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from sqlalchemy.pool import StaticPool

        from hexcore.darwin.infrastructure.orms.sqlalchemy.repositories import (
            SqlAlchemyKeyStore,
        )
        from hexcore.darwin.infrastructure.orms.sqlalchemy.schema import (
            create_identity_tables,
        )

        motor = create_async_engine(
            "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        await create_identity_tables(motor)
        fabrica = async_sessionmaker(motor, expire_on_commit=False)

        import contextlib

        @contextlib.asynccontextmanager
        async def scope():
            async with fabrica() as s:
                yield s

        almacen = SqlAlchemyKeyStore(session_scope=scope)
        clave = generate_signing_key()
        await almacen.add(clave)

        activa = await almacen.get_active()
        assert activa.kid == clave.kid
        assert activa.private_key == clave.private_key, (
            "La privada tiene que volver íntegra o los tokens firmados antes del reinicio no "
            "verifican — que es exactamente el bug que este almacén cierra."
        )

        assert (await almacen.get(clave.kid)) is not None
        assert (await almacen.get("kid-que-no-existe")) is None, (
            "Un kid desconocido devuelve None, no lanza: viene del token, o sea de quien lo "
            "presenta."
        )
        assert [c.kid for c in await almacen.list_verifiable()] == [clave.kid]

        await motor.dispose()
