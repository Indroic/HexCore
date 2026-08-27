"""
Adaptador de RabbitMQ para el EventBus de HexCore CQRS usando aio-pika.
"""
from __future__ import annotations

import asyncio
import json
import logging
import typing as t

from hexcore.domain.cqrs.buses import AbstractEventBus
from hexcore.domain.cqrs.envelope import restored_envelope_scope
from hexcore.domain.cqrs.middleware import NextHandler
from hexcore.application.cqrs.pipeline import MiddlewarePipeline
from hexcore.domain.cqrs.serializer import AbstractSerializer
from hexcore.domain.events import DomainEvent

if t.TYPE_CHECKING:
    import aio_pika
    from aio_pika.abc import AbstractRobustConnection, AbstractChannel, AbstractExchange


logger = logging.getLogger("hexcore.cqrs.rabbitmq")


class RabbitMQEventBus(AbstractEventBus):
    """
    Implementación del EventBus sobre RabbitMQ (usando aio-pika).
    Permite publicar y consumir eventos de dominio distribuidos de forma asíncrona.
    
    Cada instancia de RabbitMQEventBus típicamente usará su propia cola 
    enlazada a un exchange central de tipo 'topic' (o 'direct').
    """

    def __init__(
        self,
        connection: "AbstractRobustConnection",
        serializer: AbstractSerializer,
        pipeline: MiddlewarePipeline | None = None,
        exchange_name: str = "hexcore.events",
        queue_name: str = "hexcore.worker.queue",
    ) -> None:
        """
        Args:
            connection: Conexión robusta de aio-pika. Debe ser inyectada y gestionada
                        por el ciclo de vida de la aplicación.
            serializer: Implementación de AbstractSerializer (ej: PydanticSerializer).
            pipeline: Pipeline opcional de middlewares.
            exchange_name: Nombre del Topic Exchange para distribuir eventos.
            queue_name: Nombre de la cola (queue) exclusiva para este worker/servicio.
        """
        self._connection = connection
        self._serializer = serializer
        self._pipeline = pipeline or MiddlewarePipeline([])
        
        self._exchange_name = exchange_name
        self._queue_name = queue_name
        
        self._channel: "AbstractChannel" | None = None
        self._exchange: "AbstractExchange" | None = None
        
        # event_name -> list of handlers
        self._handlers: dict[str, list[t.Callable[[DomainEvent], t.Awaitable[None]]]] = {}
        # event_name -> type
        self._event_types: dict[str, type[DomainEvent]] = {}

    async def _setup(self) -> None:
        """Configura el channel y exchange una sola vez (lazy)."""
        if self._channel is None or self._exchange is None:
            import aio_pika
            
            self._channel = await self._connection.channel()
            # Un topic exchange permite suscripciones flexibles mediante routing keys
            self._exchange = await self._channel.declare_exchange(
                self._exchange_name, aio_pika.ExchangeType.TOPIC, durable=True
            )

    async def publish(self, event: DomainEvent) -> None:
        """
        Publica el evento de dominio a RabbitMQ.
        El routing_key es el nombre del evento.
        """
        import aio_pika
        
        await self._setup()
        assert self._exchange is not None
        
        # Serializamos usando el pipeline y serializer configurado
        data = self._serializer.serialize_envelope(event)
        body = json.dumps(data).encode("utf-8")
        
        message = aio_pika.Message(
            body=body,
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            content_type="application/json",
            message_id=str(event.event_id),
        )
        
        routing_key = event.event_name
        
        await self._exchange.publish(message, routing_key=routing_key)
        logger.debug(f"[RabbitMQEventBus] Publicado evento {routing_key}")

    def subscribe(
        self,
        event_type: type[DomainEvent],
        handler: t.Callable[[DomainEvent], t.Awaitable[None]],
    ) -> None:
        """
        Registra localmente un handler para un tipo de evento.
        Nota: Esto solo guarda en memoria. Para empezar a consumir RabbitMQ,
        debes llamar a `start_consuming()`.
        """
        # Obtenemos un event_name estandarizado (usamos una instancia temporal para leer properties o usar una heuristica estática)
        # Por seguridad creamos un string fijo a partir del nombre de clase (como hace DomainEvent.event_name)
        event_name = event_type.__name__.replace("Event", "").upper()
        
        if event_name not in self._handlers:
            self._handlers[event_name] = []
            self._event_types[event_name] = event_type
            
        self._handlers[event_name].append(handler)
        logger.debug(f"[RabbitMQEventBus] Suscrito localmente a {event_name}")

    async def _handle_message(self, message: "aio_pika.abc.AbstractIncomingMessage") -> None:
        """Callback invocado por aio-pika para cada mensaje entrante."""
        async with message.process(requeue=True, ignore_processed=True):
            routing_key = message.routing_key
            if not routing_key:
                return

            # Verificamos si tenemos handlers para este evento
            if routing_key not in self._handlers:
                return

            try:
                # 1. Deserialización
                payload = json.loads(message.body.decode("utf-8"))
                # Nos aseguramos que lleve el __type__ si el serializer lo requiere, 
                # o el serializer sabrá reconstruirlo. Idealmente el serializer CQRS asume dict genérico
                event, metadata = self._serializer.deserialize_envelope(payload)
                
                # Ejecutamos todos los handlers concurrentemente o secuencialmente
                # En un bus robusto, el fallo de un handler no debería ocultar a otros, pero si falla, 
                # el mensaje no se hace acknowledge. Para simplicidad, lanzamos gather:
                handlers = self._handlers[routing_key]
                
                async def _execute_with_pipeline(h: t.Callable[[DomainEvent], t.Awaitable[None]], evt: DomainEvent) -> None:
                    async def base_handler(m: DomainEvent) -> t.Any:
                        await h(m)
                    
                    # Usamos el pipeline del bus
                    await self._pipeline.execute(evt, base_handler)

                # El sobre se restaura una vez para todos los handlers: el evento cruzó un
                # proceso, así que sin esto el handler de este lado no sabe a nombre de quién
                # actúa. Envuelve al `gather` entero, y eso es correcto: los handlers corren
                # concurrentemente pero cada tarea hereda una **copia** del contexto, así que
                # ninguno puede pisarle el contexto a otro.
                async with restored_envelope_scope(metadata, event):
                    await asyncio.gather(*(
                        _execute_with_pipeline(handler, event) for handler in handlers
                    ))
            
            except Exception as exc:
                logger.exception(f"[RabbitMQEventBus] Error procesando mensaje {routing_key}: {exc}")
                # El mensaje será NACKed y requeueado o mandado a Dead Letter (depende del config de aio-pika process)
                raise

    async def start_consuming(self) -> None:
        """
        Conecta a la cola, hace bind a todos los eventos suscritos y comienza
        a consumir permanentemente.
        """
        await self._setup()
        assert self._channel is not None
        assert self._exchange is not None
        
        # 1. Declarar la cola para este worker/microservicio
        queue = await self._channel.declare_queue(self._queue_name, durable=True)
        
        # 2. Hacer bind para cada evento suscrito
        for event_name in self._handlers.keys():
            await queue.bind(self._exchange, routing_key=event_name)
            logger.info(f"[RabbitMQEventBus] Bind queue '{self._queue_name}' to '{event_name}'")
            
        # 3. Empezar a consumir
        await queue.consume(self._handle_message)
        logger.info(f"[RabbitMQEventBus] Escuchando eventos en cola '{self._queue_name}'...")
