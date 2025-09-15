from __future__ import annotations
import abc
import typing as t
from sqlalchemy.ext.asyncio import AsyncSession

from hexcore.domain.base import BaseEntity
from hexcore.domain.repositories import IBaseRepository
from hexcore.domain.uow import IUnitOfWork

T = t.TypeVar("T", bound=BaseEntity)


class BaseSQLRepository(IBaseRepository[T], abc.ABC, t.Generic[T]):
    def __init__(self, uow: IUnitOfWork):
        # Si la UOW tiene sesión (SQL), la usamos; si no, ignoramos
        self._session: t.Optional[AsyncSession] = getattr(uow, "session", None)

        super().__init__(uow)
