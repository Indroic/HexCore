"""
Implementaciones In-Memory de los buses CQRS con soporte para Enrutamiento Inteligente (Smart Routing).
"""
from __future__ import annotations

import typing as t
import logging

from hexcore.domain.cqrs.buses import ICommandBus, IQueryBus, IEventBus
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.context import is_worker_execution, local_execution
from hexcore.domain.cqrs.queries import Query
from hexcore.domain.events import DomainEvent
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer
from hexcore.domain.cqrs.serializer import ISerializer

from .registry import HandlerRegistry
from .pipeline import MiddlewarePipeline

logger = logging.getLogger("hexcore.cqrs.buses")


class InMemoryCommandBus(ICommandBus):
    """
    Bus de commands síncrono en memoria con Smart Routing.
    Resuelve el handler desde el registry, ejecuta el pipeline de middlewares
    y retorna el resultado.
    
    Si el comando está decorado con `@background_command` y se provee un `enqueuer`,
    el comando es automáticamente encolado para su ejecución en segundo plano
    sin bloquear el proceso actual (retornando None).

    Cuando el mensaje viene de un worker (``IN_WORKER`` activo, lo pone el
    ``CQRSConsumer``), el bus lo ejecuta **localmente** en vez de reencolarlo.
    Esto permite usar el mismo bus en el proceso web y en el worker.
    """

    def __init__(
        self,
        registry: HandlerRegistry,
        pipeline: MiddlewarePipeline | None = None,
        enqueuer: ITaskEnqueuer | None = None,
        serializer: ISerializer | None = None,
    ) -> None:
        self._registry = registry
        self._pipeline = pipeline or MiddlewarePipeline()
        self._enqueuer = enqueuer
        self._serializer = serializer

    async def dispatch(self, command: Command) -> t.Any:
        cmd_type = type(command)

        # 1. Smart Routing: ¿Debe irse a background?
        # Si ya estamos dentro de un worker, el mensaje viene de la cola: hay que
        # ejecutarlo, no volver a encolarlo.
        is_background = getattr(cmd_type, "__cqrs_background__", False)

        if is_background and not is_worker_execution():
            if not self._enqueuer or not self._serializer:
                raise RuntimeError(
                    f"El comando '{cmd_type.__name__}' requiere ejecución en background, "
                    "pero el InMemoryCommandBus no tiene configurado un 'enqueuer' o 'serializer'."
                )

            async def background_dispatcher(cmd: Command) -> None:
                queue_name = getattr(cmd_type, "__cqrs_queue__", "default")
                payload = self._serializer.serialize(cmd) # type: ignore
                await self._enqueuer.enqueue_command(cmd_type.__name__, payload, queue=queue_name) # type: ignore
                logger.debug("[SmartRouting] Comando %s enrutado a background (queue=%s)", cmd_type.__name__, queue_name)

            await self._pipeline.execute(command, background_dispatcher)
            return None

        # 2. Ejecución Local Estándar
        handler = self._registry.resolve_command_handler(cmd_type)

        async def final_handler(cmd: t.Any) -> t.Any:
            return await handler.handle(cmd)

        # `local_execution` consume el flag de worker: si el handler despacha otro
        # `@background_command`, ese sí debe encolarse.
        with local_execution():
            return await self._pipeline.execute(command, final_handler)


class InMemoryQueryBus(IQueryBus):
    """
    Bus de queries síncrono en memoria.
    Las queries NUNCA pasan por buses asíncronos
    (principio CQRS: las lecturas son siempre síncronas).
    """

    def __init__(
        self,
        registry: HandlerRegistry,
        pipeline: MiddlewarePipeline | None = None,
    ) -> None:
        self._registry = registry
        self._pipeline = pipeline or MiddlewarePipeline()

    async def ask(self, query: Query[t.Any]) -> t.Any:
        handler = self._registry.resolve_query_handler(type(query))

        async def final_handler(q: t.Any) -> t.Any:
            return await handler.handle(q)

        return await self._pipeline.execute(query, final_handler)


class InMemoryEventBus(IEventBus):
    """
    Bus de eventos en memoria con Smart Routing para Handlers Asíncronos.
    
    Permite publicar eventos a todos los suscriptores. Si un suscriptor
    fue decorado con `@background_handler`, se enviará un mensaje al Task Queue
    para que *solo* ese handler se ejecute en background para este evento.
    """

    def __init__(
        self,
        pipeline: MiddlewarePipeline | None = None,
        enqueuer: ITaskEnqueuer | None = None,
        serializer: ISerializer | None = None,
    ) -> None:
        self._handlers: dict[
            type[DomainEvent],
            list[t.Callable[[DomainEvent], t.Awaitable[None]]],
        ] = {}
        self._pipeline = pipeline or MiddlewarePipeline()
        self._enqueuer = enqueuer
        self._serializer = serializer

    def subscribe(
        self,
        event_type: type[DomainEvent],
        handler: t.Callable[[DomainEvent], t.Awaitable[None]],
    ) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def publish(self, event: DomainEvent) -> None:
        handlers = self._handlers.get(type(event), [])
        # Si el evento viene de un worker, sus handlers de background se ejecutan
        # aquí; reencolarlos sería un bucle infinito.
        in_worker = is_worker_execution()

        for event_handler in handlers:
            is_background = (
                getattr(event_handler, "__cqrs_background_handler__", False)
                and not in_worker
            )

            if is_background:
                # Enrutamiento hacia background
                if not self._enqueuer or not self._serializer:
                    raise RuntimeError(
                        f"El handler de eventos '{event_handler.__name__}' requiere ejecución en background, "
                        "pero el InMemoryEventBus no tiene configurado un 'enqueuer' o 'serializer'."
                    )
                
                async def background_dispatcher(
                    evt: t.Any,
                    _h: t.Callable[..., t.Awaitable[None]] = event_handler,
                ) -> None:
                    handler_name = getattr(_h, "__cqrs_handler_name__")
                    queue_name = getattr(_h, "__cqrs_queue__", "default")
                    payload = self._serializer.serialize(evt) # type: ignore
                    await self._enqueuer.enqueue_handler(handler_name, payload, queue=queue_name) # type: ignore
                    logger.debug("[SmartRouting] EventHandler %s enrutado a background (queue=%s)", handler_name, queue_name)
                    
                await self._pipeline.execute(event, background_dispatcher)
                
            else:
                # Ejecución local síncrona
                async def final_handler(
                    evt: t.Any,
                    _h: t.Callable[..., t.Awaitable[None]] = event_handler,
                ) -> None:
                    await _h(evt)

                with local_execution():
                    await self._pipeline.execute(event, final_handler)
