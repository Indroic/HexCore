import time
import typing as t
from hexcore.infrastructure.cache import ICache


class MemoryCache(ICache):
    def __init__(self):
        self.cache: t.Dict[str, t.Dict[str, t.Any]] = {}

    async def incr_window(self, key: str, window_seconds: int) -> tuple[int, float]:
        """
        Contador de ventana atómico (`SupportsAtomicWindow`).

        Atómico porque entre la lectura y la escritura **no hay ningún `await`**: dentro
        de un event loop, esta corutina no cede el control en el medio, así que dos
        peticiones concurrentes no pueden leer el mismo contador. Es exactamente la
        garantía que el par `get()`/`set()` no puede dar.

        No sirve entre procesos, y no pretende: para varios workers hace falta un backend
        compartido (`RedisCache`).

        El vencimiento se guarda **dentro del valor** porque `set()` acá ignora `expire` y
        nunca desaloja; si el TTL fuera del backend, la ventana no vencería jamás.
        """
        now = time.time()
        state = self.cache.get(key)

        reset_at = None
        if isinstance(state, dict):
            candidate = state.get("reset_at")
            if isinstance(candidate, (int, float)) and candidate > now:
                reset_at = float(candidate)

        if reset_at is None:
            count, reset_at = 1, now + window_seconds
        else:
            previous = state.get("count") if isinstance(state, dict) else 0
            count = (previous if isinstance(previous, int) else 0) + 1

        self.cache[key] = {"count": count, "reset_at": reset_at}
        return count, reset_at

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
