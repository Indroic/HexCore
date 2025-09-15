from _typeshed import Incomplete
from collections.abc import Generator
from hexcore.config import LazyConfig as LazyConfig
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

engine: Incomplete
AsyncSessionLocal: async_sessionmaker[AsyncSession]

async def get_async_db_session() -> Generator[Incomplete]: ...
