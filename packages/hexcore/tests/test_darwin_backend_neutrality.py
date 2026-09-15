"""
Que ningún plugin fuerce un backend de almacenamiento.

Es la otra mitad del desacoplamiento. `test_darwin_plugin_decoupling.py` fija que los plugins no
se conozcan entre sí ni el núcleo a ellos; acá se fija que **usar un plugin no obligue a instalar
SQLAlchemy**, que era lo que pasaba y por dos causas distintas:

1. `PluginRegistry.validate()` llamaba a `plugin.tables()` para detectar dos plugins con un mixin
   homónimo. `tables()` devuelve mixins de SQLAlchemy, así que registrar `two_factor` en un
   despliegue de Mongo explotaba con un `ImportError` sobre un paquete que ese despliegue eligió
   no instalar. Y `validate()` corre en todo `configure_identity`, así que no había forma de
   esquivarlo. Se arregla con `contributed_tables`: los mismos nombres, declarados sin importar.
2. `hexcore/infrastructure/api/__init__.py` importaba `.utils` eagerly, y `utils` importa
   `sqlalchemy.ext.asyncio` con razón —construye endpoints de query sobre SQL—. El efecto es que
   importar **cualquier** submódulo del paquete arrastraba sqlalchemy, porque importar
   `api.rate_limit` ejecuta el `__init__` del paquete. Eso alcanzaba a todo el borde HTTP de
   Darwin: los routers de los plugins usan `rate_limit`, así que un despliegue en Mongo no podía
   montar un magic link. Se arregla con `__getattr__` de módulo (PEP 562).

La simetría también se prueba: con `beanie` bloqueado tiene que funcionar todo sobre SQLAlchemy.
Si sólo se probara una dirección, "desacoplado" podría significar "acoplado al otro".
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytest.importorskip("joserfc")
pytest.importorskip("argon2")

RAIZ = Path(__file__).resolve().parent.parent

#: Cómo se instancia cada plugin. `passkey` exige `rp_id` porque un WebAuthn sin Relying Party
#: no se puede verificar, y el plugin prefiere fallar al cablear.
SPECS: tuple[tuple[str, str, str], ...] = (
    ("magic_link", "MagicLinkPlugin", ""),
    ("two_factor", "TwoFactorPlugin", ""),
    ("oauth", "OAuthPlugin", ""),
    ("impersonate", "ImpersonatePlugin", ""),
    ("passkey", "PasskeyPlugin", 'rp_id="mi-app.com", origins=["https://mi-app.com"]'),
    ("organization", "OrganizationPlugin", ""),
)

#: Los puntos de extensión que **no** pueden depender de un backend.
#:
#: `tables()` queda deliberadamente afuera: *es* el punto de extensión de SQL, y llamarlo importa
#: sqlalchemy a propósito. Lo llama el consumidor que está declarando sus modelos concretos, que
#: por definición tiene el extra. Lo que no puede pasar es que lo llame el framework.
PUNTOS_NEUTROS: tuple[str, ...] = (
    "hooks",
    "middlewares",
    "http_middlewares",
    "routers",
    "startup_steps",
    "exception_status_map",
)

#: Los siete mixins que aportan los cuatro plugins con tabla, en orden de plugin.
MIXINS_ESPERADOS: tuple[str, ...] = (
    "TwoFactorMixin",
    "OAuthStateMixin",
    "PasskeyMixin",
    "PasskeyChallengeMixin",
    "OrganizationMixin",
    "MemberMixin",
    "InvitationMixin",
)

_FINDER = '''
import sys
BLOQUEADOS = {bloqueados!r}
class MockFinder:
    @classmethod
    def find_spec(cls, fullname, path, target=None):
        if fullname.split(".")[0] in BLOQUEADOS:
            raise ImportError("bloqueado: " + fullname)
        return None
sys.meta_path.insert(0, MockFinder)
'''


def _sin(
    bloqueados: tuple[str, ...], *bloques: str
) -> subprocess.CompletedProcess[str]:
    """
    Corre esos bloques en un subproceso donde esos paquetes no se pueden importar.

    Es la técnica de `test_optional_dependencies.py`: un `MockFinder` en `sys.meta_path`. Se
    bloquea en vez de mirar `sys.modules` porque `import hexcore` arrastra sqlalchemy por su
    cuenta —`hexcore/__init__.py` importa `BaseSQLAlchemyRepository` eager— así que preguntar
    quién está cargado no distingue "esto lo importó" de "ya estaba".

    Varios bloques y no uno, porque cada uno se **dedenta por separado**: un literal indentado
    dentro de un método y un fragmento generado a columna cero no comparten prefijo, así que un
    solo `dedent` sobre la concatenación no saca nada y el subproceso muere con
    `IndentationError`.
    """
    codigo = _FINDER.format(bloqueados=set(bloqueados))
    codigo += "".join(textwrap.dedent(bloque).strip("\n") + "\n" for bloque in bloques)
    return subprocess.run(
        [sys.executable, "-c", codigo], capture_output=True, text=True, cwd=RAIZ
    )


SQL = ("sqlalchemy", "alembic", "asyncpg", "aiosqlite")
MONGO = ("beanie", "pymongo", "motor")


def _imports_de_plugins() -> str:
    lineas = [
        f"from hexcore.darwin.plugins.{paquete} import {clase}"
        for paquete, clase, _ in SPECS
    ]
    return "\n".join(lineas)


def _construcciones() -> str:
    partes = [f"{clase}({kwargs})" for _, clase, kwargs in SPECS]
    return "[\n    " + ",\n    ".join(partes) + ",\n]"


# ── La declaración no puede desincronizarse ───────────────────────────────────
@pytest.mark.parametrize(("paquete", "clase", "kwargs"), SPECS)
def test_contributed_tables_coincide_con_tables(
    paquete: str, clase: str, kwargs: str
) -> None:
    """
    `contributed_tables` duplica las claves de `tables()`, así que puede desincronizarse.

    Sin este test la duplicación es una bomba de tiempo silenciosa: un plugin que agrega un mixin
    a `tables()` y se olvida de declararlo deja de participar del chequeo de homónimos, y el
    conflicto que ese chequeo existe para encontrar vuelve a ser un error en runtime dentro del
    framework.
    """
    resultado = _sin(
        (),
        f"""
        from hexcore.darwin.plugins.{paquete} import {clase}
        plugin = {clase}({kwargs})
        declarados = tuple({clase}.contributed_tables)
        reales = tuple(plugin.tables())
        assert declarados == reales, f"{{declarados}} != {{reales}}"
        print("ok")
        """,
    )
    assert resultado.returncode == 0, resultado.stderr
    assert "ok" in resultado.stdout


# ── Sin SQLAlchemy ────────────────────────────────────────────────────────────
class TestSinSqlAlchemy:
    def test_los_seis_plugins_se_instancian_y_aportan(self) -> None:
        """Todos los puntos de extensión menos `tables()`, que es el de SQL."""
        resultado = _sin(
            SQL,
            _imports_de_plugins(),
            f"plugins = {_construcciones()}",
            f"""
            for p in plugins:
                for punto in {PUNTOS_NEUTROS!r}:
                    getattr(p, punto)()
            print("ok", len(plugins))
            """,
        )
        assert resultado.returncode == 0, resultado.stderr
        assert "ok 6" in resultado.stdout

    def test_el_registro_valida(self) -> None:
        """
        Es el que rompía: `validate()` corre en todo `configure_identity`.

        Un despliegue en Mongo que registraba `two_factor` no llegaba a arrancar.
        """
        resultado = _sin(
            SQL,
            "from hexcore.darwin.application.plugins import PluginRegistry",
            _imports_de_plugins(),
            f"registro = PluginRegistry({_construcciones()})",
            """
            registro.validate()
            print("names:", registro.names)
            print("tablas:", " ".join(registro.table_names()))
            """,
        )
        assert resultado.returncode == 0, resultado.stderr
        linea = next(
            x for x in resultado.stdout.splitlines() if x.startswith("tablas: ")
        )
        assert set(linea.removeprefix("tablas: ").split()) == set(MIXINS_ESPERADOS)

    def test_el_registro_agrega_hooks_routers_y_errores(self) -> None:
        resultado = _sin(
            SQL,
            "from hexcore.darwin.application.plugins import PluginRegistry",
            _imports_de_plugins(),
            f"registro = PluginRegistry({_construcciones()})",
            """
            assert len(registro.routers()) == 6, registro.routers()
            assert len(registro.exception_status_map()) > 20
            registro.hooks()
            print("ok")
            """,
        )
        assert resultado.returncode == 0, resultado.stderr
        assert "ok" in resultado.stdout

    def test_el_cableado_completo_resuelve_en_mongo(self) -> None:
        """
        La prueba de verdad: `configure_identity` con cuatro plugins y todos los repositorios.

        Se deja que el backend se **detecte** en vez de declararlo, porque con sqlalchemy
        bloqueado la detección tiene que dar `beanie` sola. Es lo que le pasa a un despliegue que
        instaló `[darwin-beanie]` y nada más.
        """
        resultado = _sin(
            SQL,
            """
            from hexcore.darwin.application.config import IdentityConfig
            from hexcore.darwin.application.container import (
                configure_identity,
                get_identity_container,
            )
            from hexcore.darwin.application.plugins import PluginRegistry
            from hexcore.darwin.plugins.magic_link import MagicLinkPlugin
            from hexcore.darwin.plugins.organization import OrganizationPlugin
            from hexcore.darwin.plugins.passkey import PasskeyPlugin
            from hexcore.darwin.plugins.two_factor import TwoFactorPlugin
            from hexcore.darwin.plugins.storage import plugin_repositories

            registro = PluginRegistry([
                TwoFactorPlugin(),
                OrganizationPlugin(),
                MagicLinkPlugin(),
                PasskeyPlugin(rp_id="mi-app.com", origins=["https://mi-app.com"]),
            ])
            configure_identity(IdentityConfig(secret_key="x" * 48), plugins=registro)
            c = get_identity_container()
            assert c.storage_backend == "beanie", c.storage_backend
            for nombre in ("users", "sessions_repository", "accounts", "verifications"):
                assert type(getattr(c, nombre)()).__name__.startswith("Beanie")
            c.session_service()
            c.identity_service()
            assert type(plugin_repositories("two_factor").TwoFactorRepository()).__name__ == (
                "BeanieTwoFactorRepository"
            )
            print("ok")
            """,
        )
        assert resultado.returncode == 0, resultado.stderr
        assert "ok" in resultado.stdout

    def test_el_borde_http_de_darwin_se_importa(self) -> None:
        """
        La segunda causa: `api/__init__` arrastraba sqlalchemy a todo el borde HTTP.

        Los routers de los plugins usan `rate_limit`, que vive en `hexcore.infrastructure.api`, y
        importar un submódulo ejecuta el `__init__` del paquete.
        """
        resultado = _sin(
            SQL,
            """
            from hexcore.infrastructure.api.rate_limit import client_ip_key, rate_limit
            from hexcore.darwin.infrastructure.api.routers import build_identity_router
            from hexcore.darwin.plugins.magic_link.router import build_magic_link_router

            build_magic_link_router()
            print("ok")
            """,
        )
        assert resultado.returncode == 0, resultado.stderr
        assert "ok" in resultado.stdout

    def test_detectar_backends_no_lanza(self) -> None:
        """
        `installed_backends()` tiene que responder "no está", no propagar.

        `find_spec` puede lanzar en vez de devolver `None` —con un finder que levanta
        `ImportError`, que es como se simula un paquete ausente— y una función cuya única pregunta
        es "¿está?" no puede tener una tercera respuesta.
        """
        resultado = _sin(
            SQL,
            """
            from hexcore.darwin.infrastructure.orms.selection import (
                installed_backends,
                resolve_storage_backend,
            )

            assert installed_backends() == ("beanie",), installed_backends()
            assert resolve_storage_backend(None) == "beanie"
            print("ok")
            """,
        )
        assert resultado.returncode == 0, resultado.stderr
        assert "ok" in resultado.stdout


# ── Sin Beanie: la simetría ───────────────────────────────────────────────────
class TestSinBeanie:
    """
    La otra dirección, para que "desacoplado" no signifique "acoplado al otro".

    Un test que sólo bloqueara sqlalchemy pasaría igual si todo el módulo hubiera pasado a
    depender de Mongo.
    """

    def test_el_registro_valida(self) -> None:
        resultado = _sin(
            MONGO,
            "from hexcore.darwin.application.plugins import PluginRegistry",
            _imports_de_plugins(),
            f"registro = PluginRegistry({_construcciones()})",
            """
            registro.validate()
            assert len(registro.table_names()) == 7
            print("ok")
            """,
        )
        assert resultado.returncode == 0, resultado.stderr
        assert "ok" in resultado.stdout

    def test_el_cableado_completo_resuelve_en_sql(self) -> None:
        resultado = _sin(
            MONGO,
            """
            from hexcore.darwin.application.config import IdentityConfig
            from hexcore.darwin.application.container import (
                configure_identity,
                get_identity_container,
            )
            from hexcore.darwin.application.plugins import PluginRegistry
            from hexcore.darwin.plugins.storage import plugin_repositories
            from hexcore.darwin.plugins.two_factor import TwoFactorPlugin

            registro = PluginRegistry([TwoFactorPlugin()])
            configure_identity(IdentityConfig(secret_key="x" * 48), plugins=registro)
            c = get_identity_container()
            assert c.storage_backend == "sqlalchemy", c.storage_backend
            # Y acá `tables()` sí funciona: es el backend que lo soporta.
            assert set(registro.tables()) == {"TwoFactorMixin"}
            assert type(plugin_repositories("two_factor").TwoFactorRepository()).__name__ == (
                "SqlAlchemyTwoFactorRepository"
            )
            print("ok")
            """,
        )
        assert resultado.returncode == 0, resultado.stderr
        assert "ok" in resultado.stdout


# ── La fachada de `api` sigue siendo la misma ─────────────────────────────────
class TestLaFachadaDeApi:
    def test_los_nombres_diferidos_resuelven(self) -> None:
        import hexcore.infrastructure.api as api

        assert callable(api.build_query_endpoint)
        assert callable(api.register_query_endpoint)

    def test_se_memoiza(self) -> None:
        """El segundo acceso no vuelve a importar: queda en `globals()`."""
        import hexcore.infrastructure.api as api

        primero = api.build_query_endpoint
        assert api.build_query_endpoint is primero
        assert "build_query_endpoint" in vars(api)

    def test_un_typo_sigue_dando_attribute_error(self) -> None:
        """Y no `ImportError`, que sería un cambio de contrato para quien usa `getattr`."""
        import hexcore.infrastructure.api as api

        with pytest.raises(AttributeError):
            api.build_query_endpint  # type: ignore[attr-defined]  # noqa: B018

    def test_all_no_cambio(self) -> None:
        import hexcore.infrastructure.api as api

        assert "build_query_endpoint" in api.__all__
        assert "register_query_endpoint" in api.__all__
        assert set(api.__all__) <= set(dir(api))


# ── El fallback a `tables()` ──────────────────────────────────────────────────
class TestElFallbackParaPluginsQueNoDeclaran:
    """
    Un plugin que sólo implementa `tables()` tiene que seguir validado.

    Es el costo real del diseño, y lo destapó un test preexistente:
    `contributed_tables` es opcional, así que saltear a los que no declaran los dejaba fuera del
    chequeo de homónimos **en silencio** — peor que el import, porque el conflicto que el chequeo
    existe para encontrar volvía a aparecer como un error dentro del framework.
    """

    def _plugin(self, nombre: str, tablas: dict[str, type], declarar: bool):
        from hexcore.darwin.domain.plugins import DarwinPlugin

        cuerpo: dict[str, object] = {
            "name": nombre,
            "tables": lambda self, _t=tablas: _t,
        }
        if declarar:
            cuerpo["contributed_tables"] = tuple(tablas)
        return type(f"Plugin_{nombre}", (DarwinPlugin,), cuerpo)()

    def test_sin_declarar_el_conflicto_se_sigue_detectando(self) -> None:
        from hexcore.darwin.application.plugins import PluginError, PluginRegistry

        class MiMixin:
            pass

        registro = PluginRegistry(
            [
                self._plugin("uno", {"XMixin": MiMixin}, declarar=False),
                self._plugin("otro", {"XMixin": MiMixin}, declarar=False),
            ]
        )
        with pytest.raises(PluginError):
            registro.validate()

    def test_declarado_y_sin_declarar_se_comparan_entre_si(self) -> None:
        """El conflicto cruzado también, que es el caso mixto de un despliegue real."""
        from hexcore.darwin.application.plugins import PluginError, PluginRegistry

        class MiMixin:
            pass

        registro = PluginRegistry(
            [
                self._plugin("uno", {"XMixin": MiMixin}, declarar=True),
                self._plugin("otro", {"XMixin": MiMixin}, declarar=False),
            ]
        )
        with pytest.raises(PluginError):
            registro.validate()

    def test_un_tables_que_no_se_puede_importar_se_saltea(self) -> None:
        """
        No se puede leer los nombres de un módulo que no está.

        Hacer fallar el arranque por no poder correr una validación es peor que no correrla — y es
        exactamente el modo de falla que todo esto vino a arreglar.
        """
        from hexcore.darwin.application.plugins import PluginRegistry
        from hexcore.darwin.domain.plugins import DarwinPlugin

        def explota(self):
            raise ImportError("No module named 'sqlalchemy'")

        Roto = type("PluginRoto", (DarwinPlugin,), {"name": "roto", "tables": explota})

        registro = PluginRegistry([Roto()])
        registro.validate()
        assert registro.table_names() == ()
