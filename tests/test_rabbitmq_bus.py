import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, ANY
from uuid import uuid4

from hexcore.domain.events import DomainEvent
from hexcore.infrastructure.cqrs.rabbitmq import RabbitMQEventBus
from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer


class DummyEvent(DomainEvent):
    my_data: str


@pytest.fixture
def aio_pika_mocks():
    """Retorna mocks para conexión, canal y exchange."""
    connection = AsyncMock()
    channel = AsyncMock()
    exchange = AsyncMock()
    queue = AsyncMock()
    
    connection.channel.return_value = channel
    channel.declare_exchange.return_value = exchange
    channel.declare_queue.return_value = queue
    
    return connection, channel, exchange, queue


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_rabbitmq_event_bus_publish(aio_pika_mocks):
    connection, channel, exchange, queue = aio_pika_mocks
    
    bus = RabbitMQEventBus(
        connection=connection,
        serializer=PydanticSerializer(),
        exchange_name="test.events"
    )
    
    event = DummyEvent(my_data="hello")
    await bus.publish(event)
    
    # Verifica que se creó el canal y el exchange (lazy setup)
    connection.channel.assert_awaited_once()
    channel.declare_exchange.assert_awaited_once_with(
        "test.events", 
        ANY, 
        durable=True
    )
    
    # Verifica que se publicó en el exchange
    exchange.publish.assert_awaited_once()
    call_args = exchange.publish.call_args[0]
    message = call_args[0]
    
    payload = json.loads(message.body.decode("utf-8"))
    assert payload["__data__"]["my_data"] == "hello"
    
    # Routing key
    kwargs = exchange.publish.call_args[1]
    assert kwargs["routing_key"] == "DUMMY"


@pytest.mark.anyio
async def test_rabbitmq_event_bus_start_consuming(aio_pika_mocks):
    connection, channel, exchange, queue = aio_pika_mocks
    
    bus = RabbitMQEventBus(
        connection=connection,
        serializer=PydanticSerializer(),
        queue_name="test.queue"
    )
    
    handler = AsyncMock()
    bus.subscribe(DummyEvent, handler)
    
    await bus.start_consuming()
    
    # Debe haber hecho bind
    queue.bind.assert_awaited_once_with(exchange, routing_key="DUMMY")
    
    # Debe haber iniciado a consumir
    queue.consume.assert_awaited_once_with(bus._handle_message)
