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
import typing as t
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _read(name: str) -> str:
    return (REPO_ROOT / name).read_text(encoding="utf-8")


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
    """El ejemplo de "una app HexCore en una pantalla" de DOCS.md."""
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

DOC_FILES = ["README.md", "DOCS.md"]


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

    for legacy in ("ICommandBus", "IQueryBus", "ISerializer", "IMiddleware"):
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

    for doc in DOC_FILES:
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
    pattern = re.compile(r"(?<![\w.])(hx|cqrs|sql)\.([A-Za-z_][A-Za-z0-9_]*)\b")
    missing: list[str] = []

    for doc in DOC_FILES:
        for alias, attribute in pattern.findall(_read(doc)):
            facade = importlib.import_module(aliases[alias])
            if attribute not in facade.__all__:
                missing.append(f"{doc}: {alias}.{attribute}")

    assert not missing, "la documentación usa atributos que la fachada no exporta: " + ", ".join(
        sorted(set(missing))
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

    readme = _read("README.md")
    policy = readme.split("## Versiones y soporte", 1)[1].split("## ", 1)[0]

    for major in range(1, current_major + 1):
        assert f"**{major}.x**" in policy, (
            f"la tabla de soporte del README no menciona la serie {major}.x "
            f"(versión actual: {version})"
        )


def test_support_policy_marks_only_the_current_major_as_active():
    import tomllib

    version = tomllib.loads(
        (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    current_major = version.split(".")[0]

    policy = _read("README.md").split("## Versiones y soporte", 1)[1].split("## ", 1)[0]
    active_rows = [line for line in policy.splitlines() if "**Activa**" in line]

    assert len(active_rows) == 1, "debe haber exactamente una serie activa"
    assert f"**{current_major}.x**" in active_rows[0], (
        f"la serie activa del README no es la {current_major}.x"
    )


def test_deprecated_api_table_lists_real_symbols():
    """
    Los nombres de la columna "Deprecado" tienen que existir todavía (si no, la tabla
    miente sobre que siguen funcionando), y los de la columna "Usá en su lugar" también.
    """
    import importlib

    modules = [
        "hexcore.domain.cqrs",
        "hexcore.domain.events",
        "hexcore.infrastructure.uow",
        "hexcore.infrastructure.repositories.implementations",
        "hexcore.infrastructure.repositories.orms.sqlalchemy.session",
    ]
    available: set[str] = set()
    for path in modules:
        try:
            available |= set(dir(importlib.import_module(path)))
        except ImportError:
            continue

    # Se comprueban los que no dependen de extras ni son atributos de instancia.
    must_exist = [
        "ICommandBus", "IQueryBus", "IEventBus",
        "ICommandHandler", "IQueryHandler",
        "IMiddleware", "ISerializer",
        "AbstractCommandBus", "AbstractQueryBus", "AbstractEventBus",
        "AbstractCommandHandler", "AbstractQueryHandler",
        "AbstractMiddleware", "AbstractSerializer",
    ]
    missing = [name for name in must_exist if name not in available]

    assert not missing, f"la tabla de API deprecada menciona símbolos que no existen: {missing}"

    readme = _read("README.md")
    for name in must_exist:
        assert name in readme, f"{name} no aparece en el README"
