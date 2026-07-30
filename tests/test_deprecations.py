"""
Deprecación de la superficie de API anterior a 5.0.

Los alias de v1/v2 siguen funcionando —no se han borrado— pero avisan y se eliminarán en
6.0. Este módulo fija las tres propiedades que hacen que la deprecación sirva de algo:

1. El alias **sigue funcionando** y devuelve el objeto canónico.
2. Pedirlo emite un `DeprecationWarning` que apunta **al código del usuario**, no a las
   tripas de HexCore. Un warning que señala un fichero de la librería es inútil.
3. Importar HexCore, o usar los nombres canónicos, **no** emite nada. Si el import avisara,
   el usuario no podría saber quién usa el alias, y el aviso se volvería ruido a ignorar.
"""
from __future__ import annotations

import importlib
import warnings

import pytest

from hexcore._deprecation import REMOVED_IN


@pytest.fixture
def anyio_backend():
    return "asyncio"


# (módulo, alias deprecado, nombre canónico)
ALIASES = [
    ("hexcore.domain.cqrs.buses", "ICommandBus", "AbstractCommandBus"),
    ("hexcore.domain.cqrs.buses", "IQueryBus", "AbstractQueryBus"),
    ("hexcore.domain.cqrs.buses", "IEventBus", "AbstractEventBus"),
    ("hexcore.domain.cqrs.handlers", "ICommandHandler", "AbstractCommandHandler"),
    ("hexcore.domain.cqrs.handlers", "IQueryHandler", "AbstractQueryHandler"),
    ("hexcore.domain.cqrs.middleware", "IMiddleware", "AbstractMiddleware"),
    ("hexcore.domain.cqrs.serializer", "ISerializer", "AbstractSerializer"),
    ("hexcore.domain.events", "IEventDispatcher", "EventBus"),
    ("hexcore.domain.cqrs", "ICommandBus", "AbstractCommandBus"),
    ("hexcore.domain.cqrs", "IQueryBus", "AbstractQueryBus"),
    ("hexcore.domain.cqrs", "IEventBus", "AbstractEventBus"),
    ("hexcore.domain.cqrs", "ICommandHandler", "AbstractCommandHandler"),
    ("hexcore.domain.cqrs", "IQueryHandler", "AbstractQueryHandler"),
    ("hexcore.domain.cqrs", "IMiddleware", "AbstractMiddleware"),
    ("hexcore.domain.cqrs", "ISerializer", "AbstractSerializer"),
    (
        "hexcore.infrastructure.events.events_backends.memory",
        "InMemoryEventDispatcher",
        "InMemoryEventBus",
    ),
]

# Los que dependen de un extra.
OPTIONAL_ALIASES = [
    (
        "hexcore.infrastructure.repositories.implementations",
        "SQLAlchemyCommonImplementationsRepo",
        "SqlAlchemyRepository",
    ),
    (
        "hexcore.infrastructure.repositories.implementations",
        "BeanieODMCommonImplementationsRepo",
        "BeanieRepository",
    ),
    ("hexcore.infrastructure.uow", "NoSqlUnitOfWork", "BeanieUnitOfWork"),
]


# ── 1. El alias sigue funcionando ──────────────────────────────────────────────


@pytest.mark.parametrize(("module_path", "alias", "canonical"), ALIASES + OPTIONAL_ALIASES)
def test_alias_resolves_to_the_canonical_object(module_path, alias, canonical):
    module = importlib.import_module(module_path)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        assert getattr(module, alias) is getattr(module, canonical)


# ── 2. Avisa, y el aviso es útil ───────────────────────────────────────────────


@pytest.mark.parametrize(("module_path", "alias", "canonical"), ALIASES)
def test_alias_emits_a_deprecation_warning(module_path, alias, canonical):
    module = importlib.import_module(module_path)

    with pytest.warns(DeprecationWarning) as record:
        getattr(module, alias)

    assert len(record) >= 1
    message = str(record[0].message)
    assert alias in message
    assert canonical in message, "el aviso no dice qué usar en su lugar"
    assert REMOVED_IN in message, "el aviso no dice cuándo se elimina"


@pytest.mark.parametrize(("module_path", "alias", "_canonical"), ALIASES)
def test_the_warning_points_at_the_caller_not_at_hexcore(module_path, alias, _canonical):
    """
    Un warning cuyo `filename` es un fichero de HexCore no le sirve a nadie: el usuario
    necesita saber **su** línea. Lo garantiza el `stacklevel`.
    """
    module = importlib.import_module(module_path)

    with pytest.warns(DeprecationWarning) as record:
        getattr(module, alias)

    assert record[0].filename == __file__, (
        f"el aviso apunta a {record[0].filename}, no al código que pidió el alias"
    )


# ── 3. Nada avisa si no usás la API vieja ──────────────────────────────────────


@pytest.mark.parametrize(
    "module_path",
    [
        "hexcore",
        "hexcore.cqrs",
        "hexcore.sql",
        "hexcore.domain.cqrs",
        "hexcore.domain.events",
        "hexcore.application.cqrs",
        "hexcore.infrastructure.uow",
        "hexcore.testing",
    ],
)
def test_importing_does_not_warn(module_path):
    """
    Se importa en un subproceso porque los módulos ya están en `sys.modules` y un import
    repetido no reejecuta nada.
    """
    import os
    import subprocess
    import sys

    code = f"""
import warnings
warnings.simplefilter("error", DeprecationWarning)
import {module_path}
print("ok")
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )

    assert result.returncode == 0, (
        f"importar {module_path} emitió un DeprecationWarning:\n{result.stderr}"
    )


@pytest.mark.parametrize(("module_path", "_alias", "canonical"), ALIASES)
def test_the_canonical_name_does_not_warn(module_path, _alias, canonical):
    module = importlib.import_module(module_path)

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert getattr(module, canonical) is not None


def test_the_whole_suite_of_canonical_imports_is_clean():
    """El camino recomendado del README no debe emitir un solo aviso."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)

        import hexcore.cqrs as cqrs

        assert cqrs.AbstractCommandBus is not None
        assert cqrs.AbstractSerializer is not None
        assert cqrs.HandlerRegistry is not None

        registry = cqrs.HandlerRegistry()
        cqrs.InMemoryCommandBus(registry=registry)


# ── Métodos y funciones deprecadas ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_event_bus_register_warns_and_delegates():
    from hexcore.infrastructure.events.events_backends.memory import InMemoryEventBus

    bus = InMemoryEventBus()
    seen: list[object] = []

    async def handler(event: object) -> None:
        seen.append(event)

    class Evt:
        pass

    with pytest.warns(DeprecationWarning, match="subscribe"):
        bus.register(Evt, handler)

    with pytest.warns(DeprecationWarning, match="publish"):
        await bus.dispatch(Evt())

    assert len(seen) == 1, "el alias deprecado no delegó en la API nueva"


@pytest.mark.anyio
async def test_reset_sqlalchemy_engine_warns_and_delegates():
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("aiosqlite")

    from hexcore.infrastructure.repositories.orms.sqlalchemy import session

    session.init_engine("sqlite+aiosqlite:///:memory:")
    assert session._engine is not None

    with pytest.warns(DeprecationWarning, match="dispose_engine"):
        await session.reset_sqlalchemy_engine()

    assert session._engine is None


@pytest.mark.anyio
async def test_server_config_event_dispatcher_still_warns():
    from hexcore.config import ServerConfig

    config = ServerConfig()

    with pytest.warns(DeprecationWarning):
        assert config.event_dispatcher is config.event_bus


# ── Un nombre inexistente sigue siendo AttributeError ──────────────────────────


@pytest.mark.parametrize(
    "module_path",
    [
        "hexcore.domain.cqrs",
        "hexcore.domain.cqrs.buses",
        "hexcore.domain.events",
        "hexcore.infrastructure.uow",
    ],
)
def test_unknown_attribute_still_raises_attribute_error(module_path):
    """El `__getattr__` de deprecación no debe tragarse los errores de tipeo."""
    module = importlib.import_module(module_path)

    with pytest.raises(AttributeError, match="has no attribute"):
        module.EstoNoExiste
