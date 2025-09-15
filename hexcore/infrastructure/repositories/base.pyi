import abc
import typing as t
from hexcore.domain.base import BaseEntity as BaseEntity
from hexcore.domain.repositories import IBaseRepository as IBaseRepository
from hexcore.domain.uow import IUnitOfWork as IUnitOfWork
from sqlalchemy.ext.asyncio import AsyncSession as AsyncSession

T = t.TypeVar('T', bound=BaseEntity)

class BaseSQLAlchemyRepository(IBaseRepository[T], abc.ABC, t.Generic[T], metaclass=abc.ABCMeta):
    def __init__(self, uow: IUnitOfWork) -> None: ...
    @property
    def session(self) -> AsyncSession: ...
