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
from hexcore.domain.cqrs.envelope import restored_envelope_scope
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer
from hexcore.domain.cqrs.dispatch import handlers_for
from hexcore.domain.cqrs.resolution import resolve_dotted
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
        resolve_unknown_events: bool = True,
    ) -> None:
        """
        Args:
            resolve_unknown_events: Si un evento cuyo tipo no esta en el registro de
                suscripciones se intenta importar por su FQN. Es lo que permite que un
                handler suscrito a una clase base reciba subclases que este proceso nunca
                registro. `False` recupera la semantica anterior a 9.0.
        """
        self.pool = pool
        self._serializer = serializer
        self.channel_name = channel_name
        self.enqueuer = enqueuer
        
        self._handlers: dict[type[DomainEvent], list[t.Callable[..., t.Any]]] = {}
        self._event_types_by_name: dict[str, type[DomainEvent]] = {}
        self._stop_event = asyncio.Event()
        self._listener_connection: "Connection | None" = None
        self._resolve_unknown_events = resolve_unknown_events

    def subscribe(self, event_type: type[DomainEvent], handler: t.Callable[..., t.Any]) -> None:
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        
        fqn = f"{event_type.__module__}.{event_type.__qualname__}"
        self._event_types_by_name[fqn] = event_type

    async def publish(self, event: DomainEvent) -> None:
        payload = self._serializer.serialize_envelope(event)
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

            event_type = self._resolver_tipo(event_name)
            if event_type is None:
                logger.warning(
                    "Evento '%s' descartado: no hay ningun handler suscrito y el tipo no se "
                    "pudo importar en este proceso.",
                    event_name,
                )
                return

            event, metadata = self._serializer.deserialize_envelope(payload_dict)
            # Por jerarquia: un handler suscrito a una clase base recibe sus subclases. Antes
            # era por clase exacta y el handler quedaba registrado sin invocarse nunca.
            handlers = handlers_for(event, self._handlers)
            if not handlers:
                return
            
            # Si ya estamos dentro de un worker, el mensaje viene de la cola:
            # ejecutar en vez de reencolar (ver hexcore.domain.cqrs.context).
            in_worker = is_worker_execution()
            # El sobre se restaura una vez para todos los handlers locales: el evento cruzó
            # un proceso, así que sin esto el handler de este lado no sabe a nombre de quién
            # actúa. El payload que se reencola sigue llevando el sobre intacto.
            async with restored_envelope_scope(metadata, event):
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

    def _resolver_tipo(self, event_name: str | None) -> type | None:
        """
        El tipo del evento: primero el registro de suscripciones, y si no esta, el FQN.

        Mismo motivo que en `RedisEventBus`: el registro solo se puebla en `subscribe()`, asi
        que con despacho por jerarquia el caso normal pasa a ser que el tipo concreto no este
        ahi -- quien se suscribe a una clase base nunca lo registra.
        """
        if not event_name:
            return None

        registrado = self._event_types_by_name.get(event_name)
        if registrado is not None:
            return registrado

        if not self._resolve_unknown_events:
            return None

        try:
            resuelto = resolve_dotted(event_name)
        except LookupError:
            return None
        return resuelto if isinstance(resuelto, type) else None
