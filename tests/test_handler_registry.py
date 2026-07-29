"""
P1-5: el `HandlerRegistry` decía ser thread-safe y no lo era.

`resolve_*` hace lazy-init con escritura en el dict, sin ningún lock. Con dos hilos
(y con el free-threading de Python 3.14 son hilos reales) el handler se instanciaba
dos veces y cada hilo se quedaba con la suya.

También cubre la ambigüedad de `callable(entry) and not isinstance(entry, ICommandHandler)`
cuando un handler implementa `__call__`.
"""
from __future__ import annotations

import threading
import typing as t

import pytest

from hexcore.application.cqrs.registry import HandlerRegistry
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.exceptions import DuplicateHandlerError, HandlerNotFoundError
from hexcore.domain.cqrs.queries import Query


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SomeCommand(Command):
    value: str


class SomeQuery(Query[str]):
    value: str


class SomeHandler:
    instances = 0

    def __init__(self) -> None:
        type(self).instances += 1

    async def handle(self, message: t.Any) -> str:
        return "ok"


class CallableHandler:
    """Handler que además implementa `__call__`: el caso ambiguo del plan."""

    async def handle(self, message: t.Any) -> str:
        return "handled"

    def __call__(self) -> "CallableHandler":  # pragma: no cover - no debe invocarse
        raise AssertionError("se trató un handler callable como si fuera un factory")


# ── Thread-safety ──────────────────────────────────────────────────────────────


def test_concurrent_resolution_instantiates_the_handler_once():
    SomeHandler.instances = 0
    registry = HandlerRegistry()
    ready = threading.Barrier(8)
    resolved: list[object] = []
    lock = threading.Lock()

    def build() -> SomeHandler:
        return SomeHandler()

    registry.register_command_handler(SomeCommand, HandlerRegistry.factory(build))

    def worker() -> None:
        ready.wait()
        handler = registry.resolve_command_handler(SomeCommand)
        with lock:
            resolved.append(handler)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert SomeHandler.instances == 1, "el handler se instanció más de una vez"
    assert len({id(handler) for handler in resolved}) == 1, "los hilos vieron instancias distintas"


def test_concurrent_registration_of_distinct_types_does_not_lose_entries():
    registry = HandlerRegistry()
    command_types = [
        type(f"Cmd{i}", (Command,), {"__annotations__": {"value": str}})
        for i in range(50)
    ]
    ready = threading.Barrier(5)

    def worker(chunk: list[type[Command]]) -> None:
        ready.wait()
        for command_type in chunk:
            registry.register_command_handler(command_type, SomeHandler())

    chunks = [command_types[i::5] for i in range(5)]
    threads = [threading.Thread(target=worker, args=(chunk,)) for chunk in chunks]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(registry.registered_commands) == 50


def test_factory_can_resolve_another_handler_reentrantly():
    """El lock es reentrante: un factory puede pedirle otro handler al registry."""
    registry = HandlerRegistry()
    registry.register_command_handler(SomeCommand, SomeHandler())

    def build() -> SomeHandler:
        registry.resolve_command_handler(SomeCommand)
        return SomeHandler()

    other = type("OtherCommand", (Command,), {"__annotations__": {"value": str}})
    registry.register_command_handler(other, HandlerRegistry.factory(build))

    assert registry.resolve_command_handler(other) is not None


# ── Marcador explícito de factory ──────────────────────────────────────────────


def test_callable_handler_is_not_mistaken_for_a_factory():
    registry = HandlerRegistry()
    handler = CallableHandler()
    registry.register_command_handler(SomeCommand, handler)

    assert registry.resolve_command_handler(SomeCommand) is handler


def test_explicit_factory_marker_is_invoked_and_cached():
    SomeHandler.instances = 0
    registry = HandlerRegistry()
    registry.register_command_handler(
        SomeCommand, HandlerRegistry.factory(lambda: SomeHandler())
    )

    first = registry.resolve_command_handler(SomeCommand)
    second = registry.resolve_command_handler(SomeCommand)

    assert first is second
    assert SomeHandler.instances == 1


def test_bare_lambda_factory_still_works():
    """Retrocompatibilidad: el callable pelado sigue detectándose por heurística."""
    SomeHandler.instances = 0
    registry = HandlerRegistry()
    registry.register_command_handler(SomeCommand, lambda: SomeHandler())

    assert registry.resolve_command_handler(SomeCommand) is not None
    assert SomeHandler.instances == 1


def test_query_factory_marker_works_too():
    SomeHandler.instances = 0
    registry = HandlerRegistry()
    registry.register_query_handler(
        SomeQuery, HandlerRegistry.factory(lambda: SomeHandler())
    )

    first = registry.resolve_query_handler(SomeQuery)

    assert first is registry.resolve_query_handler(SomeQuery)
    assert SomeHandler.instances == 1


# ── Comportamiento existente que no debe cambiar ───────────────────────────────


def test_duplicate_registration_still_raises():
    registry = HandlerRegistry()
    registry.register_command_handler(SomeCommand, SomeHandler())

    with pytest.raises(DuplicateHandlerError):
        registry.register_command_handler(SomeCommand, SomeHandler())


def test_allow_override_still_works():
    registry = HandlerRegistry(allow_override=True)
    first = SomeHandler()
    second = SomeHandler()
    registry.register_command_handler(SomeCommand, first)
    registry.register_command_handler(SomeCommand, second)

    assert registry.resolve_command_handler(SomeCommand) is second


def test_missing_handler_still_raises():
    registry = HandlerRegistry()

    with pytest.raises(HandlerNotFoundError):
        registry.resolve_command_handler(SomeCommand)
