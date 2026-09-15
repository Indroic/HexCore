"""
Fixtures de pytest para aplicaciones HexCore.

Actívalas desde tu `conftest.py`::

    pytest_plugins = ["hexcore.testing.fixtures"]

Este módulo importa `pytest`, así que sólo se carga cuando lo pides — `hexcore.testing`
no lo arrastra.
"""
from __future__ import annotations

import typing as t

import pytest

from .fakes import FakeLockProvider, InMemoryTaskEnqueuer
from .helpers import TestBuses, build_test_buses

__all__ = [
    "anyio_backend",
    "task_enqueuer",
    "lock_provider",
    "cqrs_buses",
    "sqlite_engine",
    "sqlite_session",
    "uow",
]


@pytest.fixture
def anyio_backend() -> str:
    """HexCore usa asyncio; se declara aquí para no repetirlo en cada módulo."""
    return "asyncio"


@pytest.fixture
def task_enqueuer() -> InMemoryTaskEnqueuer:
    """Un `InMemoryTaskEnqueuer` limpio."""
    return InMemoryTaskEnqueuer()


@pytest.fixture
def lock_provider() -> FakeLockProvider:
    """Un `FakeLockProvider` que concede siempre."""
    return FakeLockProvider()


@pytest.fixture
def cqrs_buses(task_enqueuer: InMemoryTaskEnqueuer) -> TestBuses:
    """Los tres buses in-memory con Smart Routing listo."""
    return build_test_buses(enqueuer=task_enqueuer)


@pytest.fixture
def sqlite_engine() -> t.Iterator[t.Any]:
    """
    Un engine SQLite en memoria, y el módulo de sesión de HexCore apuntando a él.

    Usa `StaticPool`: con `:memory:` y el pool por defecto, cada conexión estrena una
    base vacía y nada de lo que escribas en una sesión se ve en la siguiente.
    """
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("aiosqlite")

    import asyncio

    from sqlalchemy.pool import StaticPool

    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
        dispose_engine,
        init_engine,
    )

    asyncio.run(dispose_engine())
    engine = init_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    try:
        yield engine
    finally:
        asyncio.run(dispose_engine())


@pytest.fixture
async def sqlite_session(sqlite_engine: t.Any) -> t.AsyncIterator[t.Any]:
    """Una sesión sobre `sqlite_engine`, con las tablas de `Base` ya creadas."""
    from hexcore.infrastructure.repositories.orms.sqlalchemy import Base
    from hexcore.infrastructure.uow.scopes import session_scope

    async with sqlite_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_scope() as session:
        yield session


@pytest.fixture
async def uow(sqlite_session: t.Any) -> t.AsyncIterator[t.Any]:
    """
    Un `SqlAlchemyUnitOfWork` sobre `sqlite_session`, sin entrar en él.

    Sigue la convención de F3: el use case controla su propio `async with uow:`.
    """
    from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork

    yield SqlAlchemyUnitOfWork(session=sqlite_session)
