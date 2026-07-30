"""
Event Bus basado en PostgreSQL LISTEN / NOTIFY.
Ligero, rápido y no requiere infraestructura adicional si ya usas PostgreSQL.
"""
from __future__ import annotations

import asyncio
import json
import logging
import typing as t

from hexcore.domain.cqrs.buses import AbstractEventBus
from hexcore.domain.cqrs.context import is_worker_execution, local_execution
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer
from hexcore.domain.events import DomainEvent

if t.TYPE_CHECKING:
    from asyncpg import Pool, Connection
    from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer


logger = logging.getLogger(__name__)


class PostgresEventBus(AbstractEventBus):
    """
    Implementación de AbstractEventBus utilizando PostgreSQL LISTEN/NOTIFY.
    Nota: LISTEN/NOTIFY no persiste los eventos una vez entregados. 
    Ideal para eventos efímeros o arquitecturas más simples.
    """

    def __init__(
        self,
        pool: "Pool",
        serializer: "PydanticSerializer",
        channel_name: str,
        enqueuer: ITaskEnqueuer | None = None,
    ) -> None:
        self.pool = pool
        self._serializer = serializer
        self.channel_name = channel_name
        self.enqueuer = enqueuer
        
        self._handlers: dict[type[DomainEvent], list[t.Callable[..., t.Any]]] = {}
        self._event_types_by_name: dict[str, type[DomainEvent]] = {}
        self._stop_event = asyncio.Event()
        self._listener_connection: "Connection | None" = None

    def subscribe(self, event_type: type[DomainEvent], handler: t.Callable[..., t.Any]) -> None:
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        
        fqn = f"{event_type.__module__}.{event_type.__qualname__}"
        self._event_types_by_name[fqn] = event_type

    async def publish(self, event: DomainEvent) -> None:
        payload = self._serializer.serialize(event)
        json_payload = json.dumps(payload)
        
        async with self.pool.acquire() as conn:
            # En PostgreSQL, pg_notify recibe el canal y el payload en texto.
            await conn.execute("SELECT pg_notify($1, $2)", self.channel_name, json_payload)

    async def start_consuming(self) -> None:
        """
        Inicia el consumo adquiriendo una conexión dedicada y escuchando el canal.
        """
        self._stop_event.clear()
        
        # Adquirir una conexión persistente para el listener
        self._listener_connection = await self.pool.acquire()
        try:
            await self._listener_connection.add_listener(self.channel_name, self._handle_notify)
            logger.info(f"[*] PostgresEventBus listening on channel '{self.channel_name}'")
            
            # Bloquear hasta que alguien llame a stop()
            await self._stop_event.wait()
            
        except asyncio.CancelledError:
            pass
        finally:
            if self._listener_connection:
                try:
                    await self._listener_connection.remove_listener(self.channel_name, self._handle_notify)
                except Exception as e:
                    logger.warning(f"Error removing postgres listener: {e}")
                await self.pool.release(self._listener_connection)
                self._listener_connection = None

    def _handle_notify(self, connection: "Connection", pid: int, channel: str, payload: str) -> None:
        """
        Callback invocada por asyncpg cuando recibe un NOTIFY.
        Dado que es síncrona/concurrente desde el driver, delegamos la carga real
        a un asyncio.create_task para evitar bloquear la conexión.
        """
        asyncio.create_task(self._process_message(payload))

    async def _process_message(self, json_payload: str) -> None:
        try:
            payload_dict = json.loads(json_payload)
            event_name = payload_dict.get("__type__")
            
            event_type = self._event_types_by_name.get(event_name)
            if not event_type:
                return

            event = self._serializer.deserialize(payload_dict)
            handlers = self._handlers.get(event_type, [])
            
            # Si ya estamos dentro de un worker, el mensaje viene de la cola:
            # ejecutar en vez de reencolar (ver hexcore.domain.cqrs.context).
            in_worker = is_worker_execution()
            for handler in handlers:
                is_background = (
                    getattr(handler, "__cqrs_background_handler__", False)
                    and not in_worker
                )
                if is_background:
                    queue_name = getattr(handler, "__cqrs_queue__", "default")
                    if not self.enqueuer:
                        raise RuntimeError(f"El handler asíncrono {handler.__name__} requiere un enqueuer.")

                    handler_ref = getattr(handler, "__cqrs_handler_name__", f"{handler.__module__}.{handler.__name__}")
                    await self.enqueuer.enqueue_handler(handler_ref, payload_dict, queue_name)
                else:
                    with local_execution():
                        await handler(event)
                    
        except Exception as e:
            logger.error(f"Error parseando o ejecutando evento de PostgreSQL NOTIFY: {e}")

    async def stop(self) -> None:
        self._stop_event.set()
