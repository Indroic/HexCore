"""
Rate limiting como dependencia de FastAPI.

Se apoya en el puerto `ICache`, no en Redis directamente, para que funcione con
`MemoryCache` en los tests sin levantar nada.

La ventana lleva su propio vencimiento dentro del valor cacheado (`reset_at`) en vez de
depender del TTL del backend: `MemoryCache` ignora el parámetro `expire`, y un rate
limiter que sólo funciona contra Redis no se puede testear.
"""
from __future__ import annotations

import ipaddress
import logging
import time
import typing as t

from fastapi import HTTPException, Request

from hexcore.infrastructure.cache import ICache, SupportsAtomicWindow

logger = logging.getLogger("hexcore.api.rate_limit")

__all__ = [
    "rate_limit",
    "RateLimitBackendPolicy",
    "client_ip_key",
    "forwarded_ip_key",
]

RateLimitBackendPolicy = t.Literal["allow", "deny"]

KeyFunc = t.Callable[[Request], str]


def client_ip_key(request: Request) -> str:
    """
    Clave por IP del par TCP. **No** mira `X-Forwarded-For`.

    Antes sí lo miraba, sin condiciones, y eso hacía que el límite no limitara nada:
    `X-Forwarded-For` lo escribe el cliente, así que mandar un valor distinto en cada
    petición daba un bucket nuevo en cada petición. Contra un endpoint de login, un
    límite de 5 intentos se volvía infinito con una línea de código.

    Si estás detrás de un proxy o un balanceador, `request.client.host` es la IP del
    proxy y esto limita a todo el mundo junto — que es el lado **seguro** del error, pero
    no el que querés. Para ese caso usá `forwarded_ip_key()`, que exige declarar en quién
    confiás.

    Uso::

        # sin proxy
        rate_limit(5, 300, key=client_ip_key)

        # detrás de un balanceador
        rate_limit(5, 300, key=forwarded_ip_key(trusted_proxies=["10.0.0.0/8"]))
    """
    client = request.client
    return client.host if client else "unknown"


def forwarded_ip_key(
    *,
    trusted_proxies: t.Collection[str],
    trust_hops: int = 1,
) -> KeyFunc:
    """
    Clave por IP real del cliente, leyendo `X-Forwarded-For` **sólo de proxies declarados**.

    El header sólo se honra si el par TCP inmediato está en `trusted_proxies`. Si no lo
    está, se cae a la IP del par y se ignora lo que haya mandado: alguien que llega
    directo a la app no puede elegir su propio bucket.

    Args:
        trusted_proxies: IPs o redes CIDR de tus proxies (``["10.0.0.0/8", "172.18.0.5"]``).
            No puede estar vacío: un allowlist vacío significaría "no confío en nadie", y
            para eso ya está `client_ip_key`.
        trust_hops: Cuántos proxies tenés adelante. Cada proxy **agrega** la IP de quien le
            habló, así que con un balanceador el cliente es el último elemento del header,
            con dos es el penúltimo, y así. Se cuenta desde la derecha, que es la única
            parte del header que no controla el cliente: los elementos de la izquierda los
            puede inventar.

    Raises:
        ValueError: Si `trusted_proxies` está vacío, si alguna entrada no es una IP/red
            válida, o si `trust_hops < 1`.

    Uso::

        # un ALB delante
        key = forwarded_ip_key(trusted_proxies=["10.0.0.0/8"])

        # CDN -> balanceador -> app
        key = forwarded_ip_key(trusted_proxies=["10.0.0.0/8"], trust_hops=2)
    """
    if trust_hops < 1:
        raise ValueError(
            "forwarded_ip_key(trust_hops=...) tiene que ser >= 1. Si no tenés ningún "
            "proxy adelante, usá `client_ip_key` en vez de esta función."
        )
    if not trusted_proxies:
        raise ValueError(
            "forwarded_ip_key(trusted_proxies=...) no puede estar vacío: sin proxies de "
            "confianza, `X-Forwarded-For` lo controla el cliente y el límite no limita "
            "nada. Si no tenés proxy, usá `client_ip_key`."
        )

    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in trusted_proxies:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError as exc:
            raise ValueError(
                f"forwarded_ip_key(trusted_proxies=...): {entry!r} no es una IP ni una red "
                f"CIDR válida. Ejemplos válidos: '10.0.0.0/8', '172.18.0.5'."
            ) from exc

    def key_func(request: Request) -> str:
        client = request.client
        peer = client.host if client else None
        if peer is None or not _ip_in_any(peer, networks):
            # El par no es un proxy declarado: su header no vale nada.
            return peer or "unknown"

        forwarded = request.headers.get("X-Forwarded-For")
        if not forwarded:
            return peer

        hops = [item.strip() for item in forwarded.split(",") if item.strip()]
        if len(hops) < trust_hops:
            # Menos saltos de los declarados: el header está incompleto o alguien lo
            # recortó. Se cae al par en vez de agarrar la entrada más a la izquierda,
            # que es justo la que el cliente puede inventar.
            return peer

        candidate = hops[-trust_hops]
        try:
            ipaddress.ip_address(candidate)
        except ValueError:
            return peer
        return candidate

    return key_func


def _ip_in_any(
    address: str,
    networks: t.Sequence[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    return any(parsed in network for network in networks)


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
            if isinstance(backend, SupportsAtomicWindow):
                # Camino atómico: se incrementa primero y se compara después. Al revés
                # —leer, comparar, escribir— hay un `await` entre la lectura y la
                # escritura, así que N peticiones concurrentes leen el mismo contador y
                # pasan todas.
                count, reset_at = await backend.incr_window(cache_key, window_seconds)
                excedido = count > limit
            else:
                _warn_backend_sin_atomicidad(backend)
                state = await backend.get(cache_key)
                count, reset_at = _read_window(state, now, window_seconds)
                excedido = count >= limit
                if not excedido:
                    result = backend.set(
                        cache_key,
                        {"count": count + 1, "reset_at": reset_at},
                        expire=window_seconds,
                    )
                    if _is_awaitable(result):
                        await result

            if excedido:
                retry_after = max(1, int(reset_at - now) + 1)
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Límite de {limit} peticiones cada {window_seconds}s alcanzado."
                    ),
                    headers={"Retry-After": str(retry_after)},
                )
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


_BACKENDS_AVISADOS: set[int] = set()


def _warn_backend_sin_atomicidad(backend: ICache) -> None:
    """
    Avisa una sola vez por backend que el conteo no es atómico.

    Una vez y no por petición: un log por request en el camino caliente es peor que el
    problema que denuncia. `id()` alcanza porque el backend es un singleton de proceso.
    """
    marca = id(backend)
    if marca in _BACKENDS_AVISADOS:
        return
    _BACKENDS_AVISADOS.add(marca)
    logger.warning(
        "%s no implementa `incr_window`, así que el conteo del rate limit hace "
        "read-modify-write y un pico de peticiones concurrentes puede pasarse del "
        "límite. Implementá `SupportsAtomicWindow` en tu backend, o usá `RedisCache`.",
        type(backend).__name__,
    )


def _default_cache() -> ICache:
    from hexcore.config import LazyConfig

    return LazyConfig.get_config().cache_backend
