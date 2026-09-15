"""
P2-2: `CQRSConsumer` exigía un `event_bus` no-opcional, así que un worker
sólo-comandos tenía que pasar `cast(Any, None)` para satisfacer el tipo.
"""
from __future__ import annotations

import pytest

from hexcore.application.cqrs.in_memory_buses import InMemoryCommandBus
from hexcore.application.cqrs.registry import HandlerRegistry
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.events import DomainEvent
from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer
from hexcore.infrastructure.workers.consumer import CQRSConsumer


@pytest.fixture
def anyio_backend():
    return "asyncio"


class OnlyCommand(Command):
    value: str


class OnlyCommandHandler:
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def handle(self, cmd: OnlyCommand) -> None:
        self.seen.append(cmd.value)


class StrayEvent(DomainEvent):
    info: str


def _command_only_consumer() -> tuple[CQRSConsumer, OnlyCommandHandler, PydanticSerializer]:
    registry = HandlerRegistry()
    handler = OnlyCommandHandler()
    registry.register_command_handler(OnlyCommand, handler)
    serializer = PydanticSerializer()
    consumer = CQRSConsumer(InMemoryCommandBus(registry=registry, serializer=serializer))
    return consumer, handler, serializer


@pytest.mark.anyio
async def test_command_only_worker_needs_no_event_bus():
    consumer, handler, serializer = _command_only_consumer()

    await consumer.process_command(serializer.serialize(OnlyCommand(value="x")))

    assert handler.seen == ["x"]


@pytest.mark.anyio
async def test_event_without_event_bus_raises_a_clear_error():
    consumer, _handler, serializer = _command_only_consumer()

    with pytest.raises(RuntimeError, match="sin 'event_bus'"):
        await consumer.process_event(serializer.serialize(StrayEvent(info="i")))


def test_serializer_defaults_to_pydantic():
    registry = HandlerRegistry()
    consumer = CQRSConsumer(InMemoryCommandBus(registry=registry))

    assert isinstance(consumer._serializer, PydanticSerializer)
