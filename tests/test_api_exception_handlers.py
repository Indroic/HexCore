"""
F5: mapeo de excepciones de dominio a HTTP.

El caso que motiva el default de `ValueError` → 422: hasta ahora sólo se capturaba
**dentro** de `build_query_endpoint`, así que una query construida a mano devolvía 500.
"""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from hexcore.domain.cqrs.exceptions import DeserializationError, HandlerNotFoundError  # noqa: E402
from hexcore.domain.exceptions import InactiveEntityException  # noqa: E402
from hexcore.infrastructure.api.exception_handlers import (  # noqa: E402
    DEFAULT_EXCEPTION_STATUS_MAP,
    register_exception_handlers,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class NotFoundError(Exception):
    """Excepción de dominio de una app cualquiera."""


def _app(**kwargs) -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app, **kwargs)

    @app.get("/inactive")
    async def inactive() -> None:
        raise InactiveEntityException()

    @app.get("/bad-payload")
    async def bad_payload() -> None:
        raise DeserializationError("payload corrupto")

    @app.get("/no-handler")
    async def no_handler() -> None:
        raise HandlerNotFoundError(int)

    @app.get("/bad-field")
    async def bad_field() -> None:
        raise ValueError("campo 'nope' no existe")

    @app.get("/not-found")
    async def not_found() -> None:
        raise NotFoundError("el ticket 42 no existe")

    return app


def _client(app: FastAPI) -> TestClient:
    # raise_server_exceptions=False para que las excepciones no mapeadas lleguen como
    # 500 en vez de propagarse al test.
    return TestClient(app, raise_server_exceptions=False)


def test_default_mapping_covers_the_domain_exceptions():
    assert DEFAULT_EXCEPTION_STATUS_MAP[InactiveEntityException] == 409
    assert DEFAULT_EXCEPTION_STATUS_MAP[DeserializationError] == 400
    assert DEFAULT_EXCEPTION_STATUS_MAP[HandlerNotFoundError] == 501
    assert DEFAULT_EXCEPTION_STATUS_MAP[ValueError] == 422


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("/inactive", 409),
        ("/bad-payload", 400),
        ("/no-handler", 501),
        ("/bad-field", 422),
    ],
)
def test_default_handlers_map_to_the_documented_status(path, expected):
    with _client(_app()) as client:
        assert client.get(path).status_code == expected


def test_response_body_carries_detail_and_error_name():
    with _client(_app()) as client:
        body = client.get("/bad-field").json()

    assert body["detail"] == "campo 'nope' no existe"
    assert body["error"] == "ValueError"


def test_custom_mapping_is_merged_with_the_default():
    with _client(_app(mapping={NotFoundError: 404})) as client:
        assert client.get("/not-found").status_code == 404
        # Los defaults siguen ahí.
        assert client.get("/inactive").status_code == 409


def test_custom_mapping_can_override_a_default():
    with _client(_app(mapping={InactiveEntityException: 410})) as client:
        assert client.get("/inactive").status_code == 410


def test_include_detail_false_hides_the_message():
    with _client(_app(include_detail=False)) as client:
        body = client.get("/bad-field").json()

    assert "nope" not in body["detail"]
    assert body["error"] == "ValueError"


def test_include_detail_accepts_a_callable():
    with _client(
        _app(include_detail=lambda exc: f"[{type(exc).__name__}] redactado")
    ) as client:
        body = client.get("/bad-field").json()

    assert body["detail"] == "[ValueError] redactado"


def test_unmapped_exception_is_still_a_500():
    with _client(_app()) as client:
        assert client.get("/not-found").status_code == 500


def test_a_subclass_wins_over_its_parent():
    """
    `DeserializationError` hereda de `CQRSError`, no de `ValueError`, pero el orden de
    registro tiene que respetar la especificidad si alguien mapea una jerarquía.
    """
    class SpecificValueError(ValueError):
        pass

    app = FastAPI()
    register_exception_handlers(app, mapping={SpecificValueError: 418})

    @app.get("/specific")
    async def specific() -> None:
        raise SpecificValueError("soy más específica")

    @app.get("/generic")
    async def generic() -> None:
        raise ValueError("soy genérica")

    with _client(app) as client:
        assert client.get("/specific").status_code == 418
        assert client.get("/generic").status_code == 422


# ── headers_for: los status que exigen un header por especificación ────────────


class _TokenInvalido(Exception):
    pass


def test_headers_for_agrega_www_authenticate_en_un_401():
    """
    Un 401 sin `WWW-Authenticate` viola RFC 6750 §3, y `_build_handler` no podía emitir
    headers en absoluto: el mapa de excepciones sólo lleva un entero.
    """
    def cabeceras(exc: Exception) -> dict[str, str]:
        if isinstance(exc, _TokenInvalido):
            return {"WWW-Authenticate": 'Bearer error="invalid_token"'}
        return {}

    app = FastAPI()
    register_exception_handlers(
        app, mapping={_TokenInvalido: 401}, headers_for=cabeceras
    )

    @app.get("/protegido")
    async def protegido() -> None:
        raise _TokenInvalido("token vencido")

    response = TestClient(app, raise_server_exceptions=False).get("/protegido")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == 'Bearer error="invalid_token"'
    assert response.json()["error"] == "_TokenInvalido"


def test_headers_for_vacio_no_agrega_nada():
    app = FastAPI()
    register_exception_handlers(
        app, mapping={_TokenInvalido: 401}, headers_for=lambda exc: {}
    )

    @app.get("/protegido")
    async def protegido() -> None:
        raise _TokenInvalido("nope")

    response = TestClient(app, raise_server_exceptions=False).get("/protegido")

    assert response.status_code == 401
    assert "WWW-Authenticate" not in response.headers


def test_un_headers_for_que_explota_no_arruina_la_respuesta():
    """El header es accesorio: el status ya está decidido y no puede degradar a 500."""
    def cabeceras(exc: Exception) -> dict[str, str]:
        raise RuntimeError("bug en la fábrica de headers")

    app = FastAPI()
    register_exception_handlers(
        app, mapping={_TokenInvalido: 401}, headers_for=cabeceras
    )

    @app.get("/protegido")
    async def protegido() -> None:
        raise _TokenInvalido("nope")

    response = TestClient(app, raise_server_exceptions=False).get("/protegido")

    assert response.status_code == 401


def test_create_app_reenvia_exception_headers():
    from hexcore.infrastructure.api.app import create_app

    app = create_app(
        exception_mapping={_TokenInvalido: 401},
        exception_headers=lambda exc: {"WWW-Authenticate": "Bearer"},
    )

    @app.get("/protegido")
    async def protegido() -> None:
        raise _TokenInvalido("nope")

    response = TestClient(app, raise_server_exceptions=False).get("/protegido")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
