import typing as t
from hexcore.infrastructure.cache import ICache


class MemoryCache(ICache):
    def __init__(self):
        self.cache: t.Dict[str, t.Dict[str, t.Any]] = {}

    async def get(self, key: str) -> t.Optional[t.Dict[str, t.Any]]:
        return self.cache.get(key)

    async def set(
        self,
        key: str,
        value: t.Dict[str, t.Any],
        expire: int = 0,
    ) -> None:
        self.cache[key] = value

    async def delete(self, key: str) -> None:
        self.cache.pop(key, None)

    async def clear(self) -> None:
        self.cache.clear()
