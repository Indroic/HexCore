"""
F16: health checks que de verdad comprueban algo.

Un `/health/detailed` que devuelve `{"status": "ok"}` hardcodeado es peor que no tenerlo:
el orquestador lo lee como "sano" con la BD caída y le sigue mandando tráfico.
"""
from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from hexcore.infrastructure.api.health import (  # noqa: E402
    HealthReport,
    Probe,
    check_health,
    default_probes,
    register_health_routes,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _ok() -> None:
    return None


async def _boom() -> None:
    raise ConnectionError("no responde")


async def _hangs() -> None:
    await asyncio.sleep(10)


# ── Liveness: sin I/O, siempre 200 ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_liveness_does_not_run_any_probe():
    called: list[str] = []

    async def spy() -> None:
        called.append("probed")

    report = await check_health(deep=False, probes=[Probe("spy", spy)])

    assert report.status == "ok"
    assert report.deep is False
    assert report.dependencies == []
    assert called == [], "el liveness sondeó dependencias"


@pytest.mark.anyio
async def test_liveness_http_status_is_200():
    report = await check_health(deep=False)

    assert report.http_status == 200


# ── Readiness: sondea de verdad ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_readiness_reports_ok_when_everything_answers():
    report = await check_health(
        deep=True, probes=[Probe("sql", _ok), Probe("redis", _ok)]
    )

    assert report.status == "ok"
    assert report.deep is True
    assert {d.name for d in report.dependencies} == {"sql", "redis"}
    assert all(d.status == "ok" for d in report.dependencies)


@pytest.mark.anyio
async def test_readiness_is_down_when_a_critical_probe_fails():
    report = await check_health(
        deep=True, probes=[Probe("sql", _boom), Probe("redis", _ok)]
    )

    assert report.status == "down"
    assert report.http_status == 503

    sql = next(d for d in report.dependencies if d.name == "sql")
    assert sql.status == "down"
    assert sql.detail is not None
    assert "ConnectionError" in sql.detail


@pytest.mark.anyio
async def test_a_non_critical_failure_only_degrades():
    """Un cache caído ralentiza, no impide servir."""
    report = await check_health(
        deep=True,
        probes=[Probe("sql", _ok), Probe("redis", _boom, critical=False)],
    )

    assert report.status == "degraded"
    assert report.http_status == 200


@pytest.mark.anyio
async def test_a_probe_that_hangs_times_out_instead_of_hanging_the_check():
    """Un health check que se cuelga es un health check que tumba el deploy."""
    report = await check_health(
        deep=True, probes=[Probe("slow", _hangs, timeout=0.05)]
    )

    assert report.status == "down"
    assert report.dependencies[0].detail is not None
    assert "timeout" in report.dependencies[0].detail


@pytest.mark.anyio
async def test_probes_report_their_latency():
    async def slow_but_ok() -> None:
        await asyncio.sleep(0.02)

    report = await check_health(deep=True, probes=[Probe("slow", slow_but_ok)])

    assert report.dependencies[0].latency_ms >= 15


@pytest.mark.anyio
async def test_probes_run_concurrently():
    async def takes_50ms() -> None:
        await asyncio.sleep(0.05)

    import time

    start = time.perf_counter()
    await check_health(
        deep=True,
        probes=[Probe(f"p{i}", takes_50ms, timeout=1.0) for i in range(4)],
    )
    elapsed = time.perf_counter() - start

    assert elapsed < 0.2, "las sondas corrieron en serie"


@pytest.mark.anyio
async def test_readiness_with_no_probes_is_ok():
    report = await check_health(deep=True, probes=[])

    assert report.status == "ok"
    assert report.deep is True


def test_default_probes_only_includes_what_can_be_probed():
    """No queremos una sonda que siempre pasa porque la dependencia no está."""
    names = {probe.name for probe in default_probes()}

    assert names <= {"sql", "redis", "mongo"}
    for probe in default_probes():
        assert probe.timeout > 0


# ── Rutas ──────────────────────────────────────────────────────────────────────


def test_liveness_route_returns_200_without_probing():
    app = FastAPI()
    register_health_routes(app, probes=[Probe("sql", _boom)])

    with TestClient(app) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["deep"] is False


def test_readiness_route_returns_503_when_a_dependency_is_down():
    app = FastAPI()
    register_health_routes(app, probes=[Probe("sql", _boom)])

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "down"
    assert body["dependencies"][0]["name"] == "sql"


def test_readiness_route_returns_200_when_everything_is_up():
    app = FastAPI()
    register_health_routes(app, probes=[Probe("sql", _ok)])

    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_custom_path():
    app = FastAPI()
    register_health_routes(app, path="/_status", probes=[Probe("sql", _ok)])

    with TestClient(app) as client:
        assert client.get("/_status").status_code == 200
        assert client.get("/_status/ready").status_code == 200
        assert client.get("/health").status_code == 404


def test_health_report_is_serializable():
    report = HealthReport(status="ok")

    assert report.model_dump()["status"] == "ok"
