"""
F12: rate limiting como dependencia, sobre `ICache`.

Dos cosas que la implementación de la app real no hacía y que aquí son requisito:
`Retry-After` en el 429, y una política **explícita** para cuando el backend está caído
en vez de una decisión enterrada en un `except`.
"""
from __future__ import annotations

import time
import typing as t

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from hexcore.infrastructure.api.rate_limit import (  # noqa: E402
    client_ip_key,
    forwarded_ip_key,
    rate_limit,
)
from hexcore.infrastructure.cache import ICache  # noqa: E402
from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


class BrokenCache(ICache):
    async def get(self, key: str) -> t.Any:
        raise ConnectionError("redis down")

    def set(self, key: str, value: t.Any, expire: int = 3600) -> t.Any:
        raise ConnectionError("redis down")

    def delete(self, key: str) -> t.Any:
        raise ConnectionError("redis down")


def _app(**kwargs) -> FastAPI:
    app = FastAPI()
    limiter = rate_limit(**kwargs)

    @app.get("/limited", dependencies=[Depends(limiter)])
    async def limited() -> dict[str, bool]:
        return {"ok": True}

    return app


# ── Ventana básica ─────────────────────────────────────────────────────────────


def test_requests_within_the_limit_pass():
    app = _app(limit=3, window_seconds=60, cache=MemoryCache())

    with TestClient(app) as client:
        statuses = [client.get("/limited").status_code for _ in range(3)]

    assert statuses == [200, 200, 200]


def test_request_over_the_limit_gets_429():
    app = _app(limit=2, window_seconds=60, cache=MemoryCache())

    with TestClient(app) as client:
        client.get("/limited")
        client.get("/limited")
        response = client.get("/limited")

    assert response.status_code == 429


def test_429_carries_retry_after():
    """La implementación de la app real no lo hacía; sin esto el cliente adivina."""
    app = _app(limit=1, window_seconds=60, cache=MemoryCache())

    with TestClient(app) as client:
        client.get("/limited")
        response = client.get("/limited")

    assert response.status_code == 429
    retry_after = int(response.headers["Retry-After"])
    assert 1 <= retry_after <= 61


def test_window_expiry_resets_the_counter():
    cache = MemoryCache()
    app = _app(limit=1, window_seconds=60, cache=cache)

    with TestClient(app) as client:
        assert client.get("/limited").status_code == 200
        assert client.get("/limited").status_code == 429

        # Envejecemos la ventana: el vencimiento se evalúa en el limiter, no en el
        # backend, precisamente porque MemoryCache ignora `expire`.
        key = next(iter(cache.cache))
        cache.cache[key]["reset_at"] = time.time() - 1

        assert client.get("/limited").status_code == 200


def test_memory_cache_ignores_expire_so_the_limiter_must_not_rely_on_it():
    """Documenta por qué la ventana lleva su propio `reset_at`."""
    cache = MemoryCache()

    import asyncio

    asyncio.run(cache.set("k", {"v": 1}, expire=1))
    assert asyncio.run(cache.get("k")) == {"v": 1}


# ── Claves ─────────────────────────────────────────────────────────────────────


def test_different_keys_have_independent_budgets():
    cache = MemoryCache()
    app = FastAPI()
    limiter = rate_limit(
        limit=1,
        window_seconds=60,
        cache=cache,
        key=lambda request: request.headers.get("X-User", "anon"),
    )

    @app.get("/limited", dependencies=[Depends(limiter)])
    async def limited() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/limited", headers={"X-User": "a"}).status_code == 200
        assert client.get("/limited", headers={"X-User": "b"}).status_code == 200
        assert client.get("/limited", headers={"X-User": "a"}).status_code == 429


def test_client_ip_key_ignora_forwarded_for():
    """
    Fase 0 (seguridad): `client_ip_key` ya **no** confía en `X-Forwarded-For`.

    Antes lo prefería sin condiciones, y como el header lo escribe el cliente, mandar un
    valor distinto en cada petición daba un bucket nuevo en cada petición: el límite de
    login era un no-op. La versión anterior de este test afirmaba el comportamiento
    vulnerable, así que se invierte.
    """
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(request: Request) -> dict[str, str]:
        return {"key": client_ip_key(request)}

    with TestClient(app) as client:
        body = client.get(
            "/whoami", headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1"}
        ).json()

    assert body["key"] != "203.0.113.5"


def test_forwarded_for_spoofeado_no_crea_buckets_nuevos():
    """El ataque concreto: un XFF distinto por petición no debe esquivar el límite."""
    cache = MemoryCache()
    app = FastAPI()
    limiter = rate_limit(limit=2, cache=cache, key=client_ip_key)

    @app.get("/login", dependencies=[Depends(limiter)])
    async def login() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/login", headers={"X-Forwarded-For": "1.1.1.1"}).status_code == 200
        assert client.get("/login", headers={"X-Forwarded-For": "2.2.2.2"}).status_code == 200
        # Con el bug, ésta era otra IP y pasaba.
        assert client.get("/login", headers={"X-Forwarded-For": "3.3.3.3"}).status_code == 429


def test_forwarded_ip_key_honra_el_header_desde_un_proxy_declarado():
    app = FastAPI()
    key = forwarded_ip_key(trusted_proxies=["10.9.0.1"])

    @app.get("/whoami")
    async def whoami(request: Request) -> dict[str, str]:
        return {"key": key(request)}

    with TestClient(app, client=("10.9.0.1", 4000)) as client:
        body = client.get(
            "/whoami", headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1"}
        ).json()

    # `trust_hops=1` -> el cliente es el último elemento, que es la única parte del
    # header que el cliente no controla.
    assert body["key"] == "10.0.0.1"


def test_forwarded_ip_key_cuenta_los_saltos_desde_la_derecha():
    app = FastAPI()
    key = forwarded_ip_key(trusted_proxies=["10.9.0.1"], trust_hops=2)

    @app.get("/whoami")
    async def whoami(request: Request) -> dict[str, str]:
        return {"key": key(request)}

    with TestClient(app, client=("10.9.0.1", 4000)) as client:
        body = client.get(
            "/whoami",
            headers={"X-Forwarded-For": "9.9.9.9, 203.0.113.5, 10.0.0.1"},
        ).json()

    assert body["key"] == "203.0.113.5"


def test_forwarded_ip_key_ignora_el_header_de_un_par_no_declarado():
    app = FastAPI()
    key = forwarded_ip_key(trusted_proxies=["10.0.0.0/8"])

    @app.get("/whoami")
    async def whoami(request: Request) -> dict[str, str]:
        return {"key": key(request)}

    # El par es 198.51.100.7, que NO está en trusted_proxies.
    with TestClient(app, client=("198.51.100.7", 4000)) as client:
        body = client.get("/whoami", headers={"X-Forwarded-For": "203.0.113.5"}).json()

    assert body["key"] != "203.0.113.5"


def test_forwarded_ip_key_rechaza_configuracion_invalida():
    with pytest.raises(ValueError, match="client_ip_key"):
        forwarded_ip_key(trusted_proxies=[])

    with pytest.raises(ValueError, match="trust_hops"):
        forwarded_ip_key(trusted_proxies=["10.0.0.0/8"], trust_hops=0)

    with pytest.raises(ValueError, match="CIDR"):
        forwarded_ip_key(trusted_proxies=["no-es-una-ip"])


@pytest.mark.anyio
async def test_el_conteo_concurrente_no_se_pasa_del_limite():
    """
    El read-modify-write dejaba pasar un pico entero.

    Antes: `get()` → comparar → `set()` con un `await` en el medio, así que 20 corutinas
    leían `count=0` y pasaban las 20 con `limit=3`. `MemoryCache.incr_window` no cede el
    control entre la lectura y la escritura, así que ahora pasan exactamente 3.
    """
    import anyio

    cache = MemoryCache()
    limiter = rate_limit(limit=3, window_seconds=60, cache=cache, key=lambda r: "fijo")

    class _FakeRequest:
        headers: dict[str, str] = {}
        client = None

    aceptadas = 0

    async def intentar() -> None:
        nonlocal aceptadas
        try:
            await limiter(t.cast(t.Any, _FakeRequest()))
            aceptadas += 1
        except HTTPException:
            pass

    async with anyio.create_task_group() as tg:
        for _ in range(20):
            tg.start_soon(intentar)

    assert aceptadas == 3


def test_client_ip_key_falls_back_to_the_socket_address():
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(request: Request) -> dict[str, str]:
        return {"key": client_ip_key(request)}

    with TestClient(app) as client:
        assert client.get("/whoami").json()["key"] != ""


def test_namespace_isolates_two_limiters_on_the_same_key():
    cache = MemoryCache()
    app = FastAPI()
    per_route_a = rate_limit(limit=1, cache=cache, namespace="a")
    per_route_b = rate_limit(limit=1, cache=cache, namespace="b")

    @app.get("/a", dependencies=[Depends(per_route_a)])
    async def a() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/b", dependencies=[Depends(per_route_b)])
    async def b() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        assert client.get("/a").status_code == 200
        assert client.get("/b").status_code == 200
        assert client.get("/a").status_code == 429


# ── Backend caído: política explícita ──────────────────────────────────────────


def test_backend_error_allows_by_default():
    app = _app(limit=1, cache=BrokenCache())

    with TestClient(app) as client:
        assert client.get("/limited").status_code == 200
        assert client.get("/limited").status_code == 200


def test_backend_error_can_deny_instead():
    app = _app(limit=1, cache=BrokenCache(), on_backend_error="deny")

    with TestClient(app) as client:
        response = client.get("/limited")

    assert response.status_code == 429
    assert "Retry-After" in response.headers


def test_backend_error_logs_critical_when_denying(caplog):
    import logging

    app = _app(limit=1, cache=BrokenCache(), on_backend_error="deny")

    with caplog.at_level(logging.CRITICAL):
        with TestClient(app) as client:
            client.get("/limited")

    assert any(r.levelno == logging.CRITICAL for r in caplog.records)


# ── Validación de argumentos ───────────────────────────────────────────────────


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": -1}])
def test_invalid_limit_is_rejected(kwargs):
    with pytest.raises(ValueError, match="limit"):
        rate_limit(**kwargs)


def test_invalid_window_is_rejected():
    with pytest.raises(ValueError, match="window_seconds"):
        rate_limit(limit=1, window_seconds=0)
