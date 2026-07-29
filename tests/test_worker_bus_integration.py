"""
Tests de integración bus + consumer con buses **reales** (no AsyncMock).

Cubre P0-1 y P0-2 del plan de mejora: el worker debe *ejecutar* los mensajes de
background que saca de la cola, no reencolarlos. Con un `AsyncMock` como bus estos
bugs son invisibles, que es exactamente por qué pasaron el CI.
"""
from __future__ import annotations

import typing as t

import pytest

from hexcore.application.cqrs.in_memory_buses import InMemoryCommandBus, InMemoryEventBus
from hexcore.application.cqrs.registry import HandlerRegistry
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.decorators import (
    background_command,
    background_handler,
    background_task,
)
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer
from hexcore.domain.events import DomainEvent
from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer
from hexcore.infrastructure.workers.consumer import CQRSConsumer


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SpyEnqueuer(ITaskEnqueuer):
    """Enqueuer que sólo anota lo que se le pide encolar."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, t.Any], str]] = []
        self.events: list[tuple[str, dict[str, t.Any], str]] = []
        self.handlers: list[tuple[str, dict[str, t.Any], str]] = []
        self.tasks: list[tuple[str, dict[str, t.Any], str]] = []

    async def enqueue_command(self, command_name: str, payload: dict[str, t.Any], queue: str) -> None:
        self.commands.append((command_name, payload, queue))

    async def enqueue_event(self, event_name: str, payload: dict[str, t.Any], queue: str) -> None:
        self.events.append((event_name, payload, queue))

    async def enqueue_handler(self, handler_name: str, payload: dict[str, t.Any], queue: str) -> None:
        self.handlers.append((handler_name, payload, queue))

    async def enqueue_task(self, task_name: str, payload: dict[str, t.Any], queue: str) -> None:
        self.tasks.append((task_name, payload, queue))


# ── Mensajes y handlers a nivel de módulo (resolubles desde un worker) ──────────

HANDLED: list[str] = []
NESTED_HANDLED: list[str] = []
EVENT_HANDLED: list[str] = []


@background_command(queue="reactive")
class ReactiveCommand(Command):
    value: str


class ReactiveCommandHandler:
    async def handle(self, cmd: ReactiveCommand) -> str:
        HANDLED.append(cmd.value)
        return f"ok:{cmd.value}"


@background_command(queue="nested")
class NestedCommand(Command):
    value: str


class NestedCommandHandler:
    async def handle(self, cmd: NestedCommand) -> None:
        NESTED_HANDLED.append(cmd.value)


class SyncCommand(Command):
    value: str


class DispatchingCommandHandler:
    """Handler síncrono que despacha *a propósito* un comando de background."""

    def __init__(self, bus: InMemoryCommandBus) -> None:
        self._bus = bus

    async def handle(self, cmd: SyncCommand) -> None:
        HANDLED.append(cmd.value)
        await self._bus.dispatch(NestedCommand(value=f"nested:{cmd.value}"))


class IntegrationEvent(DomainEvent):
    info: str


@background_handler(queue="analytics")
async def on_integration_event(event: IntegrationEvent) -> None:
    EVENT_HANDLED.append(event.info)


@pytest.fixture(autouse=True)
def _reset_spies():
    HANDLED.clear()
    NESTED_HANDLED.clear()
    EVENT_HANDLED.clear()
    yield
    HANDLED.clear()
    NESTED_HANDLED.clear()
    EVENT_HANDLED.clear()


def _build(registry: HandlerRegistry | None = None):
    registry = registry or HandlerRegistry()
    enqueuer = SpyEnqueuer()
    serializer = PydanticSerializer()
    command_bus = InMemoryCommandBus(
        registry=registry, enqueuer=enqueuer, serializer=serializer
    )
    event_bus = InMemoryEventBus(enqueuer=enqueuer, serializer=serializer)
    consumer = CQRSConsumer(
        command_bus=command_bus, event_bus=event_bus, serializer=serializer
    )
    return registry, enqueuer, serializer, command_bus, event_bus, consumer


# ── P0-1 ───────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_worker_executes_background_command_instead_of_reenqueueing():
    """El bug original: el worker reencolaba el comando en bucle infinito."""
    registry, enqueuer, serializer, _bus, _evt, consumer = _build()
    registry.register_command_handler(ReactiveCommand, ReactiveCommandHandler())

    await consumer.process_command(serializer.serialize(ReactiveCommand(value="x")))

    assert HANDLED == ["x"], "el handler del comando de background no se ejecutó"
    assert enqueuer.commands == [], "el worker reencoló el comando en vez de ejecutarlo"


@pytest.mark.anyio
async def test_same_bus_outside_worker_still_enqueues():
    """Fuera del worker, el mismo bus debe seguir encolando."""
    registry, enqueuer, _ser, bus, _evt, _consumer = _build()
    registry.register_command_handler(ReactiveCommand, ReactiveCommandHandler())

    result = await bus.dispatch(ReactiveCommand(value="y"))

    assert result is None
    assert HANDLED == []
    assert [name for name, _p, _q in enqueuer.commands] == ["ReactiveCommand"]
    assert enqueuer.commands[0][2] == "reactive"


@pytest.mark.anyio
async def test_nested_dispatch_from_handler_still_enqueues():
    """
    El contextvar no debe convertir el worker en "todo local para siempre":
    un comando que el handler despacha a propósito sigue yendo a la cola.
    """
    registry, enqueuer, serializer, bus, _evt, consumer = _build()
    registry.register_command_handler(SyncCommand, DispatchingCommandHandler(bus))
    registry.register_command_handler(NestedCommand, NestedCommandHandler())

    await consumer.process_command(serializer.serialize(SyncCommand(value="outer")))

    assert HANDLED == ["outer"]
    assert NESTED_HANDLED == [], "el comando anidado se ejecutó inline en vez de encolarse"
    assert [name for name, _p, _q in enqueuer.commands] == ["NestedCommand"]


@pytest.mark.anyio
async def test_worker_context_is_reset_after_processing():
    """Tras procesar, el proceso vuelve a encolar los comandos de background."""
    registry, enqueuer, serializer, bus, _evt, consumer = _build()
    registry.register_command_handler(ReactiveCommand, ReactiveCommandHandler())

    await consumer.process_command(serializer.serialize(ReactiveCommand(value="a")))
    await bus.dispatch(ReactiveCommand(value="b"))

    assert HANDLED == ["a"]
    assert len(enqueuer.commands) == 1


# ── P0-2 ───────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_worker_executes_background_event_handler():
    """Mismo bucle que P0-1, por la vía del EventBus."""
    _registry, enqueuer, serializer, _bus, event_bus, consumer = _build()
    event_bus.subscribe(IntegrationEvent, on_integration_event)

    await consumer.process_event(serializer.serialize(IntegrationEvent(info="evt")))

    assert EVENT_HANDLED == ["evt"]
    assert enqueuer.handlers == [], "el worker reencoló el event handler de background"


@pytest.mark.anyio
async def test_event_bus_outside_worker_still_enqueues_background_handler():
    _registry, enqueuer, _ser, _bus, event_bus, _consumer = _build()
    event_bus.subscribe(IntegrationEvent, on_integration_event)

    await event_bus.publish(IntegrationEvent(info="evt"))

    assert EVENT_HANDLED == []
    assert len(enqueuer.handlers) == 1
    assert enqueuer.handlers[0][2] == "analytics"


@pytest.mark.anyio
async def test_sync_event_handler_runs_in_worker_and_outside():
    _registry, enqueuer, serializer, _bus, event_bus, consumer = _build()
    seen: list[str] = []

    async def sync_handler(event: IntegrationEvent) -> None:
        seen.append(event.info)

    event_bus.subscribe(IntegrationEvent, sync_handler)

    await event_bus.publish(IntegrationEvent(info="local"))
    await consumer.process_event(serializer.serialize(IntegrationEvent(info="worker")))

    assert seen == ["local", "worker"]
    assert enqueuer.handlers == []


# ── Camino de handler individual y tarea genérica (con resolución real) ────────


@pytest.mark.anyio
async def test_consumer_resolves_module_level_handler():
    _registry, _enq, serializer, _bus, _evt, consumer = _build()

    await consumer.process_handler(
        f"{__name__}.on_integration_event",
        serializer.serialize(IntegrationEvent(info="direct")),
    )

    assert EVENT_HANDLED == ["direct"]


class Jobs:
    """Contenedor de tareas: ejercita la resolución de __qualname__ anidado (P0-3)."""

    ran: list[int] = []

    @staticmethod
    @background_task(queue="maintenance")
    async def cleanup(days: int) -> None:
        Jobs.ran.append(days)


@pytest.mark.anyio
async def test_consumer_resolves_nested_static_method_task():
    _registry, _enq, _ser, _bus, _evt, consumer = _build()
    Jobs.ran.clear()

    task_name = getattr(Jobs.cleanup, "__cqrs_task_name__")
    assert task_name == f"{__name__}.Jobs.cleanup"

    await consumer.process_task(task_name, {"days": 30})

    assert Jobs.ran == [30]
