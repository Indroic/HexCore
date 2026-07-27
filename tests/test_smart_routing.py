import pytest
from unittest.mock import AsyncMock

from hexcore.domain.cqrs.task_queues import ITaskEnqueuer
from hexcore.domain.cqrs.decorators import background_command, background_handler, background_task
from hexcore.application.cqrs.in_memory_buses import InMemoryCommandBus, InMemoryEventBus
from hexcore.application.cqrs.registry import HandlerRegistry
from hexcore.infrastructure.workers.consumer import CQRSConsumer
from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.events import DomainEvent


@background_command(queue="high_priority")
class DummyTaskCommand(Command):
    data: str


class DummyTaskEvent(DomainEvent):
    info: str


class MockEnqueuer(ITaskEnqueuer):
    def __init__(self):
        self.enqueue_command_mock = AsyncMock()
        self.enqueue_event_mock = AsyncMock()
        self.enqueue_handler_mock = AsyncMock()
        self.enqueue_task_mock = AsyncMock()

    async def enqueue_command(self, command_name: str, payload: dict, queue: str) -> None:
        await self.enqueue_command_mock(command_name, payload, queue)

    async def enqueue_event(self, event_name: str, payload: dict, queue: str) -> None:
        await self.enqueue_event_mock(event_name, payload, queue)

    async def enqueue_handler(self, handler_name: str, payload: dict, queue: str) -> None:
        await self.enqueue_handler_mock(handler_name, payload, queue)

    async def enqueue_task(self, task_name: str, payload: dict, queue: str) -> None:
        await self.enqueue_task_mock(task_name, payload, queue)


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_smart_routing_command_bus_enqueues_background_commands():
    enqueuer = MockEnqueuer()
    serializer = PydanticSerializer()
    registry = HandlerRegistry()
    
    # El bus ahora es InMemory pero con enqueuer configurado
    bus = InMemoryCommandBus(registry=registry, enqueuer=enqueuer, serializer=serializer)
    
    cmd = DummyTaskCommand(data="hello")
    result = await bus.dispatch(cmd)
    
    # Background commands do not execute locally
    assert result is None
    
    enqueuer.enqueue_command_mock.assert_awaited_once()
    args, _ = enqueuer.enqueue_command_mock.call_args
    assert args[0] == "DummyTaskCommand"
    assert args[1]["__data__"]["data"] == "hello"
    assert args[2] == "high_priority"


@background_handler(queue="events_queue")
async def dummy_event_handler(event: DummyTaskEvent):
    pass


@pytest.mark.anyio
async def test_smart_routing_event_bus_enqueues_background_handlers():
    enqueuer = MockEnqueuer()
    serializer = PydanticSerializer()
    
    bus = InMemoryEventBus(enqueuer=enqueuer, serializer=serializer)
    bus.subscribe(DummyTaskEvent, dummy_event_handler)
    
    evt = DummyTaskEvent(info="event_info")
    await bus.publish(evt)
    
    enqueuer.enqueue_handler_mock.assert_awaited_once()
    args, _ = enqueuer.enqueue_handler_mock.call_args
    assert "dummy_event_handler" in args[0]
    assert args[1]["__data__"]["info"] == "event_info"
    assert args[2] == "events_queue"


@pytest.mark.anyio
async def test_cqrs_consumer_processes_handler_resolution():
    # En este test simulamos la recepción de un mensaje de "handler individual"
    local_cmd_bus = AsyncMock()
    local_evt_bus = AsyncMock()
    serializer = PydanticSerializer()
    
    consumer = CQRSConsumer(
        command_bus=local_cmd_bus,
        event_bus=local_evt_bus,
        serializer=serializer
    )
    
    evt = DummyTaskEvent(info="consumed_event")
    payload = serializer.serialize(evt)
    
    # Modificamos dummy_event_handler temporalmente para comprobar si fue llamado
    dummy_event_handler_was_called = False
    
    # Necesitamos mockear la funcion porque el import module va a buscar `dummy_event_handler` real
    # Pero el resolve import funciona sobre modulos. Como esto es un script de tests,
    # probaremos usando un modulo global conocido o un mock a _resolve_callable.
    import hexcore.infrastructure.workers.consumer as consumer_module
    
    async def mock_handler(e):
        nonlocal dummy_event_handler_was_called
        dummy_event_handler_was_called = True
    
    consumer_module._resolve_callable = lambda x: mock_handler
    
    await consumer.process_handler("tests.test_smart_routing.dummy_event_handler", payload)
    
    assert dummy_event_handler_was_called is True


@pytest.mark.anyio
async def test_sync_command_executes_locally_without_enqueuer():
    registry = HandlerRegistry()
    serializer = PydanticSerializer()
    
    # Un comando sin el decorador
    class SyncCommand(Command):
        data: str
        
    class SyncCommandHandler:
        async def handle(self, cmd: SyncCommand) -> str:
            return f"processed_{cmd.data}"
            
    registry.register_command_handler(SyncCommand, SyncCommandHandler())
    
    bus = InMemoryCommandBus(registry=registry, enqueuer=None, serializer=serializer)
    
    result = await bus.dispatch(SyncCommand(data="sync"))
    assert result == "processed_sync"


@pytest.mark.anyio
async def test_background_command_raises_if_no_enqueuer():
    registry = HandlerRegistry()
    
    bus = InMemoryCommandBus(registry=registry, enqueuer=None, serializer=None)
    
    cmd = DummyTaskCommand(data="hello")
    with pytest.raises(RuntimeError) as exc:
        await bus.dispatch(cmd)
        
    assert "requiere ejecución en background" in str(exc.value)


@pytest.mark.anyio
async def test_sync_event_handler_executes_locally():
    bus = InMemoryEventBus()
    
    handled = False
    
    async def standard_handler(event: DummyTaskEvent):
        nonlocal handled
        handled = True
        
    bus.subscribe(DummyTaskEvent, standard_handler)
    
    await bus.publish(DummyTaskEvent(info="test"))
    
    assert handled is True


@pytest.mark.anyio
async def test_background_handler_raises_if_no_enqueuer():
    bus = InMemoryEventBus(enqueuer=None, serializer=None)
    
    # dummy_event_handler tiene @background_handler
    bus.subscribe(DummyTaskEvent, dummy_event_handler)
    
    with pytest.raises(RuntimeError) as exc:
        await bus.publish(DummyTaskEvent(info="test"))
        
    assert "requiere ejecución en background" in str(exc.value)


@pytest.mark.anyio
async def test_cqrs_consumer_processes_generic_task():
    local_cmd_bus = AsyncMock()
    local_evt_bus = AsyncMock()
    serializer = PydanticSerializer()
    
    consumer = CQRSConsumer(
        command_bus=local_cmd_bus,
        event_bus=local_evt_bus,
        serializer=serializer
    )
    
    generic_task_was_called = False
    
    import hexcore.infrastructure.workers.consumer as consumer_module
    
    async def mock_task(**kwargs):
        nonlocal generic_task_was_called
        assert kwargs["days"] == 30
        generic_task_was_called = True
    
    consumer_module._resolve_callable = lambda x: mock_task
    
    await consumer.process_task("my_maintenance_task", {"days": 30})
    
    assert generic_task_was_called is True

