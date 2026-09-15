"""
P1-2: los lock providers ya no apagan el cron entero en silencio.

Ambos capturaban `Exception` y devolvían `False`. Si Redis (o Postgres) se caía,
`acquire_lock` devolvía `False` para **todos** los jobs → el cron completo dejaba de
funcionar, con un `logger.error` por job y por tick indistinguible de "el lock estaba
tomado por otra réplica", que es el caso normal y esperado.
"""
from __future__ import annotations

import logging

import pytest
from unittest.mock import AsyncMock

from hexcore.infrastructure.cqrs.postgres_lock import PostgresLockProvider
from hexcore.infrastructure.cqrs.redis_lock import RedisLockProvider


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── Redis ──────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_redis_skip_is_the_default_and_returns_false():
    client = AsyncMock()
    client.set.side_effect = ConnectionError("redis down")
    provider = RedisLockProvider(client)

    assert provider.on_error == "skip"
    assert await provider.acquire_lock("k", 60) is False


@pytest.mark.anyio
async def test_redis_raise_propagates_the_error():
    client = AsyncMock()
    client.set.side_effect = ConnectionError("redis down")
    provider = RedisLockProvider(client, on_error="raise")

    with pytest.raises(ConnectionError):
        await provider.acquire_lock("k", 60)


@pytest.mark.anyio
async def test_redis_undecidable_logs_critical(caplog):
    client = AsyncMock()
    client.set.side_effect = ConnectionError("redis down")
    provider = RedisLockProvider(client)

    with caplog.at_level(logging.DEBUG):
        await provider.acquire_lock("k", 60)

    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(criticals) == 1
    assert "No se pudo decidir" in criticals[0].getMessage()


@pytest.mark.anyio
async def test_redis_lock_already_taken_is_only_debug(caplog):
    """El caso normal no debe generar ruido de nivel error/critical."""
    client = AsyncMock()
    client.set.return_value = None  # otro proceso lo tiene
    provider = RedisLockProvider(client)

    with caplog.at_level(logging.DEBUG):
        assert await provider.acquire_lock("k", 60) is False

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert any("ya estaba tomado" in r.getMessage() for r in caplog.records)


@pytest.mark.anyio
async def test_redis_acquire_success():
    client = AsyncMock()
    client.set.return_value = True
    provider = RedisLockProvider(client)

    assert await provider.acquire_lock("k", 60) is True


# ── Postgres ───────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_postgres_skip_is_the_default_and_returns_false():
    pool = AsyncMock()
    pool.fetchrow.side_effect = ConnectionError("pg down")
    provider = PostgresLockProvider(pool)

    assert provider.on_error == "skip"
    assert await provider.acquire_lock("k", 60) is False


@pytest.mark.anyio
async def test_postgres_raise_propagates_the_error():
    pool = AsyncMock()
    pool.fetchrow.side_effect = ConnectionError("pg down")
    provider = PostgresLockProvider(pool, on_error="raise")

    with pytest.raises(ConnectionError):
        await provider.acquire_lock("k", 60)


@pytest.mark.anyio
async def test_postgres_undecidable_logs_critical(caplog):
    pool = AsyncMock()
    pool.fetchrow.side_effect = ConnectionError("pg down")
    provider = PostgresLockProvider(pool)

    with caplog.at_level(logging.DEBUG):
        await provider.acquire_lock("k", 60)

    criticals = [r for r in caplog.records if r.levelno == logging.CRITICAL]
    assert len(criticals) == 1
    assert "No se pudo decidir" in criticals[0].getMessage()


@pytest.mark.anyio
async def test_postgres_lock_already_taken_is_only_debug(caplog):
    pool = AsyncMock()
    pool.fetchrow.return_value = None
    provider = PostgresLockProvider(pool, purge_every=0)

    with caplog.at_level(logging.DEBUG):
        assert await provider.acquire_lock("k", 60) is False

    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
    assert any("ya estaba tomado" in r.getMessage() for r in caplog.records)
