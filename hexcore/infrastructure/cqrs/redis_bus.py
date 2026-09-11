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

from hexcore.domain.cqrs.buses import AbstractEventBus
from hexcore.domain.cqrs.context import is_worker_execution, local_execution
from hexcore.domain.cqrs.envelope import restored_envelope_scope
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer
from hexcore.domain.cqrs.dispatch import handlers_for
from hexcore.domain.cqrs.resolution import resolve_dotted
from hexcore.domain.events import DomainEvent

if t.TYPE_CHECKING:
    from redis.asyncio import Redis
    from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer


logger = logging.getLogger(__name__)


class RedisEventBus(AbstractEventBus):
    """
    Implementación de AbstractEventBus usando Redis Streams.
    """

    def __init__(
        self,
        redis_client: "Redis",
        serializer: "PydanticSerializer",
        stream_name: str,
        group_name: str,
        consumer_name: str | None = None,
        enqueuer: ITaskEnqueuer | None = None,
        resolve_unknown_events: bool = True,
    ) -> None:
        """
        Args:
            resolve_unknown_events: Si un evento cuyo tipo no está en el registro de
                suscripciones se intenta importar por su FQN. Es lo que permite que un
                handler suscrito a una clase base reciba subclases que este proceso nunca
                registró — sin esto, un catch-all no recibiría nada. `False` recupera la
                semántica anterior a 9.0: se descarta lo que no esté registrado.
        """
        self.redis = redis_client
        self._serializer = serializer
        self.stream_name = stream_name
        self.group_name = group_name
        self.consumer_name = consumer_name or f"consumer-{uuid.uuid4()}"
        self.enqueuer = enqueuer
        
        self._handlers: dict[type[DomainEvent], list[t.Callable[..., t.Any]]] = {}
        self._event_types_by_name: dict[str, type[DomainEvent]] = {}
        self._stop_event = asyncio.Event()
        self._resolve_unknown_events = resolve_unknown_events

    def subscribe(self, event_type: type[DomainEvent], handler: t.Callable[..., t.Any]) -> None:
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)
        
        fqn = f"{event_type.__module__}.{event_type.__qualname__}"
        self._event_types_by_name[fqn] = event_type

    async def publish(self, event: DomainEvent) -> None:
        payload = self._serializer.serialize_envelope(event)
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
                # El nombre del stream no se usa: cada consumidor escucha uno solo, así
                # que el mensaje ya se sabe de dónde viene.
                for _stream, stream_messages in messages:
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

            event_type = self._resolver_tipo(event_name)
            if event_type is None:
                # Ni suscrito ni importable: no hay forma de reconstruirlo. Se confirma para
                # que no quede atascado en el PEL, y se loguea — antes se descartaba callado.
                logger.warning(
                    "Evento '%s' descartado: no hay ningun handler suscrito y el tipo no se "
                    "pudo importar en este proceso.",
                    event_name,
                )
                await self.redis.xack(self.stream_name, self.group_name, message_id)
                return

            event, metadata = self._serializer.deserialize_envelope(payload_dict)
            handlers = handlers_for(event, self._handlers)
            if not handlers:
                await self.redis.xack(self.stream_name, self.group_name, message_id)
                return
            
            # Ejecutar handlers (respetando Smart Routing).
            # Si ya estamos dentro de un worker, el mensaje viene de la cola:
            # ejecutar en vez de reencolar (ver hexcore.domain.cqrs.context).
            in_worker = is_worker_execution()
            # El sobre se restaura una vez para todos los handlers locales: el evento cruzó un
            # proceso, así que sin esto el handler de este lado no sabe a nombre de quién
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
            # **Sin `xack`.** El mensaje se queda en el PEL (Pending Entries List) del grupo,
            # que es donde tiene que estar un mensaje que no se proceso: desde ahi se puede
            # reclamar con `XAUTOCLAIM` y reintentar. Antes el `xack` estaba dentro del `try`
            # y se ejecutaba igual cuando un handler fallaba, asi que el mensaje salia del PEL
            # y se perdia sin dejar rastro mas alla de una linea de log.
            logger.error(
                f"Error parseando o ejecutando evento de Redis (ID {message_id!r}): {e}. "
                "El mensaje queda pendiente en el grupo para poder reclamarlo."
            )
            return

        # Confirmar recien despues de que todos los handlers locales terminaron bien.
        await self.redis.xack(self.stream_name, self.group_name, message_id)

    async def stop(self) -> None:
        self._stop_event.set()

    def _resolver_tipo(self, event_name: str | None) -> type | None:
        """
        El tipo del evento: primero el registro de suscripciones, y si no está, el FQN.

        El registro sólo se puebla en `subscribe()`, así que un evento cuyo tipo concreto
        nadie registró no estaba ahí y se descartaba. Con el despacho por jerarquía eso pasa
        a ser el caso **normal**: quien se suscribe a una clase base —o a `DomainEvent`, como
        hace el relay del event store— nunca registra los tipos concretos, así que su
        catch-all no recibiría nada.

        Resolver el FQN es lo que ya hace `PydanticSerializer.deserialize`, así que no agrega
        ninguna capacidad nueva: sólo evita descartar antes de intentarlo.
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
