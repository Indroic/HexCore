"""
F1 (`create_app`) y F4 (`build_lifespan`).

El criterio de simplicidad del plan: `create_app()` **sin ningún argumento** debe dar una
app usable, con `/health` respondiendo 200.
"""
from __future__ import annotations

import typing as t

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import APIRouter  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from hexcore.config import LazyConfig  # noqa: E402
from hexcore.infrastructure.api.app import AppFeatures, create_app  # noqa: E402
from hexcore.infrastructure.api.health import Probe  # noqa: E402
from hexcore.infrastructure.api.lifespan import (  # noqa: E402
    CallableStep,
    EventBusStep,
    build_lifespan,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── F1: cero configuración ─────────────────────────────────────────────────────


def test_create_app_with_no_arguments_serves_health():
    """El criterio de simplicidad de la fase 3."""
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_title_and_version_come_from_config():
    app = create_app()
    config = LazyConfig.get_config()

    assert app.title == config.app_title
    assert app.version == config.app_version


def test_fastapi_kwargs_override_the_config_defaults():
    app = create_app(title="Red API", version="2.0.0")

    assert app.title == "Red API"
    assert app.version == "2.0.0"


def test_cors_is_wired_from_config():
    app = create_app()

    with TestClient(app) as client:
        response = client.get(
            "/health", headers={"Origin": "http://example.com"}
        )

    assert "access-control-allow-origin" in response.headers


def test_cors_can_be_disabled():
    app = create_app(features=AppFeatures(cors=False))

    with TestClient(app) as client:
        response = client.get("/health", headers={"Origin": "http://example.com"})

    assert "access-control-allow-origin" not in response.headers


def test_request_id_middleware_is_wired():
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert response.headers["X-Request-ID"]


def test_timing_middleware_is_wired():
    with TestClient(create_app()) as client:
        response = client.get("/health")

    assert float(response.headers["X-Response-Time-ms"]) >= 0


def test_middlewares_can_be_disabled():
    app = create_app(features=AppFeatures(request_id=False, timing=False))

    with TestClient(app) as client:
        response = client.get("/health")

    assert "X-Request-ID" not in response.headers
    assert "X-Response-Time-ms" not in response.headers


def test_health_can_be_disabled():
    app = create_app(features=AppFeatures(health=False))

    with TestClient(app) as client:
        assert client.get("/health").status_code == 404


def test_exception_handlers_are_wired():
    app = create_app()

    @app.get("/boom")
    async def boom() -> None:
        raise ValueError("campo inválido")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/boom").status_code == 422


def test_exception_handlers_can_be_disabled():
    app = create_app(features=AppFeatures(exception_handlers=False))

    @app.get("/boom")
    async def boom() -> None:
        raise ValueError("campo inválido")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/boom").status_code == 500


def test_custom_exception_mapping_is_applied():
    class Missing(Exception):
        pass

    app = create_app(exception_mapping={Missing: 404})

    @app.get("/missing")
    async def missing() -> None:
        raise Missing("no está")

    with TestClient(app, raise_server_exceptions=False) as client:
        assert client.get("/missing").status_code == 404


def test_routers_are_mounted():
    router = APIRouter()

    @router.get("/items")
    async def items() -> dict[str, bool]:
        return {"ok": True}

    app = create_app(routers=[(router, {"prefix": "/api"})])

    with TestClient(app) as client:
        assert client.get("/api/items").json() == {"ok": True}


def test_health_probes_are_forwarded():
    async def boom() -> None:
        raise ConnectionError("caída")

    app = create_app(health_probes=[Probe("sql", boom)])

    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/health/ready").status_code == 503


def test_docs_follow_the_debug_flag():
    app = create_app()

    # El default de ServerConfig es debug=True.
    assert app.docs_url == "/docs"

    app_no_debug = create_app(debug=False, docs_url=None)
    assert app_no_debug.docs_url is None


# ── F4: build_lifespan ─────────────────────────────────────────────────────────


class RecordingStep:
    def __init__(
        self,
        name: str,
        log: list[str],
        *,
        fail: bool = False,
        on_error: str | None = None,
    ) -> None:
        self.name = name
        self.on_error = on_error
        self._log = log
        self._fail = fail

    async def start(self) -> None:
        if self._fail:
            self._log.append(f"start:{self.name}:FAIL")
            raise RuntimeError(f"{self.name} falló")
        self._log.append(f"start:{self.name}")

    async def stop(self) -> None:
        self._log.append(f"stop:{self.name}")


def _app_with(lifespan) -> t.Any:
    return create_app(lifespan=lifespan, features=AppFeatures())


def test_steps_start_in_declared_order():
    log: list[str] = []
    lifespan = build_lifespan(
        RecordingStep("a", log), RecordingStep("b", log), RecordingStep("c", log)
    )

    with TestClient(_app_with(lifespan)):
        pass

    assert log[:3] == ["start:a", "start:b", "start:c"]


def test_teardown_runs_in_reverse_order():
    log: list[str] = []
    lifespan = build_lifespan(
        RecordingStep("a", log), RecordingStep("b", log), RecordingStep("c", log)
    )

    with TestClient(_app_with(lifespan)):
        pass

    assert log[3:] == ["stop:c", "stop:b", "stop:a"]


def test_a_failing_step_stops_the_previous_ones_in_reverse_order():
    """El requisito central de F4."""
    log: list[str] = []
    lifespan = build_lifespan(
        RecordingStep("a", log),
        RecordingStep("b", log),
        RecordingStep("boom", log, fail=True),
        RecordingStep("never", log),
    )

    with pytest.raises(RuntimeError, match="boom falló"):
        with TestClient(_app_with(lifespan)):
            pass  # pragma: no cover

    assert log == [
        "start:a",
        "start:b",
        "start:boom:FAIL",
        "stop:b",
        "stop:a",
    ]
    assert "start:never" not in log


def test_a_step_that_never_started_is_not_stopped():
    log: list[str] = []
    lifespan = build_lifespan(
        RecordingStep("boom", log, fail=True), RecordingStep("never", log)
    )

    with pytest.raises(RuntimeError):
        with TestClient(_app_with(lifespan)):
            pass  # pragma: no cover

    assert "stop:boom" not in log
    assert "stop:never" not in log


def test_on_error_warn_does_not_propagate():
    log: list[str] = []
    lifespan = build_lifespan(
        RecordingStep("a", log),
        RecordingStep("warmup", log, fail=True, on_error="warn"),
        RecordingStep("c", log),
    )

    with TestClient(_app_with(lifespan)):
        pass

    assert "start:c" in log, "un step con on_error='warn' abortó el arranque"


def test_global_on_error_warn_applies_to_every_step():
    log: list[str] = []
    lifespan = build_lifespan(
        RecordingStep("boom", log, fail=True),
        RecordingStep("after", log),
        on_error="warn",
    )

    with TestClient(_app_with(lifespan)):
        pass

    assert "start:after" in log


def test_per_step_policy_overrides_the_global_one():
    log: list[str] = []
    lifespan = build_lifespan(
        RecordingStep("critical", log, fail=True, on_error="raise"),
        on_error="warn",
    )

    with pytest.raises(RuntimeError, match="critical falló"):
        with TestClient(_app_with(lifespan)):
            pass  # pragma: no cover


def test_a_failing_teardown_does_not_stop_the_others():
    log: list[str] = []

    class BadStopStep(RecordingStep):
        async def stop(self) -> None:
            raise RuntimeError("teardown roto")

    lifespan = build_lifespan(RecordingStep("a", log), BadStopStep("bad", log))

    with TestClient(_app_with(lifespan)):
        pass

    assert "stop:a" in log, "un teardown roto impidió los siguientes"


def test_steps_without_stop_are_allowed():
    log: list[str] = []

    class StartOnlyStep:
        name = "start-only"

        async def start(self) -> None:
            log.append("start")

    lifespan = build_lifespan(StartOnlyStep())

    with TestClient(_app_with(lifespan)):
        pass

    assert log == ["start"]


def test_lifespan_with_no_steps_works():
    with TestClient(_app_with(build_lifespan())) as client:
        assert client.get("/health").status_code == 200


def test_step_durations_are_logged(caplog):
    import logging

    log: list[str] = []
    lifespan = build_lifespan(RecordingStep("timed", log))

    with caplog.at_level(logging.INFO, logger="hexcore.api.lifespan"):
        with TestClient(_app_with(lifespan)):
            pass

    messages = [r.getMessage() for r in caplog.records]
    assert any("step 'timed' arrancó en" in m for m in messages)
    assert any("step 'timed' paró en" in m for m in messages)


# ── F4: steps de serie ─────────────────────────────────────────────────────────


def test_callable_step_wraps_plain_coroutines():
    log: list[str] = []

    async def start() -> None:
        log.append("start")

    async def stop() -> None:
        log.append("stop")

    with TestClient(_app_with(build_lifespan(CallableStep("custom", start, stop)))):
        pass

    assert log == ["start", "stop"]


def test_callable_step_can_be_non_fatal():
    async def boom() -> None:
        raise RuntimeError("el warmup falló")

    lifespan = build_lifespan(CallableStep("warmup", boom, on_error="warn"))

    with TestClient(_app_with(lifespan)) as client:
        assert client.get("/health").status_code == 200


def test_event_bus_step_injects_and_restores_the_bus():
    sentinel = object()
    original = LazyConfig.get_config().event_bus

    seen: list[t.Any] = []

    async def capture() -> None:
        seen.append(LazyConfig.get_config().event_bus)

    lifespan = build_lifespan(
        EventBusStep(sentinel), CallableStep("capture", capture)
    )

    with TestClient(_app_with(lifespan)):
        pass

    assert seen == [sentinel]
    # Se restaura, para que un lifespan en tests no contamine el siguiente.
    assert LazyConfig.get_config().event_bus is original


def test_sql_engine_step_initialises_and_disposes():
    pytest.importorskip("sqlalchemy")
    pytest.importorskip("aiosqlite")

    from hexcore.infrastructure.repositories.orms.sqlalchemy import session as session_module
    from hexcore.infrastructure.api.lifespan import SqlEngineStep

    lifespan = build_lifespan(SqlEngineStep("sqlite+aiosqlite:///:memory:"))

    with TestClient(_app_with(lifespan)):
        assert session_module._engine is not None

    assert session_module._engine is None, "el engine no se cerró al apagar"
