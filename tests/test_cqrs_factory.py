"""
P0-5: la vía oficial de construcción (`CQRSFactory`) debe producir buses capaces de
Smart Routing. Antes construía `InMemoryCommandBus(registry=..., pipeline=...)` y
nada más, así que el primer `@background_command` reventaba con `RuntimeError`.
"""
from __future__ import annotations

import typing as t

import pytest

from hexcore.application.cqrs.config import BusConfig, CQRSConfig
from hexcore.application.cqrs.factory import CQRSFactory
from hexcore.application.cqrs.registry import HandlerRegistry
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.decorators import background_command, background_handler
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer
from hexcore.domain.events import DomainEvent


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SpyEnqueuer(ITaskEnqueuer):
    def __init__(self) -> None:
        self.commands: list[tuple[str, dict[str, t.Any], str]] = []
        self.handlers: list[tuple[str, dict[str, t.Any], str]] = []

    async def enqueue_command(self, command_name: str, payload: dict[str, t.Any], queue: str) -> None:
        self.commands.append((command_name, payload, queue))

    async def enqueue_event(self, event_name: str, payload: dict[str, t.Any], queue: str) -> None:
        raise NotImplementedError

    async def enqueue_handler(self, handler_name: str, payload: dict[str, t.Any], queue: str) -> None:
        self.handlers.append((handler_name, payload, queue))

    async def enqueue_task(self, task_name: str, payload: dict[str, t.Any], queue: str) -> None:
        raise NotImplementedError


@background_command(queue="factory_queue")
class FactoryBackgroundCommand(Command):
    value: str


class PlainCommand(Command):
    value: str


class PlainCommandHandler:
    async def handle(self, cmd: PlainCommand) -> str:
        return f"ok:{cmd.value}"


class FactoryEvent(DomainEvent):
    info: str


@background_handler(queue="factory_events")
async def on_factory_event(event: FactoryEvent) -> None:  # pragma: no cover
    ...


@pytest.mark.anyio
async def test_factory_command_bus_can_route_background_commands():
    registry = HandlerRegistry()
    registry.register_command_handler(FactoryBackgroundCommand, PlainCommandHandler())
    enqueuer = SpyEnqueuer()

    factory = CQRSFactory(CQRSConfig(), registry, enqueuer=enqueuer)
    bus = factory.create_command_bus()

    await bus.dispatch(FactoryBackgroundCommand(value="x"))

    assert [name for name, _p, _q in enqueuer.commands] == ["FactoryBackgroundCommand"]
    assert enqueuer.commands[0][2] == "factory_queue"


@pytest.mark.anyio
async def test_factory_event_bus_can_route_background_handlers():
    registry = HandlerRegistry()
    enqueuer = SpyEnqueuer()

    factory = CQRSFactory(CQRSConfig(), registry, enqueuer=enqueuer)
    event_bus = factory.create_event_bus()
    event_bus.subscribe(FactoryEvent, on_factory_event)

    await event_bus.publish(FactoryEvent(info="i"))

    assert len(enqueuer.handlers) == 1
    assert enqueuer.handlers[0][2] == "factory_events"


def test_factory_fails_at_construction_when_background_commands_have_no_enqueuer():
    registry = HandlerRegistry()
    registry.register_command_handler(FactoryBackgroundCommand, PlainCommandHandler())

    factory = CQRSFactory(CQRSConfig(), registry)

    with pytest.raises(RuntimeError, match="FactoryBackgroundCommand"):
        factory.create_command_bus()


def test_factory_without_enqueuer_still_works_without_background_commands():
    registry = HandlerRegistry()
    registry.register_command_handler(PlainCommand, PlainCommandHandler())

    factory = CQRSFactory(CQRSConfig(), registry)

    assert factory.create_command_bus() is not None


@pytest.mark.anyio
async def test_factory_command_bus_executes_plain_commands():
    registry = HandlerRegistry()
    registry.register_command_handler(PlainCommand, PlainCommandHandler())

    bus = CQRSFactory(CQRSConfig(), registry).create_command_bus()

    assert await bus.dispatch(PlainCommand(value="v")) == "ok:v"


def test_factory_caches_the_serializer():
    factory = CQRSFactory(CQRSConfig(), HandlerRegistry())

    assert factory.create_serializer() is factory.create_serializer()


def test_factory_reports_middlewares_that_need_configuration():
    config = CQRSConfig(
        command_bus=BusConfig(
            middlewares=["hexcore.infrastructure.cqrs.middlewares.TransactionMiddleware"]
        )
    )
    factory = CQRSFactory(config, HandlerRegistry())

    with pytest.raises(ValueError, match="instancialo a mano"):
        factory.create_command_bus()
