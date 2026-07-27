"""
Implementaciones In-Memory de los buses CQRS.
Funcionan out-of-the-box sin dependencias de infraestructura.
"""
from __future__ import annotations

import typing as t

from hexcore.domain.cqrs.buses import ICommandBus, IQueryBus, IEventBus
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.queries import Query
from hexcore.domain.events import DomainEvent

from .registry import HandlerRegistry
from .pipeline import MiddlewarePipeline


class InMemoryCommandBus(ICommandBus):
    """
    Bus de commands síncrono en memoria.
    Resuelve el handler desde el registry, ejecuta el pipeline de middlewares
    y retorna el resultado directamente.
    """

    def __init__(
        self,
        registry: HandlerRegistry,
        pipeline: MiddlewarePipeline | None = None,
    ) -> None:
        self._registry = registry
        self._pipeline = pipeline or MiddlewarePipeline()

    async def dispatch(self, command: Command) -> t.Any:
        handler = self._registry.resolve_command_handler(type(command))

        async def final_handler(cmd: t.Any) -> t.Any:
            return await handler.handle(cmd)

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
    Bus de eventos en memoria. Compatible con el IEventDispatcher existente.
    """

    def __init__(
        self,
        pipeline: MiddlewarePipeline | None = None,
    ) -> None:
        self._handlers: dict[
            type[DomainEvent],
            list[t.Callable[[DomainEvent], t.Awaitable[None]]],
        ] = {}
        self._pipeline = pipeline or MiddlewarePipeline()

    def subscribe(
        self,
        event_type: type[DomainEvent],
        handler: t.Callable[[DomainEvent], t.Awaitable[None]],
    ) -> None:
        self._handlers.setdefault(event_type, []).append(handler)

    async def publish(self, event: DomainEvent) -> None:
        handlers = self._handlers.get(type(event), [])

        for event_handler in handlers:

            async def final_handler(
                evt: t.Any,
                _h: t.Callable[..., t.Awaitable[None]] = event_handler,
            ) -> None:
                await _h(evt)

            await self._pipeline.execute(event, final_handler)
