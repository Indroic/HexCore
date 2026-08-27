"""
Que el esquema de Darwin llegue a Alembic y a `init_beanie`, plugins incluidos.

El modo de falla que este archivo cierra es el peor de todos los del módulo, porque no da un
error: una tabla que existe en la base y está ausente de `Base.metadata` hace que el próximo
`alembic revision --autogenerate` le emita ``op.drop_table``. Con datos adentro.

Eran cinco agujeros distintos, y conviene tenerlos separados porque cada uno se arregla en otro
lado:

1. `ensure_framework_models_loaded()` importa **sólo** `cron_sql`. El comentario del `env.py`
   generado decía "y las tablas de identidad cuando uses el modulo", que era falso.
2. El `env.py` generado no llamaba a `ensure_identity_schema_loaded()`, así que las seis tablas
   del núcleo quedaban afuera.
3. `ensure_identity_schema_loaded()` importaba sólo los modelos del núcleo: las siete tablas de
   los cuatro plugins con esquema propio quedaban afuera igual.
4. En Mongo el equivalente no es una migración perdida sino una app que no arranca —un `Document`
   que `init_beanie` no vio falla con `CollectionWasNotInitialized`— y `init_identity_documents`
   tampoco cubría los plugins.
5. `IdentityStep`, que es la red de contención, verificaba sólo el núcleo.
"""
from __future__ import annotations

import ast
import inspect
import typing as t

import pytest

pytest.importorskip("joserfc")
pytest.importorskip("argon2")
pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

from hexcore.darwin.infrastructure.orms.sqlalchemy.schema import (  # noqa: E402
    ensure_identity_schema_loaded,
    identity_tables,
    plugin_models,
)
from hexcore.darwin.plugins.storage import (  # noqa: E402
    installed_plugins,
    plugin_schema_module,
)

#: Los plugins con esquema propio en SQL, y cuántas tablas aporta cada uno.
CON_TABLA: dict[str, int] = {
    "two_factor": 1,
    "oauth": 1,
    "passkey": 2,
    "organization": 3,
}

#: Los que no aportan tabla, y por qué. `magic_link` reusa `verification`; `impersonate` no
#: guarda nada aparte —la impersonación es una sesión con dos principales, y esas columnas son
#: del núcleo desde la Fase 3.
SIN_TABLA: tuple[str, ...] = ("magic_link", "impersonate")

#: Los plugins con documentos propios en Mongo, y cuántos. `organization` aporta dos y no tres
#: porque los miembros van **embebidos** en la organización: es lo que hace que el invariante del
#: último owner sea de un solo documento.
CON_DOCUMENTOS: dict[str, int] = {
    "two_factor": 1,
    "oauth": 1,
    "passkey": 2,
    "organization": 2,
}

TODOS = tuple(installed_plugins())



@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def motor() -> t.Iterator[t.Any]:
    """
    Un engine SQLite en memoria con `StaticPool`, el patrón de `test_darwin_repositories.py`.

    `StaticPool` es obligatorio con `:memory:`: sin él cada conexión estrena una base vacía y
    un `create_all` seguido de un `inspect` no ve nada.
    """
    import asyncio

    from sqlalchemy.pool import StaticPool

    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
        dispose_engine,
        init_engine,
    )

    asyncio.run(dispose_engine())
    engine = init_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    yield engine
    asyncio.run(dispose_engine())

# ── El contrato de nombre neutro ──────────────────────────────────────────────
class TestElContratoDeNombreNeutro:
    """
    `PLUGIN_MODELS` / `PLUGIN_DOCUMENTS`, con el mismo criterio que `UserRepository`.

    Sin un nombre igual en todos los plugins, juntar los esquemas obligaba al núcleo a conocerlos
    por nombre — que es el acoplamiento que la separación en extras sacó. Cada plugin ya tiene su
    constante propia (`TWO_FACTOR_MODELS`, `OAUTH_MODELS`, …); el alias neutro es lo que el núcleo
    busca.
    """

    @pytest.mark.parametrize("plugin", sorted(CON_TABLA))
    def test_el_modulo_sql_expone_plugin_models(self, plugin: str) -> None:
        modulo = plugin_schema_module(plugin, backend="sqlalchemy", module="models")
        assert modulo is not None
        assert hasattr(modulo, "PLUGIN_MODELS"), (
            f"`{plugin}` no expone PLUGIN_MODELS, así que `ensure_identity_schema_loaded` no "
            f"puede juntar su tabla sin conocerlo por nombre."
        )
        assert "PLUGIN_MODELS" in modulo.__all__
        assert len(modulo.PLUGIN_MODELS) == CON_TABLA[plugin]

    @pytest.mark.parametrize("plugin", sorted(CON_DOCUMENTOS))
    def test_el_modulo_beanie_expone_plugin_documents(self, plugin: str) -> None:
        pytest.importorskip("beanie")
        modulo = plugin_schema_module(plugin, backend="beanie", module="repository")
        assert modulo is not None
        assert hasattr(modulo, "PLUGIN_DOCUMENTS"), (
            f"`{plugin}` no expone PLUGIN_DOCUMENTS. En Mongo eso no es una migración perdida: "
            f"un Document que init_beanie no ve falla con CollectionWasNotInitialized."
        )
        assert "PLUGIN_DOCUMENTS" in modulo.__all__
        assert len(modulo.PLUGIN_DOCUMENTS) == CON_DOCUMENTOS[plugin]

    @pytest.mark.parametrize("plugin", SIN_TABLA)
    def test_un_plugin_sin_esquema_no_es_un_error(self, plugin: str) -> None:
        """Devuelve `None`, no lanza: es el caso normal, no un plugin incompleto."""
        assert plugin_schema_module(plugin, backend="sqlalchemy", module="models") is None
        assert plugin_models([plugin]) == []

    def test_un_plugin_que_no_existe_tampoco_lanza(self) -> None:
        """
        Un plugin de terceros puede llamarse cualquier cosa y no tener paquete acá.

        Que la resolución del esquema explote sería peor que que no encuentre nada: impediría
        arrancar por un chequeo que existe para avisar.
        """
        assert plugin_models(["no-existe-este-plugin"]) == []


# ── SQL: los modelos llegan a Base.metadata ───────────────────────────────────
class TestElEsquemaSqlLlegaAlMetadata:
    def test_sin_plugins_carga_solo_el_nucleo(self) -> None:
        modulos = ensure_identity_schema_loaded()
        assert modulos == ["hexcore.darwin.infrastructure.orms.sqlalchemy.models"]

    def test_con_plugins_carga_los_modulos_de_cada_uno(self) -> None:
        modulos = ensure_identity_schema_loaded(plugins=TODOS)
        # El núcleo más los cuatro con tabla. Los dos sin tabla no suman módulo.
        assert len(modulos) == 1 + len(CON_TABLA)
        for plugin in CON_TABLA:
            esperado = f"hexcore.darwin.plugins.{plugin}.orms.sqlalchemy.models"
            assert esperado in modulos

    def test_toda_tabla_de_plugin_queda_registrada(self) -> None:
        """
        La regresión de verdad: cada tabla de plugin en `Base.metadata`.

        Es lo que decide si `--autogenerate` la crea o la dropea, así que se verifica contra el
        metadata real y no contra la lista de módulos importados.
        """
        from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

        ensure_identity_schema_loaded(plugins=TODOS)

        faltan = [
            modelo.__tablename__
            for modelo in plugin_models(TODOS)
            if modelo.__tablename__ not in Base.metadata.tables
        ]
        assert faltan == [], (
            f"Estas tablas de plugin no están en Base.metadata: {faltan}. "
            f"`revision --autogenerate` les va a emitir op.drop_table."
        )

    def test_la_cuenta_de_tablas_es_la_esperada(self) -> None:
        assert len(identity_tables()) == 6
        assert len(identity_tables(plugins=TODOS)) == 6 + sum(CON_TABLA.values())

    def test_las_tablas_de_plugin_van_al_final(self) -> None:
        """
        El orden **es** la seguridad de FK.

        `drop_identity_tables` invierte la lista completa. Las tablas de los plugins referencian
        a `darwin_user`, así que tienen que borrarse primero — y eso pasa sólo si en la lista sin
        invertir van últimas.
        """
        nombres = [tabla.name for tabla in identity_tables(plugins=TODOS)]
        assert nombres[:6] == [
            "darwin_user",
            "darwin_session",
            "darwin_account",
            "darwin_verification",
            "darwin_audit_log",
            "darwin_jwks",
        ]
        assert "darwin_user" not in nombres[6:]
        assert "darwin_two_factor" in nombres[6:]

    @pytest.mark.anyio
    async def test_create_y_drop_con_plugins_funcionan(self, motor: t.Any) -> None:
        """
        Que `create` las cree y `drop` las borre, contra un motor de verdad.

        El `drop` es la mitad que importa: con el orden equivocado falla por FK, y un test que
        sólo crea no lo detecta. SQLite valida las FKs si se le pide, y el `drop_all` de
        SQLAlchemy ordena solo — pero el orden que le llega sale de acá, así que si `identity_tables`
        pusiera las tablas de plugin primero, el `reversed` las dejaría últimas.
        """
        import sqlalchemy as sa

        from hexcore.darwin.infrastructure.orms.sqlalchemy.schema import (
            create_identity_tables,
            drop_identity_tables,
        )

        async def tablas() -> set[str]:
            async with motor.connect() as conexion:
                return await conexion.run_sync(
                    lambda sync: set(sa.inspect(sync).get_table_names())
                )

        await create_identity_tables(motor, plugins=TODOS)

        existentes = await tablas()
        esperadas = {m.__tablename__ for m in plugin_models(TODOS)}
        assert esperadas <= existentes, f"no se crearon: {sorted(esperadas - existentes)}"

        await drop_identity_tables(motor, plugins=TODOS)

        quedaron = await tablas()
        assert not esperadas & quedaron
        assert "darwin_user" not in quedaron


# ── El env.py generado ────────────────────────────────────────────────────────
def _template_del_env_py() -> str:
    """
    El bloque que `_setup_alembic` inyecta en el `env.py`, como texto.

    Se saca del AST y no corriendo `_setup_alembic`, que arranca `alembic init` por subproceso y
    escribe en disco. El `{models_import}` se sustituye por un import cualquiera para que el
    fragmento sea parseable.
    """
    from hexcore.infrastructure import cli

    arbol = ast.parse(inspect.getsource(cli))
    for nodo in ast.walk(arbol):
        if not isinstance(nodo, ast.JoinedStr):
            continue
        partes: list[str] = []
        for pieza in nodo.values:
            if isinstance(pieza, ast.Constant) and isinstance(pieza.value, str):
                partes.append(pieza.value)
            else:
                partes.append("import models")
        texto = "".join(partes)
        if "DARWIN_PLUGINS" in texto:
            return texto
    raise AssertionError("no encontré el template del env.py con DARWIN_PLUGINS")


class TestElEnvPyGenerado:
    def test_llama_a_ensure_identity_schema_loaded(self) -> None:
        assert "ensure_identity_schema_loaded(plugins=DARWIN_PLUGINS)" in _template_del_env_py()

    def test_ya_no_afirma_que_el_loader_del_framework_cubre_identidad(self) -> None:
        """
        El comentario viejo decía "y las tablas de identidad cuando uses el modulo".

        Era falso: `ensure_framework_models_loaded()` importa un solo módulo, `cron_sql`. Un
        comentario que promete cobertura que el código no da es peor que ninguno, porque el
        consumidor deja de buscar.
        """
        template = _template_del_env_py()
        assert "las tablas de identidad cuando" not in template
        assert "NO cubre las tablas de Darwin" in template

    def test_el_fragmento_es_python_valido(self) -> None:
        """Si no parsea, el `env.py` generado no arranca y `alembic` no corre."""
        ast.parse(_template_del_env_py())

    def test_el_import_de_darwin_es_opcional(self) -> None:
        """
        Sin el extra `[darwin]` el `env.py` tiene que seguir funcionando.

        El `env.py` lo genera `hexcore init` para cualquier proyecto, use identidad o no.
        """
        template = _template_del_env_py()
        assert "except ImportError" in template
        i = template.index("ensure_identity_schema_loaded")
        assert template.index("try:") < i

    def test_el_orden_es_framework_identidad_consumidor(self) -> None:
        """
        Los del framework primero para que un fallo importando los del consumidor no deje el
        metadata a medias.
        """
        template = _template_del_env_py()
        assert (
            template.index("ensure_framework_models_loaded()")
            < template.index("ensure_identity_schema_loaded")
            < template.index("import_all_models(models)")
        )


# ── La red de contención: el paso de arranque ─────────────────────────────────
class TestElPasoDeArranqueVerificaLosPlugins:
    def _paso(self) -> t.Any:
        from hexcore.darwin.infrastructure.lifespan import IdentityStep

        return IdentityStep.__new__(IdentityStep)

    def test_avisa_por_la_tabla_de_un_plugin_que_falta(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Es la red que agarra al que se olvidó de poner el plugin en `DARWIN_PLUGINS`.

        Se falsea el modelo en vez de sacar la tabla del metadata porque el metadata es global y
        compartido con el resto de la suite: desregistrar una tabla ahí rompería otros tests.
        """
        import logging

        paso = self._paso()

        class ModeloFantasma:
            __tablename__ = "darwin_tabla_que_no_existe"

        monkeypatch.setattr(
            paso, "_nombres_de_los_plugins", lambda: ("two_factor",), raising=False
        )
        monkeypatch.setattr(
            paso, "_modelos_de_los_plugins", lambda: [ModeloFantasma], raising=False
        )

        with caplog.at_level(logging.ERROR):
            paso._verificar_esquema()

        assert "darwin_tabla_que_no_existe" in caplog.text
        assert "op.drop_table" in caplog.text
        # El mensaje trae la lista para copiar al env.py, que es la remediación.
        assert "two_factor" in caplog.text

    def test_no_avisa_cuando_todo_esta_registrado(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        ensure_identity_schema_loaded(plugins=TODOS)
        paso = self._paso()
        monkeypatch.setattr(
            paso, "_nombres_de_los_plugins", lambda: TODOS, raising=False
        )

        with caplog.at_level(logging.ERROR):
            paso._verificar_esquema()

        assert caplog.text == ""

    def test_sin_contenedor_no_explota(self) -> None:
        """
        `_verificar_esquema` corre después de `configure_identity`, pero el paso se puede
        instanciar y llamar suelto — y un chequeo que existe para avisar no puede ser lo que
        impide arrancar.
        """
        from hexcore.darwin.application.container import reset_identity

        reset_identity()
        paso = self._paso()
        assert paso._nombres_de_los_plugins() == ()
        assert paso._modelos_de_los_plugins() == []


# ── Mongo: los documentos llegan a init_beanie ────────────────────────────────
class TestLosDocumentosLleganAInitBeanie:
    def test_plugin_documents_junta_los_de_cada_plugin(self) -> None:
        pytest.importorskip("beanie")
        from hexcore.darwin.infrastructure.orms.beanie.schema import plugin_documents

        assert plugin_documents(list(SIN_TABLA)) == []
        assert len(plugin_documents(TODOS)) == sum(CON_DOCUMENTOS.values())

    def test_identity_documents_suma_los_de_los_plugins(self) -> None:
        pytest.importorskip("beanie")
        from hexcore.darwin.infrastructure.orms.beanie.schema import identity_documents

        assert len(identity_documents()) == 6
        todos = identity_documents(plugins=TODOS)
        assert len(todos) == 6 + sum(CON_DOCUMENTOS.values())
        nombres = {d.__name__ for d in todos}
        assert {"TwoFactorDocument", "OAuthStateDocument", "PasskeyDocument"} <= nombres

    def test_los_del_nucleo_van_primero(self) -> None:
        """
        No por FK —Mongo no valida referencias— sino porque `documents=` reemplaza la lista
        entera: alguien que subclasea el documento de usuario espera que su reemplazo siga en la
        posición del original.
        """
        pytest.importorskip("beanie")
        from hexcore.darwin.infrastructure.orms.beanie.schema import identity_documents

        todos = identity_documents(plugins=TODOS)
        assert [d.__name__ for d in todos[:6]] == [
            d.__name__ for d in identity_documents()
        ]

    def test_init_identity_documents_acepta_plugins(self) -> None:
        """
        Tiene que ser un parámetro de **esta** llamada y no una función aparte.

        `init_beanie` no acumula: la segunda llamada sobre la misma base reemplaza el registro de
        la primera, así que inicializar el núcleo y después cada plugin dejaría funcionando sólo
        al último.
        """
        pytest.importorskip("beanie")
        from hexcore.darwin.infrastructure.orms.beanie.schema import (
            init_identity_documents,
        )

        firma = inspect.signature(init_identity_documents)
        assert "plugins" in firma.parameters
        assert firma.parameters["plugins"].kind is inspect.Parameter.KEYWORD_ONLY
