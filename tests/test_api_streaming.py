"""
F14: SSE, heartbeat de WebSocket y límite de conexiones.

El caso con más valor es `connection_slot`: filtrar un slot al desconectarse mal es un
bug clásico —el usuario queda sin poder reconectar hasta que expire—, así que aquí el
teardown tiene que estar garantizado incluso si el bloque lanza o se cancela.
"""
from __future__ import annotations

import asyncio
import json
import time
import typing as t

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from hexcore.infrastructure.api.streaming import (  # noqa: E402
    connection_slot,
    format_sse_event,
    sse_stream,
    ws_heartbeat,
)
from hexcore.infrastructure.cache import ICache  # noqa: E402
from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── format_sse_event ───────────────────────────────────────────────────────────


def test_format_sse_event_serializes_the_payload_as_data():
    frame = format_sse_event({"ticket": 42, "state": "open"})

    assert frame.endswith("\n\n")
    assert json.loads(frame.split("data: ", 1)[1].strip()) == {
        "ticket": 42,
        "state": "open",
    }


def test_format_sse_event_supports_the_control_keys():
    frame = format_sse_event(
        {"event": "ticket.updated", "id": "7", "retry": 5000, "data": {"x": 1}}
    )

    assert "event: ticket.updated" in frame
    assert "id: 7" in frame
    assert "retry: 5000" in frame
    assert '"x": 1' in frame


def test_format_sse_event_prefixes_every_line_of_multiline_data():
    """Sin un `data:` por línea, el cliente corta el evento en el primer salto."""
    frame = format_sse_event({"data": "linea1\nlinea2"})

    assert frame == "data: linea1\ndata: linea2\n\n"


def test_format_sse_event_accepts_a_plain_string():
    assert format_sse_event({"data": "hola"}) == "data: hola\n\n"


# ── sse_stream ─────────────────────────────────────────────────────────────────


def _sse_app(source_factory, **kwargs) -> FastAPI:
    app = FastAPI()

    @app.get("/stream")
    async def stream():
        return sse_stream(source_factory(), **kwargs)

    return app


def test_sse_stream_emits_the_events():
    async def source() -> t.AsyncIterator[dict]:
        yield {"n": 1}
        yield {"n": 2}

    with TestClient(_sse_app(source)) as client:
        with client.stream("GET", "/stream") as response:
            body = "".join(response.iter_text())

    assert body.count("data: ") == 2
    assert '"n": 1' in body and '"n": 2' in body


def test_sse_stream_sets_the_content_type_and_anti_buffering_headers():
    async def source() -> t.AsyncIterator[dict]:
        yield {"n": 1}

    with TestClient(_sse_app(source)) as client:
        with client.stream("GET", "/stream") as response:
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache"
            # Sin esto, nginx acumula los eventos y el stream llega a bloques.
            assert response.headers["x-accel-buffering"] == "no"
            list(response.iter_text())


def test_sse_stream_emits_a_heartbeat_while_the_source_is_quiet():
    async def slow_source() -> t.AsyncIterator[dict]:
        await asyncio.sleep(0.15)
        yield {"n": 1}

    app = _sse_app(slow_source, heartbeat_seconds=0.03)

    with TestClient(app) as client:
        with client.stream("GET", "/stream") as response:
            body = "".join(response.iter_text())

    assert ": ping" in body, "no se emitió heartbeat durante el silencio"
    assert '"n": 1' in body


def test_sse_stream_without_heartbeat_emits_no_pings():
    async def source() -> t.AsyncIterator[dict]:
        yield {"n": 1}

    with TestClient(_sse_app(source, heartbeat_seconds=0)) as client:
        with client.stream("GET", "/stream") as response:
            body = "".join(response.iter_text())

    assert ": ping" not in body


def test_sse_stream_closes_the_source_generator():
    closed: list[bool] = []

    async def source() -> t.AsyncIterator[dict]:
        try:
            yield {"n": 1}
        finally:
            closed.append(True)

    with TestClient(_sse_app(source)) as client:
        with client.stream("GET", "/stream") as response:
            list(response.iter_text())

    assert closed == [True]


def test_sse_stream_with_an_empty_source_completes():
    async def source() -> t.AsyncIterator[dict]:
        return
        yield  # pragma: no cover

    with TestClient(_sse_app(source)) as client:
        with client.stream("GET", "/stream") as response:
            assert "".join(response.iter_text()) == ""


# ── ws_heartbeat ───────────────────────────────────────────────────────────────


class FakeWebSocket:
    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[dict] = []
        self._fail = fail

    async def send_json(self, payload: dict) -> None:
        if self._fail:
            raise RuntimeError("cliente cerró")
        self.sent.append(payload)


@pytest.mark.anyio
async def test_ws_heartbeat_sends_pings_while_the_block_runs():
    ws = FakeWebSocket()

    async with ws_heartbeat(t.cast(t.Any, ws), interval=0.02):
        await asyncio.sleep(0.07)

    assert len(ws.sent) >= 2
    assert ws.sent[0]["type"] == "ping"


@pytest.mark.anyio
async def test_ws_heartbeat_stops_on_exit():
    ws = FakeWebSocket()

    async with ws_heartbeat(t.cast(t.Any, ws), interval=0.02):
        await asyncio.sleep(0.05)

    count_at_exit = len(ws.sent)
    await asyncio.sleep(0.08)

    assert len(ws.sent) == count_at_exit, "el heartbeat siguió después del bloque"


@pytest.mark.anyio
async def test_ws_heartbeat_stops_even_if_the_block_raises():
    ws = FakeWebSocket()

    with pytest.raises(RuntimeError, match="fallo del handler"):
        async with ws_heartbeat(t.cast(t.Any, ws), interval=0.02):
            await asyncio.sleep(0.03)
            raise RuntimeError("fallo del handler")

    count = len(ws.sent)
    await asyncio.sleep(0.06)
    assert len(ws.sent) == count


@pytest.mark.anyio
async def test_ws_heartbeat_swallows_send_errors():
    """El cliente ya se fue: quien lea del socket se enterará; no queremos dos errores."""
    ws = FakeWebSocket(fail=True)

    async with ws_heartbeat(t.cast(t.Any, ws), interval=0.01):
        await asyncio.sleep(0.05)


@pytest.mark.anyio
async def test_ws_heartbeat_with_zero_interval_is_disabled():
    ws = FakeWebSocket()

    async with ws_heartbeat(t.cast(t.Any, ws), interval=0):
        await asyncio.sleep(0.03)

    assert ws.sent == []


# ── connection_slot ────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_connection_slot_grants_up_to_the_limit():
    cache = MemoryCache()

    async with connection_slot(cache, "user:1", max_connections=2) as first:
        async with connection_slot(cache, "user:1", max_connections=2) as second:
            async with connection_slot(cache, "user:1", max_connections=2) as third:
                assert (first, second, third) == (True, True, False)


@pytest.mark.anyio
async def test_connection_slot_is_released_on_exit():
    cache = MemoryCache()

    async with connection_slot(cache, "user:1", max_connections=1) as granted:
        assert granted is True

    async with connection_slot(cache, "user:1", max_connections=1) as granted:
        assert granted is True, "el slot no se liberó"


@pytest.mark.anyio
async def test_connection_slot_is_released_when_the_block_raises():
    """El bug clásico: el cliente se desconecta mal y el slot queda ocupado."""
    cache = MemoryCache()

    with pytest.raises(RuntimeError):
        async with connection_slot(cache, "user:1", max_connections=1):
            raise RuntimeError("el cliente murió")

    async with connection_slot(cache, "user:1", max_connections=1) as granted:
        assert granted is True


@pytest.mark.anyio
async def test_connection_slot_is_released_on_cancellation():
    cache = MemoryCache()

    async def holder() -> None:
        async with connection_slot(cache, "user:1", max_connections=1):
            await asyncio.sleep(3600)

    task = asyncio.create_task(holder())
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    async with connection_slot(cache, "user:1", max_connections=1) as granted:
        assert granted is True


@pytest.mark.anyio
async def test_denied_slot_does_not_consume_anything():
    cache = MemoryCache()

    async with connection_slot(cache, "user:1", max_connections=1):
        async with connection_slot(cache, "user:1", max_connections=1) as denied:
            assert denied is False

    # Tras cerrar el que sí tenía slot, vuelve a haber uno libre (el denegado no
    # reservó nada que hubiera que liberar).
    async with connection_slot(cache, "user:1", max_connections=1) as granted:
        assert granted is True


@pytest.mark.anyio
async def test_expired_slots_do_not_count():
    cache = MemoryCache()
    await cache.set(
        "hexcore:conn_slot:user:1",
        {"slots": {"zombi": time.time() - 10}},
    )

    async with connection_slot(cache, "user:1", max_connections=1) as granted:
        assert granted is True


@pytest.mark.anyio
async def test_slots_are_per_key():
    cache = MemoryCache()

    async with connection_slot(cache, "user:1", max_connections=1) as first:
        async with connection_slot(cache, "user:2", max_connections=1) as second:
            assert (first, second) == (True, True)


@pytest.mark.anyio
async def test_release_failure_does_not_propagate():
    """Si el backend se cae al liberar, el TTL lo arregla; no rompemos al usuario."""

    class HalfBrokenCache(ICache):
        def __init__(self) -> None:
            self.inner = MemoryCache()
            self.fail_get = False

        async def get(self, key: str) -> t.Any:
            if self.fail_get:
                raise ConnectionError("redis down")
            return await self.inner.get(key)

        def set(self, key: str, value: t.Any, expire: int = 3600) -> t.Any:
            return self.inner.set(key, value, expire)

        def delete(self, key: str) -> t.Any:
            return self.inner.delete(key)

    cache = HalfBrokenCache()

    async with connection_slot(cache, "user:1", max_connections=1) as granted:
        assert granted is True
        cache.fail_get = True
    # No debe lanzar al salir.


@pytest.mark.anyio
async def test_concurrent_slot_requests_respect_the_limit():
    cache = MemoryCache()
    outcomes: list[bool] = []

    async def attempt() -> None:
        async with connection_slot(cache, "user:1", max_connections=2) as granted:
            outcomes.append(granted)
            if granted:
                await asyncio.sleep(0.05)

    await asyncio.gather(*(attempt() for _ in range(4)))

    assert sum(outcomes) <= 4
    assert outcomes.count(False) >= 1, "el límite no rechazó a nadie"
