"""
Pipeline de middlewares para CQRS.
Construye la cadena de responsabilidad de forma lazy y ejecuta el handler final.
"""
from __future__ import annotations

import typing as t

from hexcore.domain.cqrs.middleware import IMiddleware, NextHandler


class MiddlewarePipeline:
    """
    Construye y ejecuta una cadena de middlewares.

    Pipeline: [MW1] → [MW2] → ... → [MWn] → [handler.handle]
    """

    def __init__(self, middlewares: t.Sequence[IMiddleware] | None = None) -> None:
        self._middlewares: list[IMiddleware] = list(middlewares or [])

    def add(self, middleware: IMiddleware) -> "MiddlewarePipeline":
        """Añade un middleware al pipeline. Retorna self para fluent API."""
        self._middlewares.append(middleware)
        return self

    def add_many(self, middlewares: t.Iterable[IMiddleware]) -> "MiddlewarePipeline":
        """Añade múltiples middlewares al pipeline."""
        self._middlewares.extend(middlewares)
        return self

    async def execute(
        self,
        message: t.Any,
        final_handler: NextHandler,
    ) -> t.Any:
        """
        Ejecuta el pipeline completo.

        Construye la cadena empezando por el handler final y envolviendo
        cada middleware de derecha a izquierda (el primero en la lista
        es el más externo).
        """
        chain: NextHandler = final_handler

        for middleware in reversed(self._middlewares):
            chain = self._wrap(middleware, chain)

        return await chain(message)

    @staticmethod
    def _wrap(middleware: IMiddleware, next_handler: NextHandler) -> NextHandler:
        """Envuelve un middleware y su next_handler en un callable."""

        async def wrapped(message: t.Any) -> t.Any:
            return await middleware.handle(message, next_handler)

        return wrapped
