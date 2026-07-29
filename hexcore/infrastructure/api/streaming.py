"""
Utilidades de streaming: SSE, WebSocket y límite de conexiones por usuario.

`ServerConfig` de las apps reales ya declara `sse_heartbeat_seconds`,
`admin_stream_max_connections_per_user` y compañía — es decir, el proyecto configuró
estas cosas y luego tuvo que implementarlas él mismo. Si la configuración es genérica,
la implementación debería serlo.

De las tres, `connection_slot` es la que más valor tiene: filtrar un slot al
desconectarse mal es un bug clásico —el usuario queda sin poder reconectar hasta que
expire—, y aquí el teardown está garantizado.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import typing as t
import uuid
from contextlib import asynccontextmanager

from fastapi.responses import StreamingResponse

from hexcore.infrastructure.cache import ICache

if t.TYPE_CHECKING:
    from fastapi import WebSocket

logger = logging.getLogger("hexcore.api.streaming")

__all__ = ["sse_stream", "format_sse_event", "ws_heartbeat", "connection_slot"]


def format_sse_event(payload: t.Mapping[str, t.Any]) -> str:
    """
    Formatea un dict como un evento SSE.

    Soporta las claves de control del protocolo (`event`, `id`, `retry`); el resto del
    dict va como JSON en `data`.
    """
    lines: list[str] = []

    event_name = payload.get("event")
    if event_name is not None:
        lines.append(f"event: {event_name}")

    event_id = payload.get("id")
    if event_id is not None:
        lines.append(f"id: {event_id}")

    retry = payload.get("retry")
    if retry is not None:
        lines.append(f"retry: {retry}")

    data = payload.get("data", {k: v for k, v in payload.items() if k not in _CONTROL_KEYS})
    if isinstance(data, str):
        serialized = data
    else:
        serialized = json.dumps(data, default=str)
    # Un `data` multilínea necesita un `data:` por línea, o el cliente corta el evento.
    for line in serialized.splitlines() or [""]:
        lines.append(f"data: {line}")

    return "\n".join(lines) + "\n\n"


_CONTROL_KEYS = frozenset({"event", "id", "retry", "data"})


def sse_stream(
    source: t.AsyncIterator[t.Mapping[str, t.Any]],
    *,
    heartbeat_seconds: float = 30.0,
    **response_kwargs: t.Any,
) -> StreamingResponse:
    """
    Envuelve un iterador async en una `StreamingResponse` de SSE con heartbeat.

    El heartbeat es un comentario SSE (`: ping`), que los clientes ignoran y los proxies
    cuentan como tráfico. Sin él, un balanceador con idle timeout corta la conexión en
    cuanto pasan unos minutos sin eventos, y el cliente lo ve como un error.

    Args:
        source: Iterador de eventos. Cada elemento se formatea con `format_sse_event`.
        heartbeat_seconds: Cada cuánto emitir el ping. 0 o negativo lo desactiva.
        **response_kwargs: Se pasan a `StreamingResponse` (`status_code`, `headers`…).
    """
    async def generator() -> t.AsyncIterator[str]:
        iterator = source.__aiter__()
        pending: asyncio.Task[t.Mapping[str, t.Any]] | None = None
        try:
            while True:
                if pending is None:
                    pending = asyncio.ensure_future(_anext(iterator))

                if heartbeat_seconds and heartbeat_seconds > 0:
                    done, _ = await asyncio.wait(
                        [pending], timeout=heartbeat_seconds
                    )
                    if not done:
                        yield ": ping\n\n"
                        continue
                else:
                    await asyncio.wait([pending])

                try:
                    event = pending.result()
                except StopAsyncIteration:
                    return
                finally:
                    pending = None

                yield format_sse_event(event)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()
                with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                    await pending
            aclose = getattr(iterator, "aclose", None)
            if aclose is not None:
                with contextlib.suppress(Exception):
                    await aclose()

    headers = {
        "Cache-Control": "no-cache",
        "Connection": "keep-alive",
        # Desactiva el buffering de nginx, que si no acumula los eventos y los entrega
        # a bloques: el stream "funciona" pero llega con minutos de retraso.
        "X-Accel-Buffering": "no",
        **response_kwargs.pop("headers", {}),
    }
    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers=headers,
        **response_kwargs,
    )


async def _anext(iterator: t.AsyncIterator[t.Mapping[str, t.Any]]) -> t.Mapping[str, t.Any]:
    return await iterator.__anext__()


@asynccontextmanager
async def ws_heartbeat(
    ws: "WebSocket",
    interval: float = 30.0,
) -> t.AsyncIterator[None]:
    """
    Mantiene vivo un WebSocket enviando pings mientras dure el bloque.

    El ping se cancela al salir, también si el bloque lanza. Un error al enviar el ping
    (el cliente ya se fue) termina la tarea en silencio: quien lea del socket se va a
    enterar igual, y no queremos dos excepciones compitiendo.

    Uso::

        await ws.accept()
        async with ws_heartbeat(ws, interval=30):
            while True:
                message = await ws.receive_text()
                ...
    """
    async def beat() -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await ws.send_json({"type": "ping", "ts": time.time()})
            except Exception:
                logger.debug("Heartbeat de WebSocket detenido: el cliente cerró.")
                return

    task: asyncio.Task[None] | None = None
    if interval and interval > 0:
        task = asyncio.create_task(beat())
    try:
        yield
    finally:
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


@asynccontextmanager
async def connection_slot(
    cache: ICache,
    key: str,
    *,
    max_connections: int,
    ttl_seconds: int = 3600,
) -> t.AsyncIterator[bool]:
    """
    Reserva un slot de conexión, y lo **libera siempre** al salir del bloque.

    Cede `True` si había hueco y `False` si no. El teardown está garantizado por el
    context manager, que es todo el punto: filtrar un slot al desconectarse mal deja al
    usuario sin poder reconectar hasta que expire el registro.

    **Límite conocido:** la secuencia leer-comprobar-escribir no es atómica sobre el
    puerto `ICache`, que no expone un CAS. Con varias réplicas aceptando conexiones en el
    mismo milisegundo se puede conceder un slot de más. Es aceptable para lo que esto
    resuelve (evitar que un usuario abra cientos de conexiones); si necesitás el límite
    exacto, hace falta un script Lua sobre Redis, y eso ataría la utilidad a Redis.

    Args:
        cache: Backend `ICache` (Redis en producción, `MemoryCache` en tests).
        key: Identifica al sujeto del límite (p. ej. ``f"ws:{user_id}"``).
        max_connections: Cuántas conexiones simultáneas se permiten.
        ttl_seconds: Vencimiento del registro, como red de seguridad si el proceso muere
            entero sin poder liberar.

    Uso::

        async with connection_slot(cache, f"ws:{user_id}", max_connections=3) as granted:
            if not granted:
                await ws.close(code=1013)  # try again later
                return
            await ws.accept()
            ...
    """
    cache_key = f"hexcore:conn_slot:{key}"
    # UUID y no `id()` + timestamp: `id()` se reutiliza en cuanto el objeto muere, y dos
    # slot_id iguales se pisan en el dict, con lo que el contador se queda corto y el
    # límite deja de limitar.
    slot_id = uuid.uuid4().hex
    granted = False

    try:
        state = await cache.get(cache_key)
        slots = _read_slots(state, ttl_seconds)

        if len(slots) >= max_connections:
            logger.info(
                "Slot de conexión denegado para '%s': %d/%d en uso.",
                key,
                len(slots),
                max_connections,
            )
            yield False
            return

        slots[slot_id] = time.time() + ttl_seconds
        await _store(cache, cache_key, slots, ttl_seconds)
        granted = True
        yield True
    finally:
        if granted:
            # Se relee para no pisar los slots que otras conexiones tomaron mientras
            # esta estaba abierta.
            try:
                state = await cache.get(cache_key)
                slots = _read_slots(state, ttl_seconds)
                slots.pop(slot_id, None)
                await _store(cache, cache_key, slots, ttl_seconds)
            except Exception:
                logger.exception(
                    "No se pudo liberar el slot de conexión '%s'; expirará en %ss.",
                    key,
                    ttl_seconds,
                )


def _read_slots(state: t.Any, ttl_seconds: int) -> dict[str, float]:
    """
    Slots vigentes, descartando los vencidos.

    El vencimiento se evalúa aquí porque `MemoryCache` ignora el `expire` del backend, y
    porque un slot con TTL vencido no debe contar aunque el backend aún lo tenga.
    """
    if not isinstance(state, dict):
        return {}

    raw = state.get("slots")
    if not isinstance(raw, dict):
        return {}

    now = time.time()
    return {
        slot_id: expires_at
        for slot_id, expires_at in raw.items()
        if isinstance(expires_at, (int, float)) and expires_at > now
    }


async def _store(
    cache: ICache, cache_key: str, slots: dict[str, float], ttl_seconds: int
) -> None:
    result = cache.set(cache_key, {"slots": slots}, expire=ttl_seconds)
    if hasattr(result, "__await__"):
        await result
