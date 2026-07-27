import json
import pytest
from unittest.mock import AsyncMock, ANY

from hexcore.domain.events import DomainEvent
from hexcore.infrastructure.cqrs.redis_bus import RedisEventBus
from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer


class DummyEvent(DomainEvent):
    my_data: str


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_redis_event_bus_publish():
    redis_mock = AsyncMock()
    serializer = PydanticSerializer()
    
    bus = RedisEventBus(
        redis_client=redis_mock,
        serializer=serializer,
        stream_name="mystream",
        group_name="mygroup"
    )
    
    event = DummyEvent(my_data="redis_rocks")
    await bus.publish(event)
    
    redis_mock.xadd.assert_awaited_once()
    args, _ = redis_mock.xadd.call_args
    assert args[0] == "mystream"
    
    payload_str = args[1]["payload"]
    payload_dict = json.loads(payload_str)
    
    assert payload_dict["__data__"]["my_data"] == "redis_rocks"


@pytest.mark.anyio
async def test_redis_event_bus_handle_message():
    redis_mock = AsyncMock()
    serializer = PydanticSerializer()
    
    bus = RedisEventBus(
        redis_client=redis_mock,
        serializer=serializer,
        stream_name="mystream",
        group_name="mygroup"
    )
    
    handler = AsyncMock()
    bus.subscribe(DummyEvent, handler)
    
    event = DummyEvent(my_data="test")
    payload = serializer.serialize(event)
    payload_bytes = json.dumps(payload).encode("utf-8")
    
    # Simular que recibimos un mensaje de XREADGROUP
    message_data = {b"payload": payload_bytes}
    
    await bus._handle_message(b"1234-0", message_data)
    
    # El handler local debe ser invocado
    handler.assert_awaited_once()
    passed_event = handler.call_args[0][0]
    assert isinstance(passed_event, DummyEvent)
    assert passed_event.my_data == "test"
    
    # El mensaje debió confirmarse con XACK
    redis_mock.xack.assert_awaited_once_with("mystream", "mygroup", b"1234-0")
