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

    queries = [call.args[0] for call in mock_pool.execute.await_args_list]
    assert any("CREATE TABLE IF NOT EXISTS hexcore_cron_locks" in q for q in queries)
    # P0-4: el índice sobre expires_at es lo que hace la purga barata.
    assert any(
        "CREATE INDEX IF NOT EXISTS hexcore_cron_locks_expires_at_idx" in q
        for q in queries
    )
    # P0-4: setup() arrastra la basura de ejecuciones anteriores.
    assert any("DELETE FROM hexcore_cron_locks" in q for q in queries)


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


# ── P0-4: la tabla de locks no puede crecer sin límite ─────────────────────────


@pytest.mark.anyio
async def test_acquire_lock_purges_expired_rows_periodically(mock_pool):
    mock_pool.fetchrow.return_value = {"lock_key": "k"}
    provider = PostgresLockProvider(mock_pool, purge_every=5)

    for i in range(5):
        await provider.acquire_lock(f"job:{i}", ttl_seconds=60)

    deletes = [
        call.args[0]
        for call in mock_pool.execute.await_args_list
        if "DELETE FROM hexcore_cron_locks" in call.args[0]
    ]
    assert len(deletes) == 1, "la purga no se disparó al alcanzar purge_every"
    assert "expires_at <" in deletes[0]


@pytest.mark.anyio
async def test_acquire_lock_does_not_purge_before_threshold(mock_pool):
    mock_pool.fetchrow.return_value = {"lock_key": "k"}
    provider = PostgresLockProvider(mock_pool, purge_every=100)

    for i in range(10):
        await provider.acquire_lock(f"job:{i}", ttl_seconds=60)

    assert mock_pool.execute.await_count == 0


@pytest.mark.anyio
async def test_purge_every_zero_disables_automatic_purge(mock_pool):
    mock_pool.fetchrow.return_value = {"lock_key": "k"}
    provider = PostgresLockProvider(mock_pool, purge_every=0)

    for i in range(200):
        await provider.acquire_lock(f"job:{i}", ttl_seconds=60)

    assert mock_pool.execute.await_count == 0


@pytest.mark.anyio
async def test_purge_expired_respects_grace_period(mock_pool):
    provider = PostgresLockProvider(mock_pool, purge_grace_seconds=7200)
    await provider.purge_expired()

    args = mock_pool.execute.call_args[0]
    assert "DELETE FROM hexcore_cron_locks" in args[0]
    assert args[1] == "7200"


@pytest.mark.anyio
async def test_purge_expired_does_not_propagate_errors(mock_pool):
    mock_pool.execute.side_effect = Exception("DB down")
    provider = PostgresLockProvider(mock_pool)

    await provider.purge_expired()  # no debe lanzar


@pytest.mark.anyio
async def test_purge_failure_does_not_break_acquisition(mock_pool):
    mock_pool.fetchrow.return_value = {"lock_key": "k"}
    mock_pool.execute.side_effect = Exception("DB down")
    provider = PostgresLockProvider(mock_pool, purge_every=1)

    assert await provider.acquire_lock("job", ttl_seconds=60) is True
