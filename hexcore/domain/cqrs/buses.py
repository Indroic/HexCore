"""
Contratos (puertos) para los Buses de CQRS.
"""
from __future__ import annotations

import abc
import typing as t

from hexcore.domain.events import DomainEvent

if t.TYPE_CHECKING:
    from .commands import Command
    from .queries import Query

TResult = t.TypeVar("TResult")


class AbstractCommandBus(abc.ABC):
    """
    Puerto para despachar Commands al Handler correspondiente.
    El bus es responsable de:

    1. Resolver el handler apropiado
    2. Ejecutar la cadena de middlewares
    3. Delegar al handler
    """

    @abc.abstractmethod
    async def dispatch(self, command: "Command") -> t.Any:
        """
        Despacha un command al handler registrado.

        Raises:
            HandlerNotFoundError: Si no hay handler registrado para este tipo de command.
        """
        raise NotImplementedError


class AbstractQueryBus(abc.ABC):
    """
    Puerto para despachar Queries al Handler correspondiente.
    """

    @abc.abstractmethod
    async def ask(self, query: "Query[TResult]") -> TResult:
        """
        Despacha una query al handler registrado y retorna el resultado.

        Raises:
            HandlerNotFoundError: Si no hay handler registrado para este tipo de query.
        """
        raise NotImplementedError


class AbstractEventBus(abc.ABC):
    """
    Puerto para publicar y suscribir eventos de dominio.
    Reemplaza al antiguo IEventDispatcher con una API simplificada
    y soporte para middlewares.
    """

    @abc.abstractmethod
    async def publish(self, event: DomainEvent) -> None:
        """Publica un evento a todos los handlers suscritos."""
        raise NotImplementedError

    @abc.abstractmethod
    def subscribe(
        self,
        event_type: type[DomainEvent],
        handler: t.Callable[[DomainEvent], t.Awaitable[None]],
    ) -> None:
        """Registra un handler para un tipo de evento."""
        raise NotImplementedError


# Aliases de retrocompatibilidad
ICommandBus = AbstractCommandBus
IQueryBus = AbstractQueryBus
IEventBus = AbstractEventBus
