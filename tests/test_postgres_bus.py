import json
import pytest
from unittest.mock import AsyncMock, MagicMock, ANY

from hexcore.domain.events import DomainEvent
from hexcore.infrastructure.cqrs.postgres_bus import PostgresEventBus
from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer


class DummyEvent(DomainEvent):
    my_data: str


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_postgres_event_bus_publish():
    pool_mock = MagicMock()
    conn_mock = AsyncMock()
    
    # Mockear el AsyncContextManager devuelto por pool.acquire()
    cm = AsyncMock()
    cm.__aenter__.return_value = conn_mock
    pool_mock.acquire.return_value = cm
    
    serializer = PydanticSerializer()
    
    bus = PostgresEventBus(
        pool=pool_mock,
        serializer=serializer,
        channel_name="my_events"
    )
    
    event = DummyEvent(my_data="pg_rocks")
    await bus.publish(event)
    
    conn_mock.execute.assert_awaited_once()
    args = conn_mock.execute.call_args[0]
    assert args[0] == "SELECT pg_notify($1, $2)"
    assert args[1] == "my_events"
    
    payload_dict = json.loads(args[2])
    assert payload_dict["__data__"]["my_data"] == "pg_rocks"


@pytest.mark.anyio
async def test_postgres_event_bus_handle_notify():
    pool_mock = AsyncMock()
    serializer = PydanticSerializer()
    
    bus = PostgresEventBus(
        pool=pool_mock,
        serializer=serializer,
        channel_name="my_events"
    )
    
    handler = AsyncMock()
    bus.subscribe(DummyEvent, handler)
    
    event = DummyEvent(my_data="test")
    payload = serializer.serialize(event)
    payload_str = json.dumps(payload)
    
    # Procesar directamente (evitamos invocar el asyncio.create_task para testeabilidad plana)
    await bus._process_message(payload_str)
    
    handler.assert_awaited_once()
    passed_event = handler.call_args[0][0]
    assert isinstance(passed_event, DummyEvent)
    assert passed_event.my_data == "test"
