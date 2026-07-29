"""
P0-3: resolución de `__qualname__` para clases y funciones anidadas.

Antes, `PydanticSerializer.deserialize` y `_resolve_callable` partían el FQN con
`rsplit(".", 1)`. Para una clase anidada el `__qualname__` ya contiene puntos, así
que el module path resultante era inválido y el mensaje fallaba **en el worker**,
donde ya no se puede recuperar.
"""
from __future__ import annotations

import pytest

from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.decorators import (
    background_command,
    background_handler,
    background_task,
)
from hexcore.domain.cqrs.resolution import build_fqn, resolve_dotted
from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer
from hexcore.infrastructure.workers.consumer import _resolve_callable


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Outer:
    class InnerCommand(Command):
        value: str

    @staticmethod
    def a_function() -> str:
        return "inner-function"


def module_level_function() -> str:
    return "module-level"


# ── resolve_dotted ─────────────────────────────────────────────────────────────


def test_resolve_dotted_resolves_module_level_object():
    resolved = resolve_dotted(f"{__name__}.module_level_function")
    assert resolved is module_level_function


def test_resolve_dotted_resolves_nested_class():
    resolved = resolve_dotted(f"{__name__}.Outer.InnerCommand")
    assert resolved is Outer.InnerCommand


def test_resolve_dotted_resolves_nested_function():
    resolved = resolve_dotted(f"{__name__}.Outer.a_function")
    assert resolved() == "inner-function"


def test_resolve_dotted_raises_lookup_error_for_unknown():
    with pytest.raises(LookupError):
        resolve_dotted(f"{__name__}.DoesNotExist")


def test_resolve_dotted_raises_lookup_error_for_unknown_module():
    with pytest.raises(LookupError):
        resolve_dotted("no_such_module_at_all.Thing")


def test_build_fqn_keeps_full_qualname():
    assert (
        build_fqn(Outer.InnerCommand)
        == f"{__name__}.Outer.InnerCommand"
    )


# ── Round-trip del serializer ──────────────────────────────────────────────────


def test_serializer_round_trip_of_nested_command():
    serializer = PydanticSerializer()
    payload = serializer.serialize(Outer.InnerCommand(value="nested"))

    assert payload["__type__"] == f"{__name__}.Outer.InnerCommand"

    restored = serializer.deserialize(payload)
    assert isinstance(restored, Outer.InnerCommand)
    assert restored.value == "nested"


# ── _resolve_callable del consumer ─────────────────────────────────────────────


class TaskHolder:
    ran: list[str] = []

    @staticmethod
    @background_task(queue="q")
    async def cleanup() -> None:
        TaskHolder.ran.append("cleanup")


def test_consumer_resolves_nested_static_task():
    task_name = getattr(TaskHolder.cleanup, "__cqrs_task_name__")
    assert task_name == f"{__name__}.TaskHolder.cleanup"
    assert _resolve_callable(task_name) is not None


def test_consumer_resolve_callable_raises_runtime_error_on_unknown():
    with pytest.raises(RuntimeError):
        _resolve_callable(f"{__name__}.nope")


# ── Rechazo en tiempo de decoración de objetos no resolubles ───────────────────


def test_background_task_rejects_local_function():
    with pytest.raises(ValueError, match="<locals>"):

        @background_task(queue="q")
        async def local_task() -> None:  # pragma: no cover - nunca se decora
            ...


def test_background_handler_rejects_local_function():
    with pytest.raises(ValueError, match="<locals>"):

        @background_handler(queue="q")
        async def local_handler(event: object) -> None:  # pragma: no cover
            ...


def test_background_command_rejects_local_class():
    with pytest.raises(ValueError, match="<locals>"):

        @background_command(queue="q")
        class LocalCommand(Command):  # pragma: no cover
            value: str
