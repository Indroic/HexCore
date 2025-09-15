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
        self.events_dispatcher: t.Any = None

    async def __aenter__(self) -> IUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: t.Optional[type],
        exc_val: t.Optional[BaseException],
        exc_tb: t.Optional[t.Any],
    ) -> None:
        await self.rollback()

    @abc.abstractmethod
    def IUnitOfWork(self) -> t.Set[BaseEntity]:
        raise NotImplementedError

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
