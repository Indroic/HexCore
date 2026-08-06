import time
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

    async def incr_window(self, key: str, window_seconds: int) -> tuple[int, float]:
        """
        Contador de ventana atómico (`SupportsAtomicWindow`), compartido entre procesos.

        Dos pasos, los dos atómicos:

        1. `SET key 0 EX <ventana> NX` — crea la ventana **una sola vez** y le pone el TTL
           en la misma operación. El `NX` es lo que evita la carrera clásica de
           "incrementá y después poné el vencimiento", donde dos peticiones simultáneas
           pueden dejar la clave sin TTL y el límite pegado para siempre.
        2. `INCR` + `TTL` en un pipeline transaccional.

        El contador se guarda como entero pelado, no como el JSON que usan `get`/`set`: es
        lo que hace que `INCR` sea posible. No mezclés esta clave con `get()`.
        """
        await self.redis.set(key, 0, ex=window_seconds, nx=True)

        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.incr(key)
            pipe.ttl(key)
            count, ttl = await pipe.execute()

        # `ttl` es -1 (sin vencimiento) o -2 (no existe) si la clave se cayó entre el SET
        # y el pipeline. Ahí se reconstruye la ventana en vez de devolver un reset en el
        # pasado, que haría que el `Retry-After` fuera 1 para siempre.
        remaining = int(ttl) if isinstance(ttl, int) and ttl >= 0 else window_seconds
        if remaining == window_seconds and not (isinstance(ttl, int) and ttl >= 0):
            await self.redis.expire(key, window_seconds)

        return int(count), time.time() + remaining
