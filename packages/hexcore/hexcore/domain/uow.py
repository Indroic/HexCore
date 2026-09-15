from __future__ import annotations
import abc
import typing as t
from hexcore.domain.base import BaseEntity

T = t.TypeVar("T", bound=BaseEntity)


class IUnitOfWork(abc.ABC):
    """
    Interfaz para la Unidad de Trabajo. Define un contexto transaccional.
    """

    def __init__(self):
        self.repositories: t.Dict[str, t.Any] = {}
        self.event_bus: t.Any = None
        #: `AbstractEventStore | None`. Si está, los eventos de dominio se persisten en la
        #: **misma transacción** que el cambio que los produjo, antes de publicarse.
        #:
        #: Es un atributo y no un `@abc.abstractmethod` a propósito: agregar un método
        #: abstracto al puerto rompe a todo consumidor que tenga un UoW propio, y no compra
        #: nada — un UoW que no sepa del event store simplemente lo deja en `None`.
        self.event_store: t.Any = None

    async def __aenter__(self) -> IUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: t.Optional[type],
        exc_val: t.Optional[BaseException],
        exc_tb: t.Optional[t.Any],
    ) -> None:
        if exc_type:
            await self.rollback()

    @abc.abstractmethod
    def collect_domain_events(self) -> t.List[t.Any]:
        raise NotImplementedError

    @abc.abstractmethod
    async def dispatch_events(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def clear_tracked_entities(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def commit(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def rollback(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def collect_entity(self, entity: BaseEntity) -> None:
        raise NotImplementedError
