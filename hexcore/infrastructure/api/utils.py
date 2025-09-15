from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import Depends

from hexcore.infrastructure.repositories.orms.sqlalchemy.session import AsyncSessionLocal
from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork, NoSqlUnitOfWork


async def get_session():
    async with AsyncSessionLocal() as session:
        yield session


async def get_sql_uow(session: AsyncSession = Depends(get_session)):
    async with SqlAlchemyUnitOfWork(session=session) as uow:
        yield uow


async def get_nosql_uow():
    async with NoSqlUnitOfWork() as uow:
        yield uow
