import pytest
from unittest.mock import AsyncMock

from hexcore.infrastructure.cqrs.redis_lock import RedisLockProvider


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture
def mock_redis():
    redis = AsyncMock()
    return redis


@pytest.mark.anyio
async def test_redis_lock_acquire_success(mock_redis):
    # Simula que set devuelve True (el lock no existía y se asignó)
    mock_redis.set.return_value = True
    
    provider = RedisLockProvider(mock_redis)
    acquired = await provider.acquire_lock("my_lock", ttl_seconds=60)
    
    assert acquired is True
    mock_redis.set.assert_awaited_once_with("my_lock", "locked", nx=True, ex=60)


@pytest.mark.anyio
async def test_redis_lock_acquire_fail(mock_redis):
    # Simula que set devuelve None (el lock ya existía por nx=True)
    mock_redis.set.return_value = None
    
    provider = RedisLockProvider(mock_redis)
    acquired = await provider.acquire_lock("my_lock", ttl_seconds=60)
    
    assert acquired is False
    mock_redis.set.assert_awaited_once_with("my_lock", "locked", nx=True, ex=60)


@pytest.mark.anyio
async def test_redis_lock_acquire_exception(mock_redis):
    # Simula que la conexión a Redis falla
    mock_redis.set.side_effect = Exception("Connection Error")
    
    provider = RedisLockProvider(mock_redis)
    acquired = await provider.acquire_lock("my_lock", ttl_seconds=60)
    
    assert acquired is False


@pytest.mark.anyio
async def test_redis_lock_release(mock_redis):
    provider = RedisLockProvider(mock_redis)
    await provider.release_lock("my_lock")
    
    mock_redis.delete.assert_awaited_once_with("my_lock")


@pytest.mark.anyio
async def test_redis_lock_release_exception(mock_redis):
    # Asegura que las excepciones en release_lock no bloquean la aplicación,
    # solo loguean el error.
    mock_redis.delete.side_effect = Exception("Connection Error")
    
    provider = RedisLockProvider(mock_redis)
    await provider.release_lock("my_lock")
    mock_redis.delete.assert_awaited_once_with("my_lock")
