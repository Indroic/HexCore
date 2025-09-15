import typing as t
import redis.asyncio as redis
import json
from hexcore.infrastructure.cache import ICache
from hexcore.config import LazyConfig


class RedisCache(ICache):
    def __init__(self):
        config = LazyConfig.get_config()
        self.redis: redis.Redis = redis.Redis.from_url(  # type: ignore
            config.redis_uri, decode_responses=True
        )

    async def get(self, key: str) -> t.Optional[t.Dict[str, t.Any]]:
        value = await self.redis.get(key)
        return json.loads(value) if value else None

    async def set(
        self,
        key: str,
        value: t.Dict[str, t.Any],
        expire: int = LazyConfig().get_config().redis_cache_duration,
    ) -> None:
        await self.redis.set(key, json.dumps(value), ex=expire)

    async def delete(self, key: str) -> None:
        await self.redis.delete(key)

    async def clear(self):
        await self.redis.flushdb(asynchronous=True)  # type: ignore
