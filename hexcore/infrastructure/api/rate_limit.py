"""
Rate limiting como dependencia de FastAPI.

Se apoya en el puerto `ICache`, no en Redis directamente, para que funcione con
`MemoryCache` en los tests sin levantar nada.

La ventana lleva su propio vencimiento dentro del valor cacheado (`reset_at`) en vez de
depender del TTL del backend: `MemoryCache` ignora el parámetro `expire`, y un rate
limiter que sólo funciona contra Redis no se puede testear.
"""
from __future__ import annotations

import logging
import time
import typing as t

from fastapi import HTTPException, Request

from hexcore.infrastructure.cache import ICache

logger = logging.getLogger("hexcore.api.rate_limit")

__all__ = ["rate_limit", "RateLimitBackendPolicy", "client_ip_key"]

RateLimitBackendPolicy = t.Literal["allow", "deny"]

KeyFunc = t.Callable[[Request], str]


def client_ip_key(request: Request) -> str:
    """
    Clave por IP del cliente.

    Respeta `X-Forwarded-For` si viene, porque detrás de un balanceador
    `request.client.host` es la IP del balanceador y limitaría a todo el mundo junto.
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client else "unknown"


def rate_limit(
    limit: int,
    window_seconds: int = 60,
    *,
    key: KeyFunc | None = None,
    cache: ICache | None = None,
    on_backend_error: RateLimitBackendPolicy = "allow",
    namespace: str = "hexcore:ratelimit",
) -> t.Callable[..., t.Awaitable[None]]:
    """
    Construye una dependencia que limita las peticiones por clave.

    Args:
        limit: Peticiones permitidas por ventana.
        window_seconds: Duración de la ventana.
        key: Cómo derivar la clave del request. Por defecto, la IP del cliente. Para
            limitar por usuario: ``key=lambda r: r.state.user_id``.
        cache: El backend. Por defecto, `config.cache_backend`.
        on_backend_error: Qué hacer si el backend falla. ``"allow"`` (default) deja pasar
            —un Redis caído no debería tumbar la API—; ``"deny"`` devuelve 429. Es una
            decisión explícita y configurable, no enterrada en un `except`.
        namespace: Prefijo de las claves en el cache.

    Returns:
        Una corutina para usar en ``Depends(...)``.

    Devuelve **429 con `Retry-After`**, que es lo que un cliente bien hecho necesita para
    reintentar sin adivinar.

    Uso::

        @router.get("/reports", dependencies=[Depends(rate_limit(10, 60))])
        async def reports(): ...

        por_usuario = rate_limit(100, 3600, key=lambda r: r.state.user_id)
    """
    if limit < 1:
        raise ValueError("rate_limit(limit=...) tiene que ser >= 1")
    if window_seconds < 1:
        raise ValueError("rate_limit(window_seconds=...) tiene que ser >= 1")

    key_func = key or client_ip_key

    async def dependency(request: Request) -> None:
        backend = cache or _default_cache()
        cache_key = f"{namespace}:{key_func(request)}"
        now = time.time()

        try:
            state = await backend.get(cache_key)
            count, reset_at = _read_window(state, now, window_seconds)

            if count >= limit:
                retry_after = max(1, int(reset_at - now) + 1)
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Límite de {limit} peticiones cada {window_seconds}s alcanzado."
                    ),
                    headers={"Retry-After": str(retry_after)},
                )

            result = backend.set(
                cache_key,
                {"count": count + 1, "reset_at": reset_at},
                expire=window_seconds,
            )
            if _is_awaitable(result):
                await result
        except HTTPException:
            raise
        except Exception as exc:
            if on_backend_error == "deny":
                logger.critical(
                    "El backend de rate limiting falló (%s) y on_backend_error='deny': "
                    "se rechaza la petición.",
                    exc,
                )
                raise HTTPException(
                    status_code=429,
                    detail="Rate limiting no disponible.",
                    headers={"Retry-After": str(window_seconds)},
                ) from exc
            logger.error(
                "El backend de rate limiting falló (%s) y on_backend_error='allow': "
                "se deja pasar la petición sin limitar.",
                exc,
            )

    return dependency


def _read_window(
    state: t.Any,
    now: float,
    window_seconds: int,
) -> tuple[int, float]:
    """
    Lee el contador de la ventana en curso.

    Si no hay estado, o si la ventana ya venció, empieza una nueva. El vencimiento se
    evalúa aquí y no se delega al TTL del backend, porque no todos los backends de
    `ICache` lo respetan.
    """
    if isinstance(state, dict):
        reset_at = state.get("reset_at")
        count = state.get("count")
        if isinstance(reset_at, (int, float)) and isinstance(count, int):
            if reset_at > now:
                return count, float(reset_at)

    return 0, now + window_seconds


def _is_awaitable(value: t.Any) -> bool:
    return hasattr(value, "__await__")


def _default_cache() -> ICache:
    from hexcore.config import LazyConfig

    return LazyConfig.get_config().cache_backend
