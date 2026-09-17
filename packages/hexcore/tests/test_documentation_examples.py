"""
P2-4: los ejemplos de la documentación se ejecutan.

Un ejemplo que nadie ejecuta se desalinea de la API en el primer refactor, y eso es
exactamente lo que había pasado: `registry.register_command(...)` cuando el método es
`register_command_handler`, `process_cqrs_command` cuando la tarea es
`hexcore.process_command`, y `UseCaseCommandHandler(CreateUserUseCase())` sin las
dependencias del use case.

Este módulo hace dos cosas:

1. Ejecuta versiones vivas de los ejemplos clave (los que enseñan el arranque y el
   Smart Routing).
2. Comprueba, contra la API real, que los símbolos y los nombres de método que los
   documentos mencionan existen — para que un rename rompa el CI y no la app de alguien.
"""
from __future__ import annotations

import re
import sys
import typing as t

import pytest

from rutas import PAQUETE as REPO_ROOT
from rutas import REPO


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _read(name: str) -> str:
    """El contenido de un documento, nombrado relativo a la raíz del **monorepo**.

    El ancla es `REPO` y no `PAQUETE` desde que la documentación se consolidó en `docs/` de la
    raíz: con `PAQUETE` no habría una sola ruta que nombre a la vez `packages/hexcore/README.md`
    —que sigue siendo el `readme =` del `pyproject.toml`— y a `docs/hexcore/en/sql.md`, que vive
    dos niveles más arriba. Nombrarlos desde la raíz deja los IDs de pytest legibles y sin
    ambigüedad: hay un `README.md` en la raíz *y* otro en el paquete.
    """
    return (REPO / name).read_text(encoding="utf-8")


# Los mensajes y tareas de los ejemplos van a nivel de módulo, que es exactamente lo que
# los decoradores exigen desde P0-3: definidos dentro de una función no serían resolubles
# desde el worker, y el decorador lo rechaza al aplicarse.
from hexcore.domain.cqrs.commands import Command  # noqa: E402
from hexcore.domain.cqrs.decorators import (  # noqa: E402
    background_command,
    background_task,
)


@background_command(queue="high_priority")
class _SendEmailCommand(Command):
    user_id: str
    template: str


@background_task(queue="maintenance")
async def _clean_old_records_task(days_retention: int) -> None:  # pragma: no cover
    ...


# ── Los ejemplos de arranque, ejecutados ───────────────────────────────────────


def test_docs_startup_example_runs():
    """El ejemplo de arranque de docs/es/inicio-rapido.md."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from hexcore.fastapi import build_lifespan, create_app

    app = create_app(lifespan=build_lifespan(), routers=[])

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_docs_startup_example_with_sql_engine_step_runs():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("aiosqlite")
    from fastapi.testclient import TestClient

    from hexcore.fastapi import SqlEngineStep, build_lifespan, create_app

    app = create_app(
        lifespan=build_lifespan(SqlEngineStep("sqlite+aiosqlite:///:memory:"))
    )

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200


def test_docs_app_features_example_runs():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from hexcore.fastapi import AppFeatures, create_app

    app = create_app(features=AppFeatures(cors=False))

    with TestClient(app) as client:
        response = client.get("/health", headers={"Origin": "http://x.com"})

    assert "access-control-allow-origin" not in response.headers


def test_docs_three_facade_imports_work():
    import hexcore.cqrs as cqrs
    import hexcore.sql as sql

    assert cqrs.Command is not None
    assert sql.session_scope is not None

    pytest.importorskip("fastapi")
    import hexcore.fastapi as hx

    assert hx.create_app is not None


@pytest.mark.anyio
async def test_docs_scopes_example_runs():
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("aiosqlite")

    from sqlalchemy.pool import StaticPool

    import hexcore.sql as sql

    await sql.dispose_engine()
    sql.init_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    try:
        async with sql.session_scope() as session:
            assert session is not None
    finally:
        await sql.dispose_engine()


# ── El ejemplo de migración de UseCase (P2-4) ──────────────────────────────────


@pytest.mark.anyio
async def test_readme_use_case_migration_example_runs():
    """
    El ejemplo del README usaba `registry.register_command(...)` —método que no existe— y
    `UseCaseCommandHandler(CreateUserUseCase())` sin las dependencias del use case.
    """
    import hexcore.cqrs as cqrs
    from hexcore.application.use_cases.base import UseCase

    class CreateUserCommand(cqrs.Command):
        email: str

    class FakeUoW:
        created: list[str] = []

    class CreateUserUseCase(UseCase[CreateUserCommand, str]):
        def __init__(self, uow: t.Any) -> None:
            self.uow = uow

        async def execute(self, request: CreateUserCommand) -> str:
            self.uow.created.append(request.email)
            return request.email

    uow = FakeUoW()
    registry = cqrs.HandlerRegistry()
    registry.register_command_handler(
        CreateUserCommand, cqrs.UseCaseCommandHandler(CreateUserUseCase(uow))
    )

    bus = cqrs.InMemoryCommandBus(registry=registry)
    assert await bus.dispatch(CreateUserCommand(email="a@b.c")) == "a@b.c"
    assert uow.created == ["a@b.c"]


@pytest.mark.anyio
async def test_readme_factory_registration_example_runs():
    """La variante con `HandlerRegistry.factory(...)` del mismo ejemplo."""
    import hexcore.cqrs as cqrs
    from hexcore.application.use_cases.base import UseCase

    class CreateUserCommand(cqrs.Command):
        email: str

    class CreateUserUseCase(UseCase[CreateUserCommand, str]):
        def __init__(self, uow: t.Any) -> None:
            self.uow = uow

        async def execute(self, request: CreateUserCommand) -> str:
            return request.email

    registry = cqrs.HandlerRegistry()
    registry.register_command_handler(
        CreateUserCommand,
        cqrs.HandlerRegistry.factory(
            lambda: cqrs.UseCaseCommandHandler(CreateUserUseCase(object()))
        ),
    )

    bus = cqrs.InMemoryCommandBus(registry=registry)
    assert await bus.dispatch(CreateUserCommand(email="x@y.z")) == "x@y.z"


# ── El ejemplo de Smart Routing y del worker ────────────────────────────────────


@pytest.mark.anyio
async def test_readme_smart_routing_and_worker_example_runs():
    """
    El contrato que documenta P2-5: el mismo bus encola fuera del worker y ejecuta
    dentro de él.
    """
    import hexcore.cqrs as cqrs
    from hexcore.testing import InMemoryTaskEnqueuer

    handled: list[str] = []

    class SendEmailHandler:
        async def handle(self, cmd: t.Any) -> None:
            handled.append(cmd.user_id)

    registry = cqrs.HandlerRegistry()
    registry.register_command_handler(_SendEmailCommand, SendEmailHandler())
    enqueuer = InMemoryTaskEnqueuer()
    serializer = cqrs.PydanticSerializer()

    command_bus = cqrs.InMemoryCommandBus(
        registry=registry, enqueuer=enqueuer, serializer=serializer
    )
    event_bus = cqrs.InMemoryEventBus(enqueuer=enqueuer, serializer=serializer)
    consumer = cqrs.CQRSConsumer(command_bus, event_bus)

    # Fuera del worker: encola.
    await command_bus.dispatch(_SendEmailCommand(user_id="1", template="welcome"))
    assert enqueuer.command_names == ["_SendEmailCommand"]
    assert handled == []

    # Dentro del worker, el MISMO bus: ejecuta.
    await consumer.process_command(enqueuer.commands[0].payload)
    assert handled == ["1"]


@pytest.mark.anyio
async def test_readme_generic_task_enqueue_example_runs():
    """El ejemplo que encola una `@background_task` derivando su nombre y su cola."""
    from hexcore.testing import InMemoryTaskEnqueuer

    enqueuer = InMemoryTaskEnqueuer()

    await enqueuer.enqueue_task(
        task_name=getattr(_clean_old_records_task, "__cqrs_task_name__"),
        payload={"days_retention": 30},
        queue=getattr(_clean_old_records_task, "__cqrs_queue__"),
    )

    assert enqueuer.tasks[0].name.endswith("_clean_old_records_task")
    assert enqueuer.tasks[0].queue == "maintenance"


@pytest.mark.anyio
async def test_readme_cron_job_example_runs():
    """`cron_job()` deriva el task_name del decorador, como dice el README."""
    pytest.importorskip("sqlalchemy")

    import hexcore.cqrs as cqrs

    definition = cqrs.cron_job(
        _clean_old_records_task, "*/5 * * * *", payload={"days_retention": 30}
    )

    assert definition.task_name == getattr(
        _clean_old_records_task, "__cqrs_task_name__"
    )
    assert definition.queue == "maintenance"
    assert definition.cron_expression == "*/5 * * * *"


def test_readme_lock_provider_on_error_example_is_valid():
    """Las dos formas que el README documenta para `on_error`."""
    pytest.importorskip("redis")

    from unittest.mock import AsyncMock

    from hexcore.infrastructure.cqrs.redis_lock import RedisLockProvider

    assert RedisLockProvider(AsyncMock(), on_error="skip").on_error == "skip"
    assert RedisLockProvider(AsyncMock(), on_error="raise").on_error == "raise"


def test_readme_command_only_consumer_example_runs():
    """`cqrs.CQRSConsumer(command_bus)` sin event bus, como dice el README."""
    import hexcore.cqrs as cqrs

    bus = cqrs.InMemoryCommandBus(registry=cqrs.HandlerRegistry())

    assert cqrs.CQRSConsumer(bus) is not None


# ── La documentación no debe mencionar API que no existe ───────────────────────

#: La página de entrada. Los guardas de "no enseñes API que no existe" corren sólo acá:
#: es la que quedó de la documentación vieja, y la única que todavía nombra en prosa la
#: superficie removida —la tabla de deprecación vive en el README—.
#: Los títulos del README que estos tests parten para leer una sección. Van acá y no inline
#: porque el README pasó a inglés y estaban escritos en español en cinco lugares: una constante
#: hace que el próximo cambio de idioma o de redacción sea una línea y no una cacería.
POLICY_HEADING = "## Versions and support"
REMOVED_API_HEADING = "### Removed API and its replacement"
ACTIVE_MARKER = "**Active**"

#: Las guías del paquete Python, ya no bajo `packages/hexcore/docs/` sino en la carpeta `docs/`
#: de la raíz del monorepo, que documenta los **dos** paquetes. Sólo se recorre `docs/hexcore/`:
#: `docs/darwin-client/` es TypeScript, y sus bloques de código no tienen ningún `from hexcore…`
#: que resolver —pasarlos por estos guardas sería ruido, no cobertura—.
DOCS = REPO / "docs" / "hexcore"

#: El README del **paquete** —no el del monorepo—. Es el que empaqueta la wheel y el que
#: renderiza PyPI, y el que lleva la tabla de "Versions and support" que estos tests contrastan
#: contra `pyproject.toml`. La constante existe porque desde la raíz hay dos `README.md` y el
#: literal `"README.md"` resolvía al equivocado.
README = "packages/hexcore/README.md"

DOC_FILES = [README]

#: Toda la documentación, incluida la de `docs/hexcore/`. Los chequeos de "los símbolos que
#: nombra existen" corren sobre esto: son los que convierten un rename en un CI rojo, y no
#: tendría sentido que cubrieran la portada y no las guías, que son las que la gente copia y
#: pega.
#:
#: Se descubre recorriendo el directorio en vez de enumerarlo: una guía nueva que nadie agregue
#: a una lista es exactamente el archivo que se desalinea sin que nadie se entere.
#:
#: Todas las rutas van relativas a la raíz del monorepo, como las espera `_read()`.
#: La skill de agente, que vive en `skills/` y documenta los dos paquetes. Entra en el mismo
#: gate que `docs/` por la misma razón: es prosa que enseña imports, y una prosa que enseña un
#: import inexistente es peor que no tener prosa. Su propia versión anterior enseñaba
#: `SQLAlchemyCommonImplementationsRepo`, borrado en 7.0, porque nada la contrastaba con el
#: paquete.
#:
#: `scripts/` queda afuera: son la herramienta, no la enseñanza, y sus docstrings muestran
#: ejemplos rotos a propósito para explicar qué detectan.
SKILLS = REPO / "skills"

ALL_DOC_FILES = (
    DOC_FILES
    + sorted(str(path.relative_to(REPO)).replace("\\", "/") for path in DOCS.rglob("*.md"))
    + sorted(
        str(path.relative_to(REPO)).replace("\\", "/")
        for path in SKILLS.rglob("*.md")
        if "scripts" not in path.parts
    )
)


def _code_blocks(content: str) -> list[str]:
    """Los bloques de código de un markdown. La prosa puede *mencionar* lo que quiera."""
    return re.findall(r"```[a-zA-Z]*\n(.*?)```", content, re.DOTALL)


# Todos los guardas de abajo miran **sólo los bloques de código**. La prosa puede —y debe—
# nombrar la API vieja: la tabla de deprecación y la guía de migración existen precisamente
# para decirte qué dejar de usar. Lo que no debe pasar es que un *ejemplo*, que es lo que la
# gente copia y pega, enseñe algo que no existe o que está deprecado.


@pytest.mark.parametrize("doc", DOC_FILES)
def test_docs_examples_do_not_use_the_wrong_registry_method(doc):
    """`register_command(` no existe; el método es `register_command_handler`."""
    code = "\n".join(_code_blocks(_read(doc)))

    assert not re.search(r"register_command\(", code), (
        f"un ejemplo de {doc} usa register_command(, que no existe"
    )
    assert not re.search(r"register_query\(", code), (
        f"un ejemplo de {doc} usa register_query(, que no existe"
    )


@pytest.mark.parametrize("doc", DOC_FILES)
def test_docs_examples_do_not_use_the_wrong_task_names(doc):
    """Las tareas del consumidor se llaman `hexcore.process_*`."""
    code = "\n".join(_code_blocks(_read(doc)))

    for wrong in ("process_cqrs_command", "process_cqrs_handler", "process_cqrs_task"):
        assert wrong not in code, f"un ejemplo de {doc} usa {wrong}, que no existe"


@pytest.mark.parametrize("doc", DOC_FILES)
def test_docs_examples_do_not_use_deleted_api(doc):
    code = "\n".join(_code_blocks(_read(doc)))

    assert "MiddlewareConfig" not in code, (
        f"un ejemplo de {doc} usa MiddlewareConfig, que se eliminó en 3.0"
    )


@pytest.mark.parametrize("doc", DOC_FILES)
def test_docs_examples_do_not_teach_the_legacy_aliases(doc):
    """
    S4: los **ejemplos** enseñan un solo nombre por concepto — los canónicos `Abstract*`.
    Los alias siguen existiendo en el código, y la prosa puede nombrarlos para explicar
    que existen; lo que no debe pasar es que un ejemplo los use.
    """
    code = "\n".join(_code_blocks(_read(doc)))

    for legacy in ("AbstractCommandBus", "AbstractQueryBus", "AbstractSerializer", "AbstractMiddleware"):
        assert legacy not in code, (
            f"un ejemplo de {doc} usa el alias legacy {legacy}; usá su nombre canónico Abstract*"
        )


def test_every_hexcore_symbol_referenced_in_the_docs_exists():
    """
    Los símbolos de HexCore que aparecen en un `from hexcore... import ...` de la
    documentación tienen que existir. Es lo que convierte un rename en un CI rojo.
    """
    import importlib

    missing: list[str] = []
    pattern = re.compile(r"^from (hexcore[\w.]*) import ([^\n(]+)$", re.MULTILINE)

    for doc in ALL_DOC_FILES:
        for module_path, names in pattern.findall(_read(doc)):
            try:
                module = importlib.import_module(module_path)
            except ImportError:
                continue  # necesita un extra no instalado
            for raw_name in names.split(","):
                name = raw_name.strip().split(" as ")[0].strip()
                if not name or not name.isidentifier():
                    continue
                if not hasattr(module, name):
                    missing.append(f"{doc}: {module_path}.{name}")

    assert not missing, "la documentación referencia símbolos inexistentes: " + ", ".join(
        missing
    )


def test_docs_facade_attributes_exist():
    """Los `hx.x` / `cqrs.x` / `sql.x` que menciona la documentación existen."""
    import importlib

    aliases = {"hx": "hexcore.fastapi", "cqrs": "hexcore.cqrs", "sql": "hexcore.sql"}
    # `(?<![\w.])` evita capturar el fragmento `cqrs.` de una ruta larga como
    # `hexcore.application.cqrs.commands`, que no es un uso de la fachada.
    #
    # La `/` del lookbehind saca los **nombres de archivo**: desde que la documentación vive en
    # `docs/hexcore/es/` y `docs/hexcore/en/`, un enlace a `./sql.md` o una mención a
    # `hexcore/cqrs.py` matcheaban como si fueran `sql.md` y `cqrs.py` de la fachada. Son rutas,
    # no usos.
    pattern = re.compile(r"(?<![\w./])(hx|cqrs|sql)\.([A-Za-z_][A-Za-z0-9_]*)\b")
    # Los módulos del framework se llaman igual que sus alias, así que la prosa que habla de
    # los **archivos** —"ante `sql.py` y `sql.pyi`, Pyright usa el stub"— matchea igual que un
    # uso de la fachada. Una extensión no es ni va a ser un símbolo exportado.
    extensiones = {"md", "py", "pyi"}
    missing: list[str] = []

    for doc in ALL_DOC_FILES:
        for alias, attribute in pattern.findall(_read(doc)):
            if attribute in extensiones:
                continue
            facade = importlib.import_module(aliases[alias])
            if attribute not in facade.__all__:
                missing.append(f"{doc}: {alias}.{attribute}")

    assert not missing, "la documentación usa atributos que la fachada no exporta: " + ", ".join(
        sorted(set(missing))
    )


# ── Los classifiers de Python dicen la verdad ──────────────────────────────


def test_cada_version_de_python_declarada_tiene_su_pata_en_la_matriz():
    """
    Los `Programming Language :: Python :: X.Y` de `pyproject.toml` y la matriz de
    `pytest.yml` tienen que coincidir exactamente.

    Un classifier no es decoración: es la respuesta a "¿corre en mi Python?", y es de donde
    shields.io saca el badge del README. Prometer una versión que la CI no ejecuta es un verde
    falso con la peor latencia posible — se descubre cuando alguien la instala.

    Al revés también falla: una pata en la matriz que no esté declarada significa que se está
    pagando el runner por una versión que el paquete no dice soportar.
    """
    tomllib = pytest.importorskip("tomllib")
    yaml = pytest.importorskip("yaml")

    with (REPO / "packages" / "hexcore" / "pyproject.toml").open("rb") as archivo:
        classifiers = tomllib.load(archivo)["project"]["classifiers"]

    prefijo = "Programming Language :: Python :: "
    declaradas = {
        c[len(prefijo) :]
        for c in classifiers
        if c.startswith(prefijo) and c[len(prefijo) :][:1].isdigit() and "." in c
    }

    flujo = yaml.safe_load((REPO / ".github" / "workflows" / "pytest.yml").read_text(encoding="utf-8"))
    en_matriz = {str(v) for v in flujo["jobs"]["test"]["strategy"]["matrix"]["python"]}

    assert declaradas == en_matriz, (
        f"pyproject declara {sorted(declaradas)} y pytest.yml corre {sorted(en_matriz)}. "
        "Agreg\u00e1 la versi\u00f3n que falta al otro lado, o sac\u00e1 la que sobra."
    )


# ── La skill de agente ─────────────────────────────────────────────────────────
#
# `skills/hexcore/` ya entra en los guardas de arriba por `ALL_DOC_FILES`: sus `from hexcore…
# import …` y sus `hx.`/`cqrs.`/`sql.` se resuelven contra el paquete real. Lo que esos guardas
# no cubren es la otra mitad de lo que la skill enseña —el cliente TypeScript— ni el frontmatter
# del que depende que la skill exista.


SKILL_DIR = REPO / "skills" / "hexcore"


def _surface():
    """
    El `hexcore_surface.py` de la skill, cargado por ruta.

    Se importa el módulo de la skill en vez de reimplementar el parser acá: si el gate usara
    una segunda implementación, un bug en la de la skill —la que corre el agente— pasaría el
    CI en verde. El test tiene que fallar exactamente cuando falla la herramienta real.
    """
    import importlib.util

    ruta = SKILL_DIR / "scripts" / "hexcore_surface.py"
    if not ruta.exists():
        pytest.skip("la skill no está en este árbol")
    nombre = "_hexcore_surface_skill"
    spec = importlib.util.spec_from_file_location(nombre, ruta)
    assert spec and spec.loader
    modulo = importlib.util.module_from_spec(spec)
    # Registrarlo **antes** de ejecutarlo: `@dataclass` resuelve las anotaciones mirando
    # `sys.modules[cls.__module__]`, y con el módulo sin registrar eso es `None` y revienta
    # con un `AttributeError` que no nombra la causa.
    sys.modules[nombre] = modulo
    spec.loader.exec_module(modulo)
    return modulo


def test_la_skill_tiene_frontmatter_utilizable():
    """
    El frontmatter de `SKILL.md` tiene que **parsear como YAML**, no sólo contener las claves.

    Chequear que el texto `description:` esté presente no alcanza, y no es una hipótesis: la
    primera versión de este test hacía exactamente eso y dejó pasar una `description` con un
    `: ` en el medio (`"...del monorepo: el framework Python..."`), que YAML lee como un mapping
    anidado dentro de un mapping compacto. El parser de Claude Code es lo bastante tolerante
    como para cargarla igual, así que acá todo se veía bien; el de `npx skills` no lo es y
    rechazó la skill entera con "No valid skills found".

    Ese es justo el modo de falla que esta skill documenta en otros lados: no levanta una
    excepción donde está el error, simplemente el agente no la usa nunca.
    """
    yaml = pytest.importorskip("yaml")

    contenido = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

    assert contenido.startswith("---\n"), "SKILL.md tiene que abrir con el frontmatter YAML"
    cierre = contenido.index("\n---\n", 3)
    frontmatter = contenido[4:cierre]

    try:
        metadatos = yaml.safe_load(frontmatter)
    except yaml.YAMLError as error:  # pragma: no cover - el mensaje es el valor del test
        pytest.fail(f"el frontmatter de SKILL.md no parsea como YAML: {error}")

    assert isinstance(metadatos, dict), "el frontmatter tiene que ser un mapping YAML"

    for clave in ("name", "description"):
        assert metadatos.get(clave), f"al frontmatter de SKILL.md le falta `{clave}`"

    assert "darwin-client" in metadatos["description"], (
        "la `description` es lo único que el modelo lee para decidir si activa la skill: si no "
        "nombra el cliente TypeScript, un prompt de frontend no la dispara"
    )


def test_los_imports_del_cliente_typescript_de_la_skill_existen():
    """
    Espejo de `test_every_hexcore_symbol_referenced_in_the_docs_exists`, del lado de
    `@hexcore-js/darwin-client`.

    El parser es Python y lee `packages/darwin-client/src/*.ts`, así que este gate no necesita
    Node y corre en la suite normal. Es lo que convierte un rename en el cliente en un CI rojo
    en vez de en un ejemplo que el agente copia y no compila.
    """
    surface = _surface()

    raiz_cliente = surface.darwin_client_root(surface.package_root())
    if raiz_cliente is None:  # pragma: no cover - sólo si alguien mueve el paquete
        pytest.fail("no se encontró packages/darwin-client")

    exports = surface.darwin_client_exports(raiz_cliente)
    assert exports, "no se leyó ningún export del cliente: el parser quedó desalineado"

    checker = surface.Checker(surface.package_root(), client=exports)

    hallazgos: list[str] = []
    for documento in SKILL_DIR.rglob("*.md"):
        for hallazgo in checker._check_client_imports(
            documento.read_text(encoding="utf-8"),
            str(documento.relative_to(REPO)).replace("\\", "/"),
            fenced=True,
        ):
            hallazgos.append(f"{hallazgo.path}:{hallazgo.line} {hallazgo.symbol} ({hallazgo.detail})")

    assert not hallazgos, "la skill enseña imports que el cliente no exporta: " + ", ".join(
        hallazgos
    )


# ── La política de soporte tiene que reflejar la versión real ───────────────────


def test_support_policy_covers_every_released_major():
    """
    Cada major publicado tiene que aparecer en la tabla de soporte del README. Si se
    releasea un 6.x y nadie actualiza la tabla, el usuario no sabe qué está soportado.
    """
    import tomllib

    version = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    current_major = int(version.split(".")[0])

    readme = _read(README)
    policy = readme.split(POLICY_HEADING, 1)[1].split("## ", 1)[0]

    for major in range(1, current_major + 1):
        assert f"**{major}.x**" in policy, (
            f"la tabla de soporte del README no menciona la serie {major}.x "
            f"(versión actual: {version})"
        )


def test_support_policy_marks_only_the_current_major_as_active():
    """
    Una sola serie activa, y tiene que ser la vigente **o la que está por salir**.

    Lo segundo no es una concesión: es la única forma de que el test sea satisfacible. El
    `CLAUDE.md` pide actualizar esta tabla *antes* de publicar un major, y la versión de
    `pyproject.toml` la escribe `cz bump` recién en el push a `master`. O sea que en el PR que
    trae el `feat!` la tabla dice `N+1.x` y el `pyproject` todavía dice `N`, y exigir igualdad
    estricta dejaba ese PR en rojo por hacer exactamente lo que el procedimiento manda.

    Aceptar `N+1` mantiene lo que el test protege de verdad —que no haya dos series activas, y
    que nadie declare activa una serie arbitraria— y deja de castigar la ventana entre el PR y
    el bump. La alternativa era no tocar la tabla hasta después del bump, y entonces el rojo
    caía en `master`, donde nadie lo está mirando.
    """
    import tomllib

    version = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    current_major = int(version.split(".")[0])
    aceptados = {str(current_major), str(current_major + 1)}

    policy = _read(README).split(POLICY_HEADING, 1)[1].split("## ", 1)[0]
    active_rows = [line for line in policy.splitlines() if ACTIVE_MARKER in line]

    assert len(active_rows) == 1, "debe haber exactamente una serie activa"
    assert any(f"**{major}.x**" in active_rows[0] for major in sorted(aceptados)), (
        f"la serie activa del README no es la {current_major}.x ni la "
        f"{current_major + 1}.x (la que saldría del próximo bump). Fila: {active_rows[0]!r}"
    )


def test_the_removed_api_table_matches_reality():
    """
    La tabla de API removida del README tiene que decir la verdad en **las dos direcciones**:
    los nombres viejos ya no resuelven, y los canónicos que propone sí.

    Antes este test verificaba lo contrario —que los alias siguieran funcionando— porque el
    README prometía eso. Se eliminaron en 7.0, así que se invierte junto con la tabla. Que el
    test siga enumerándolos es lo que evita que la documentación quede prometiendo una
    compatibilidad que ya no existe.
    """
    import importlib

    removidos = [
        ("hexcore.domain.cqrs", "ICommandBus"),
        ("hexcore.domain.cqrs", "IQueryBus"),
        ("hexcore.domain.cqrs", "IEventBus"),
        ("hexcore.domain.cqrs", "ICommandHandler"),
        ("hexcore.domain.cqrs", "IQueryHandler"),
        ("hexcore.domain.cqrs", "IMiddleware"),
        ("hexcore.domain.cqrs", "ISerializer"),
        ("hexcore.domain.cqrs.buses", "ICommandBus"),
        ("hexcore.domain.cqrs.handlers", "ICommandHandler"),
        ("hexcore.domain.cqrs.middleware", "IMiddleware"),
        ("hexcore.domain.cqrs.serializer", "ISerializer"),
        ("hexcore.domain.events", "IEventDispatcher"),
    ]

    sobrevivientes = [
        f"{module_path}.{name}"
        for module_path, name in removidos
        if getattr(importlib.import_module(module_path), name, None) is not None
    ]
    assert not sobrevivientes, (
        "el README dice que estos nombres se removieron, y siguen resolviendo: "
        + ", ".join(sobrevivientes)
    )

    canonicos = [
        ("hexcore.domain.cqrs", "AbstractCommandBus"),
        ("hexcore.domain.cqrs", "AbstractQueryBus"),
        ("hexcore.domain.cqrs", "AbstractEventBus"),
        ("hexcore.domain.cqrs", "AbstractCommandHandler"),
        ("hexcore.domain.cqrs", "AbstractQueryHandler"),
        ("hexcore.domain.cqrs", "AbstractMiddleware"),
        ("hexcore.domain.cqrs", "AbstractSerializer"),
        ("hexcore.domain.events", "EventBus"),
    ]
    faltantes = [
        f"{module_path}.{name}"
        for module_path, name in canonicos
        if getattr(importlib.import_module(module_path), name, None) is None
    ]
    assert not faltantes, (
        "el README propone estos reemplazos y no existen: " + ", ".join(faltantes)
    )

    readme = _read(README)
    for _module_path, name in removidos + canonicos:
        assert name in readme, f"{name} no aparece en el README"

    # La tabla ya no puede prometer que los alias siguen funcionando. El aviso viejo decía que
    # se eliminaban "en 6.0" y salieron igual en 6.0.0, así que la frase tampoco puede volver.
    assert "will be removed in **6.0**" not in readme

    seccion = readme.split(REMOVED_API_HEADING, 1)[-1].split("##", 1)[0]
    for promesa in ("still works", "still work", "still importable", "still available"):
        assert promesa not in seccion, (
            f"la sección de API removida del README dice '{promesa}': esos nombres se "
            f"eliminaron en 7.0 y no resuelven"
        )
