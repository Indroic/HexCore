"""
F11 (middlewares de serie) y F13 (composición de routers).
"""
from __future__ import annotations

import logging

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")  # requerido por starlette.testclient

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from hexcore.infrastructure.api.middlewares import (  # noqa: E402
    RequestIDLogFilter,
    RequestIDMiddleware,
    TimingMiddleware,
    get_request_id,
    install_request_id_logging,
)
from hexcore.infrastructure.api.routing import build_root_router, mount_routers  # noqa: E402


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── F11: RequestIDMiddleware ───────────────────────────────────────────────────


def _app_with_request_id(**middleware_kwargs) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware, **middleware_kwargs)

    @app.get("/echo")
    async def echo() -> dict[str, str]:
        return {"request_id": get_request_id()}

    return app


def test_request_id_is_generated_when_absent():
    with TestClient(_app_with_request_id()) as client:
        response = client.get("/echo")

    assert response.status_code == 200
    header_id = response.headers["X-Request-ID"]
    assert header_id
    assert response.json()["request_id"] == header_id


def test_incoming_request_id_is_reused():
    """Romper la cadena del gateway es perder la traza."""
    with TestClient(_app_with_request_id()) as client:
        response = client.get("/echo", headers={"X-Request-ID": "from-gateway"})

    assert response.headers["X-Request-ID"] == "from-gateway"
    assert response.json()["request_id"] == "from-gateway"


def test_request_id_is_available_in_request_state():
    """Para quien prefiera `request.state` al ContextVar."""
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/state")
    async def from_state(request: Request) -> dict[str, str]:
        return {"request_id": request.state.request_id}

    with TestClient(app) as client:
        response = client.get("/state", headers={"X-Request-ID": "abc"})

    assert response.json()["request_id"] == "abc"


def test_custom_header_name_and_generator():
    app = _app_with_request_id(header_name="X-Trace", generator=lambda: "fixed-id")

    with TestClient(app) as client:
        response = client.get("/echo")

    assert response.headers["X-Trace"] == "fixed-id"


def test_context_var_is_reset_between_requests():
    app = _app_with_request_id()

    with TestClient(app) as client:
        first = client.get("/echo", headers={"X-Request-ID": "one"}).json()["request_id"]
        second = client.get("/echo", headers={"X-Request-ID": "two"}).json()["request_id"]

    assert (first, second) == ("one", "two")
    # Fuera de todo request el default vuelve a estar.
    assert get_request_id() == "-"


def test_request_id_header_is_set_even_on_error():
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/boom")
    async def boom() -> None:
        raise HTTPException(status_code=418, detail="teapot")

    with TestClient(app) as client:
        response = client.get("/boom", headers={"X-Request-ID": "err-id"})

    assert response.status_code == 418
    assert response.headers["X-Request-ID"] == "err-id"


# ── F11: TimingMiddleware ──────────────────────────────────────────────────────


def test_timing_middleware_adds_the_header():
    app = FastAPI()
    app.add_middleware(TimingMiddleware)

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/ping")

    assert float(response.headers["X-Response-Time-ms"]) >= 0


def test_timing_middleware_custom_header():
    app = FastAPI()
    app.add_middleware(TimingMiddleware, header_name="X-Took")

    @app.get("/ping")
    async def ping() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/ping")

    assert "X-Took" in response.headers


# ── F11: el filtro de logging (la mitad del valor) ─────────────────────────────


def test_log_filter_injects_the_request_id(caplog):
    logger = logging.getLogger("hexcore.test.reqid")
    app = FastAPI()
    app.add_middleware(RequestIDMiddleware)

    @app.get("/log")
    async def log_something() -> dict[str, bool]:
        logger.warning("algo pasó")
        return {"ok": True}

    handler = logging.StreamHandler()
    handler.addFilter(RequestIDLogFilter())
    logger.addHandler(handler)
    try:
        with caplog.at_level(logging.WARNING, logger="hexcore.test.reqid"):
            for record_handler in caplog.handler, handler:
                record_handler.addFilter(RequestIDLogFilter())
            with TestClient(app) as client:
                client.get("/log", headers={"X-Request-ID": "traced"})
    finally:
        logger.removeHandler(handler)

    records = [r for r in caplog.records if r.name == "hexcore.test.reqid"]
    assert records
    assert getattr(records[0], "request_id") == "traced"


def test_log_filter_outside_a_request_uses_the_default():
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "m", None, None)
    RequestIDLogFilter().filter(record)

    assert getattr(record, "request_id") == "-"


def test_install_request_id_logging_is_idempotent():
    logger = logging.getLogger("hexcore.test.install")
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:
        install_request_id_logging(logger)
        install_request_id_logging(logger)

        filters = [f for f in handler.filters if isinstance(f, RequestIDLogFilter)]
        assert len(filters) == 1
    finally:
        logger.removeHandler(handler)


def test_install_request_id_logging_can_set_the_format():
    logger = logging.getLogger("hexcore.test.fmt")
    handler = logging.StreamHandler()
    logger.addHandler(handler)
    try:
        install_request_id_logging(logger, fmt="[%(request_id)s] %(message)s")

        record = logging.LogRecord("x", logging.INFO, __file__, 1, "hola", None, None)
        RequestIDLogFilter().filter(record)
        assert handler.formatter is not None
        assert handler.formatter.format(record) == "[-] hola"
    finally:
        logger.removeHandler(handler)


# ── F13: build_root_router / mount_routers ─────────────────────────────────────


def _child(name: str) -> APIRouter:
    router = APIRouter()

    @router.get("/items")
    async def items() -> dict[str, str]:
        return {"from": name}

    return router


def test_build_root_router_mounts_children_under_their_prefixes():
    root = build_root_router(
        "/admin", {"/catalog": _child("catalog"), "/reports": _child("reports")}
    )
    app = FastAPI()
    app.include_router(root)

    with TestClient(app) as client:
        assert client.get("/admin/catalog/items").json() == {"from": "catalog"}
        assert client.get("/admin/reports/items").json() == {"from": "reports"}


def test_build_root_router_applies_shared_dependencies():
    calls: list[str] = []

    async def guard() -> None:
        calls.append("checked")

    root = build_root_router(
        "/secure", {"/a": _child("a")}, dependencies=[Depends(guard)]
    )
    app = FastAPI()
    app.include_router(root)

    with TestClient(app) as client:
        assert client.get("/secure/a/items").status_code == 200

    assert calls == ["checked"]


def test_build_root_router_shared_dependency_can_reject():
    async def deny() -> None:
        raise HTTPException(status_code=403, detail="nope")

    root = build_root_router("/secure", {"/a": _child("a")}, dependencies=[Depends(deny)])
    app = FastAPI()
    app.include_router(root)

    with TestClient(app) as client:
        assert client.get("/secure/a/items").status_code == 403


def test_build_root_router_applies_tags():
    root = build_root_router("/tagged", {"/a": _child("a")}, tags=["admin"])
    app = FastAPI()
    app.include_router(root)

    schema = app.openapi()
    assert schema["paths"]["/tagged/a/items"]["get"]["tags"] == ["admin"]


def test_build_root_router_supports_empty_child_prefix():
    root = build_root_router("/root", {"": _child("direct")})
    app = FastAPI()
    app.include_router(root)

    with TestClient(app) as client:
        assert client.get("/root/items").json() == {"from": "direct"}


def test_mount_routers_accepts_plain_routers_and_tuples():
    app = FastAPI()
    mount_routers(
        app,
        [
            _child("plain"),
            (_child("with-kwargs"), {"prefix": "/v2", "tags": ["v2"]}),
        ],
    )

    with TestClient(app) as client:
        assert client.get("/items").json() == {"from": "plain"}
        assert client.get("/v2/items").json() == {"from": "with-kwargs"}

    schema = app.openapi()
    assert schema["paths"]["/v2/items"]["get"]["tags"] == ["v2"]


def test_mount_routers_with_an_empty_list_is_a_noop():
    app = FastAPI()
    mount_routers(app, [])

    with TestClient(app) as client:
        assert client.get("/nope").status_code == 404
