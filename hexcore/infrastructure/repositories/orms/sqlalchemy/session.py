from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hexcore.config import LazyConfig


# 1. CREAR EL ENGINE ASÍNCRONO DE SQLAlchemy
# Usamos create_async_engine en lugar de create_engine.
engine: AsyncEngine = create_async_engine(
    LazyConfig.get_config().async_sql_database_url,
    # `echo=True` es útil para depuración, ya que imprime todas las sentencias SQL.
    # Desactívalo en producción.
    # echo=True,
)

# 2. CREAR UNA FACTORÍA DE SESIONES ASÍNCRONAS
# Usamos async_sessionmaker y especificamos la clase AsyncSession.
AsyncSessionLocal: async_sessionmaker[AsyncSession] = async_sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    class_=AsyncSession,
)


async def get_async_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Generador de sesiones asíncronas delegando el ciclo de vida al context manager."""
    async with AsyncSessionLocal() as db:
        try:
            yield db
        except Exception:
            if db.in_transaction():
                await db.rollback()
            raise
