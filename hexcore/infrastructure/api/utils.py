from collections.abc import AsyncGenerator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from hexcore.domain.uow import IUnitOfWork
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
    AsyncSessionLocal,
)
from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork, NoSqlUnitOfWork


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def get_sql_uow(
    session: AsyncSession = Depends(get_session),
) -> AsyncGenerator[IUnitOfWork, None]:
    async with SqlAlchemyUnitOfWork(session=session) as uow:
        yield uow


async def get_nosql_uow() -> AsyncGenerator[IUnitOfWork, None]:
    uow: IUnitOfWork = NoSqlUnitOfWork()
    async with uow:
        yield uow
