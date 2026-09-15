"""
Contrato para middlewares del pipeline CQRS.
"""
from __future__ import annotations

import abc
import typing as t


# Tipo callable para el siguiente middleware/handler en la cadena
NextHandler = t.Callable[[t.Any], t.Awaitable[t.Any]]


class AbstractMiddleware(abc.ABC):
    """
    Middleware genérico para interceptar la ejecución de Commands, Queries o Events.

    Sigue el patrón Chain of Responsibility. Cada middleware recibe el mensaje
    y una función ``next_handler`` que invoca el siguiente paso de la cadena.

    Ejemplo::

        class LoggingMiddleware(AbstractMiddleware):
            async def handle(self, message, next_handler):
                print(f"Processing: {message}")
                result = await next_handler(message)
                print(f"Done: {message}")
                return result
    """

    @abc.abstractmethod
    async def handle(
        self,
        message: t.Any,
        next_handler: NextHandler,
    ) -> t.Any:
        """
        Procesa el mensaje y delega al siguiente handler en la cadena.

        Args:
            message: El Command, Query o Event siendo procesado.
            next_handler: Callable que invoca el siguiente middleware o el handler final.

        Returns:
            El resultado de la ejecución (puede ser None para commands/events).
        """
        raise NotImplementedError
