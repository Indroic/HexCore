from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hexcore.config import LazyConfig

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    """
    Retorna el engine asíncrono de SQLAlchemy.
    Se inicializa de forma lazy (solo cuando se solicita).
    """
    global _engine
    if _engine is None:
        _engine = create_async_engine(
            LazyConfig.get_config().async_sql_database_url,
            # echo=True,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """
    Retorna la factoría de sesiones de SQLAlchemy.
    Se inicializa de forma lazy.
    """
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            autocommit=False,
            autoflush=False,
            bind=get_engine(),
            class_=AsyncSession,
        )
    return _session_factory


async def get_async_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Generador de sesiones asíncronas delegando el ciclo de vida al context manager."""
    factory = get_session_factory()
    async with factory() as db:
        yield db


async def reset_sqlalchemy_engine() -> None:
    """
    Cierra el pool de conexiones actual y recrea el engine de forma lazy.
    Crítico para inicializar asíncronamente workers (ej. RabbitMQ, Celery) 
    y evitar el error de 'Task attached to a different loop'.
    """
    global _engine
    global _session_factory

    if _engine is not None:
        await _engine.dispose()
    
    _engine = None
    _session_factory = None
