import typing as t
import abc

__all__ = ["ICache", "SupportsAtomicWindow"]


class ICache(abc.ABC):
    @abc.abstractmethod
    async def get(self, key: str) -> t.Optional[t.Any]:
        pass

    @abc.abstractmethod
    def set(self, key: str, value: t.Any, expire: int = 3600) -> t.Any:
        pass

    @abc.abstractmethod
    def delete(self, key: str) -> t.Union[t.Any, None]:
        pass


@t.runtime_checkable
class SupportsAtomicWindow(t.Protocol):
    """
    Capacidad opcional de un backend de cache: contar una ventana **atómicamente**.

    Existe como Protocol aparte y no como método de `ICache` porque agregar un
    `@abc.abstractmethod` al puerto rompería a todo el que ya tenga un backend propio.
    Quien la implementa, la ofrece; quien no, sigue funcionando.

    Por qué hace falta: un contador hecho con `get()` y después `set()` tiene un
    read-modify-write con un `await` en el medio, así que N peticiones concurrentes leen
    todas el mismo valor y pasan todas. Contra un endpoint de login eso convierte el
    límite en un techo blando: con `limit=5` y 50 peticiones simultáneas pasan bastante
    más de 5. `rate_limit` usa esta capacidad si el backend la tiene y avisa por log
    cuando no.

    Uso::

        class MiCache(ICache):
            async def incr_window(self, key: str, window_seconds: int) -> tuple[int, float]:
                ...  # devolvé (contador_ya_incrementado, timestamp_de_reset)
    """

    async def incr_window(self, key: str, window_seconds: int) -> tuple[int, float]:
        """
        Incrementa el contador de `key` y devuelve `(contador, reset_at)`.

        `reset_at` es un timestamp absoluto (epoch en segundos). La primera llamada de una
        ventana devuelve `1`, y es la que fija el vencimiento.
        """
        ...
