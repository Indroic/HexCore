import pytest
from unittest.mock import AsyncMock, MagicMock

from hexcore.infrastructure.cqrs.postgres_lock import PostgresLockProvider


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def mock_pool():
    pool = AsyncMock()
    # Para acquire_lock
    pool.fetchrow = AsyncMock()
    # Para setup y release_lock
    pool.execute = AsyncMock()
    return pool


@pytest.mark.anyio
async def test_postgres_lock_setup(mock_pool):
    provider = PostgresLockProvider(mock_pool)
    await provider.setup()
    
    mock_pool.execute.assert_awaited_once()
    query = mock_pool.execute.call_args[0][0]
    assert "CREATE TABLE IF NOT EXISTS hexcore_cron_locks" in query


@pytest.mark.anyio
async def test_postgres_lock_acquire_success(mock_pool):
    # Simula que fetchrow devuelve una fila (el lock fue adquirido/insertado)
    mock_pool.fetchrow.return_value = {"lock_key": "my_lock"}
    
    provider = PostgresLockProvider(mock_pool)
    acquired = await provider.acquire_lock("my_lock", ttl_seconds=60)
    
    assert acquired is True
    mock_pool.fetchrow.assert_awaited_once()
    args = mock_pool.fetchrow.call_args[0]
    assert "INSERT INTO hexcore_cron_locks" in args[0]
    assert args[1] == "my_lock"
    assert args[2] == "60"


@pytest.mark.anyio
async def test_postgres_lock_acquire_fail(mock_pool):
    # Simula que fetchrow devuelve None (alguien más tiene el lock vivo)
    mock_pool.fetchrow.return_value = None
    
    provider = PostgresLockProvider(mock_pool)
    acquired = await provider.acquire_lock("my_lock", ttl_seconds=60)
    
    assert acquired is False


@pytest.mark.anyio
async def test_postgres_lock_acquire_exception(mock_pool):
    mock_pool.fetchrow.side_effect = Exception("DB Connection Error")
    
    provider = PostgresLockProvider(mock_pool)
    acquired = await provider.acquire_lock("my_lock", ttl_seconds=60)
    
    assert acquired is False


@pytest.mark.anyio
async def test_postgres_lock_release(mock_pool):
    provider = PostgresLockProvider(mock_pool)
    await provider.release_lock("my_lock")
    
    mock_pool.execute.assert_awaited_once()
    args = mock_pool.execute.call_args[0]
    assert "DELETE FROM" in args[0]
    assert args[1] == "my_lock"
