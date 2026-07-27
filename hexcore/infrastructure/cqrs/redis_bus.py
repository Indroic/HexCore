"""
Event Bus basado en Redis Streams.
Soporta alta concurrencia mediante Consumer Groups.
"""
from __future__ import annotations

import asyncio
import json
import logging
import typing as t
import uuid

from hexcore.domain.cqrs.buses import IEventBus
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer
from hexcore.domain.events import DomainEvent

if t.TYPE_CHECKING:
    from redis.asyncio import Redis
    from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer


logger = logging.getLogger(__name__)


class RedisEventBus(IEventBus):
    """
    Implementación de IEventBus usando Redis Streams.
    """

    def __init__(
        self,
        redis_client: "Redis",
        serializer: "PydanticSerializer",
        stream_name: str,
        group_name: str,
        consumer_name: str | None = None,
        enqueuer: ITaskEnqueuer | None = None,
    ) -> None:
        self.redis = redis_client
        self._serializer = serializer
        self.stream_name = stream_name
        self.group_name = group_name
        self.consumer_name = consumer_name or f"consumer-{uuid.uuid4()}"
        self.enqueuer = enqueuer
        
        self._handlers: dict[type[DomainEvent], list[t.Callable[..., t.Any]]] = {}
        self._event_types_by_name: dict[str, type[DomainEvent]] = {}
        self._stop_event = asyncio.Event()

    def subscribe(self, event_type: type[DomainEvent], handler: t.Callable[..., t.Any]) -> None:
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        
        fqn = f"{event_type.__module__}.{event_type.__qualname__}"
        self._event_types_by_name[fqn] = event_type

    async def publish(self, event: DomainEvent) -> None:
        payload = self._serializer.serialize(event)
        # Redis streams accepts dicts with string keys and string values
        json_payload = json.dumps(payload)
        await self.redis.xadd(self.stream_name, {"payload": json_payload})

    async def start_consuming(self) -> None:
        """
        Inicia el consumo utilizando Grupos de Consumidores de Redis.
        """
        import redis.exceptions

        # Asegurar que el grupo existe, creándolo si no existe (con MKSTREAM)
        try:
            await self.redis.xgroup_create(self.stream_name, self.group_name, id="$", mkstream=True)
        except redis.exceptions.ResponseError as e:
            if "BUSYGROUP Consumer Group name already exists" not in str(e):
                raise e

        logger.info(f"[*] RedisEventBus consuming from stream '{self.stream_name}' (group: {self.group_name}, consumer: {self.consumer_name})")
        self._stop_event.clear()

        while not self._stop_event.is_set():
            try:
                # Bloquea hasta 1000ms esperando mensajes no leídos ('>')
                messages = await self.redis.xreadgroup(
                    groupname=self.group_name,
                    consumername=self.consumer_name,
                    streams={self.stream_name: ">"},
                    count=10,
                    block=1000,
                )
                
                if not messages:
                    continue
                
                # messages: [[b'stream_name', [(b'message_id', {b'payload': b'...'}), ...]]]
                for stream, stream_messages in messages:
                    for message_id, message_data in stream_messages:
                        await self._handle_message(message_id, message_data)
                        
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error procesando mensaje de Redis: {e}")
                await asyncio.sleep(1)

    async def _handle_message(self, message_id: bytes, message_data: dict[bytes, bytes]) -> None:
        try:
            payload_bytes = message_data.get(b"payload")
            if not payload_bytes:
                return

            payload_dict = json.loads(payload_bytes)
            event_name = payload_dict.get("__type__")
            
            event_type = self._event_types_by_name.get(event_name)
            if not event_type:
                # Evento no registrado localmente, lo saltamos y confirmamos
                await self.redis.xack(self.stream_name, self.group_name, message_id)
                return

            event = self._serializer.deserialize(payload_dict)
            handlers = self._handlers.get(event_type, [])
            
            # Ejecutar handlers (respetando Smart Routing)
            for handler in handlers:
                is_background = getattr(handler, "__cqrs_background_handler__", False)
                if is_background:
                    queue_name = getattr(handler, "__cqrs_queue__", "default")
                    if not self.enqueuer:
                        raise RuntimeError(f"El handler asíncrono {handler.__name__} requiere un enqueuer.")
                    
                    handler_ref = getattr(handler, "__cqrs_handler_name__", f"{handler.__module__}.{handler.__name__}")
                    await self.enqueuer.enqueue_handler(handler_ref, payload_dict, queue_name)
                else:
                    await handler(event)

            # Confirmar mensaje
            await self.redis.xack(self.stream_name, self.group_name, message_id)
            
        except Exception as e:
            logger.error(f"Error parseando o ejecutando evento de Redis (ID {message_id}): {e}")

    async def stop(self) -> None:
        self._stop_event.set()
