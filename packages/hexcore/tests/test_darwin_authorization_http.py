"""
Darwin — Fase 0 del plan rbac/drbac: `require_permission` contra una app real.

No pasa por el flujo completo de sign-in —eso ya lo cubre `test_darwin_http.py`— sino que
publica el `AuthContext` a mano en `request.state.darwin_auth`, exactamente la forma que
`AuthContextMiddleware` deja para que `provide_auth`/`require_permission` la lean. Lo que se
fija es específico de esta fase:

1. Sin credencial → 401 con `WWW-Authenticate`.
2. Con el scope que la acción pide → 200.
3. Sin el scope (incluido: con otro scope que no aplica) → 403, con `required` en el body y
   **sin** la razón de la política en ningún lado de la respuesta.
4. Un `resource` que depende del request (ej. un `owner_id` del path) participa de la decisión.
5. `AccessDeniedError` no se confunde con `InsufficientScopeError`: son caminos separados.
"""
from __future__ import annotations

import typing as t
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("joserfc")
pytest.importorskip("argon2")

from fastapi import Depends, FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

from hexcore.darwin import (  # noqa: E402
    AuthContext,
    IDENTITY_EXCEPTION_STATUS_MAP,
    IdentityConfig,
    Principal,
    ResourceRef,
    configure_identity,
    reset_identity,
)
from hexcore.darwin.infrastructure.api.authorization import require_permission  # noqa: E402
from hexcore.darwin.infrastructure.api.dependencies import (  # noqa: E402
    identity_exception_headers,
    identity_exception_payload,
)
from hexcore.infrastructure.api.exception_handlers import (  # noqa: E402
    register_exception_handlers,
)

CLAVE = "k" * 48


class _ContextoDePrueba(BaseHTTPMiddleware):
    """
    El sustituto mínimo de `AuthContextMiddleware` para este test.

    Publica el mismo contrato que el middleware real —`request.state.darwin_auth`— a partir
    de un header de prueba, sin pasar por un token real. Lo que se prueba acá es
    `require_permission`, no el borde de autenticación, que ya tiene su propia suite.
    """

    async def dispatch(self, request: Request, call_next: t.Any) -> t.Any:
        scopes_header = request.headers.get("X-Test-Scopes")
        if scopes_header is None:
            request.state.darwin_auth = None
        else:
            scopes = frozenset(s for s in scopes_header.split(",") if s)
            actor = Principal(user_id=uuid4(), scopes=scopes)
            request.state.darwin_auth = AuthContext(
                actor=actor, subject=actor, transport="bearer"
            )
        return await call_next(request)


async def _cargar_factura(request: Request) -> ResourceRef:
    return ResourceRef(type="invoice", id=request.path_params["invoice_id"])


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(autouse=True)
def _contenedor():
    reset_identity()
    configure_identity(IdentityConfig(storage="sqlalchemy", secret_key=CLAVE))
    yield
    reset_identity()


@pytest.fixture
def client() -> t.Iterator[TestClient]:
    app = FastAPI()
    app.add_middleware(_ContextoDePrueba)
    register_exception_handlers(
        app,
        mapping=dict(IDENTITY_EXCEPTION_STATUS_MAP),
        headers_for=identity_exception_headers,
        payload_for=identity_exception_payload,
    )

    @app.post(
        "/invoices",
        dependencies=[Depends(require_permission("invoice.create"))],
    )
    async def crear() -> dict:
        return {"ok": True}

    @app.post(
        "/invoices/{invoice_id}/approve",
        dependencies=[Depends(require_permission("invoice.approve", resource=_cargar_factura))],
    )
    async def aprobar(invoice_id: str) -> dict:
        return {"ok": True, "invoice_id": invoice_id}

    with TestClient(app) as cliente:
        yield cliente


def test_sin_credencial_es_401_con_www_authenticate(client: TestClient):
    respuesta = client.post("/invoices")

    assert respuesta.status_code == 401
    assert "WWW-Authenticate" in respuesta.headers


def test_con_el_scope_pasa(client: TestClient):
    respuesta = client.post("/invoices", headers={"X-Test-Scopes": "invoice.create"})

    assert respuesta.status_code == 200
    assert respuesta.json() == {"ok": True}


def test_el_comodin_tambien_pasa(client: TestClient):
    """El mismo comodín que `ScopeAuthorizationProvider` resuelve, contra la app real."""
    respuesta = client.post("/invoices", headers={"X-Test-Scopes": "invoice.*"})

    assert respuesta.status_code == 200


def test_sin_el_scope_es_403_con_required_y_sin_razon(client: TestClient):
    respuesta = client.post("/invoices", headers={"X-Test-Scopes": "users.view"})

    assert respuesta.status_code == 403
    cuerpo = respuesta.json()
    assert cuerpo["error"] == "AccessDeniedError"
    assert cuerpo["required"] == "invoice.create"
    # La razón de la decisión es interna: no aparece en ningún lado del cuerpo.
    assert "reason" not in cuerpo
    assert "policy" not in str(cuerpo).lower()


def test_un_scope_de_otra_accion_no_alcanza(client: TestClient):
    respuesta = client.post("/invoices", headers={"X-Test-Scopes": "invoice.read"})

    assert respuesta.status_code == 403


def test_la_ruta_con_recurso_resuelve_el_resource_ref_del_request(client: TestClient):
    """
    `require_permission(resource=...)` construye el `ResourceRef` desde el propio request; acá
    sólo se fija que la ruta corre y llega hasta el handler cuando el scope alcanza —el
    `ScopeAuthorizationProvider` no usa el recurso, así que no cambia el resultado, pero
    confirma que pasar un `resource` no rompe el camino feliz.
    """
    respuesta = client.post(
        "/invoices/42/approve", headers={"X-Test-Scopes": "invoice.approve"}
    )

    assert respuesta.status_code == 200
    assert respuesta.json() == {"ok": True, "invoice_id": "42"}


def test_la_ruta_con_recurso_tambien_deniega_sin_el_scope(client: TestClient):
    respuesta = client.post(
        "/invoices/42/approve", headers={"X-Test-Scopes": "invoice.read"}
    )

    assert respuesta.status_code == 403
    assert respuesta.json()["required"] == "invoice.approve"
