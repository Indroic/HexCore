from __future__ import annotations
import abc
import typing as t
from uuid import UUID
from hexcore.domain.base import BaseEntity
from hexcore.domain.exceptions import InactiveEntityException
from .uow import IUnitOfWork

T = t.TypeVar("T", bound=BaseEntity)


class IBaseRepository(abc.ABC, t.Generic[T]):
    """
    Interfaz genérica para un repositorio base.
    Define las operaciones CRUD comunes que todos los repositorios deben implementar.
    """

    def __init__(self, uow: IUnitOfWork):
        self.uow: IUnitOfWork = uow

    @abc.abstractmethod
    async def get_by_id(self, entity_id: UUID) -> T:
        raise NotImplementedError

    async def get_active_by_id(self, entity_id: UUID) -> T:
        entity = await self.get_by_id(entity_id)
        if not entity.is_active:
            raise InactiveEntityException
        return entity

    @abc.abstractmethod
    async def list_all(self) -> t.List[T]:
        raise NotImplementedError

    @abc.abstractmethod
    async def save(self, entity: T) -> T:
        raise NotImplementedError

    @abc.abstractmethod
    async def delete(self, entity: T) -> None:
        raise NotImplementedError
