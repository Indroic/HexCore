"""
Darwin — Fase F5 del plan rbac/drbac: el router de `drbac`, contra una app real.

Mismo patrón que `test_darwin_authorization_http.py`: el `AuthContext` se publica a mano en
`request.state.darwin_auth`, con la forma que `AuthContextMiddleware` deja.

Lo que se fija:

1. `POST /check`: autenticado alcanza; la decisión real sale de `AuthorizationEngine`
   (`deny` de una política le gana al `allow` del scope retrocompatible).
2. `GET /me/snapshot`: sólo reglas `client_evaluable`, de políticas habilitadas, sin datos de
   ningún binding.
3. `/policies` y `/bindings`: 403 sin `authz.manage` en el scope, 200/201 con él.
4. `/simulate`: 403 sin `authz.debug`.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import NAMESPACE_DNS, uuid4, uuid5

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")
pytest.importorskip("joserfc")
pytest.importorskip("argon2")

from fastapi import FastAPI, Request  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402

from hexcore.darwin import (  # noqa: E402
    IDENTITY_EXCEPTION_STATUS_MAP,
    FixedClock,
    IdentityConfig,
    Principal,
    PluginRegistry,
    StaticKeyStore,
    AuthContext,
    configure_identity,
    create_identity_tables,
    generate_signing_key,
    reset_identity,
)
from hexcore.darwin.infrastructure.api.dependencies import (  # noqa: E402
    identity_exception_headers,
    identity_exception_payload,
)
from hexcore.darwin.plugins.drbac import DrbacPlugin, get_drbac_service  # noqa: E402
from hexcore.darwin.plugins.drbac.domain import DRBAC_EXCEPTION_STATUS_MAP  # noqa: E402
from hexcore.darwin.plugins.drbac.router import build_drbac_router  # noqa: E402
from hexcore.darwin.plugins.rbac import RbacPlugin  # noqa: E402
from hexcore.darwin.plugins.rbac.domain import RBAC_EXCEPTION_STATUS_MAP  # noqa: E402
from hexcore.infrastructure.api.exception_handlers import (  # noqa: E402
    register_exception_handlers,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    dispose_engine,
    init_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
CLAVE = "k" * 48


def _id_de(email: str) -> t.Any:
    """Un `user_id` determinístico a partir del email de prueba, para que un test pueda
    referenciar el id del actor en el cuerpo de un request (`owner_id`) sin depender de un id
    generado al azar por el middleware."""
    return uuid5(NAMESPACE_DNS, email)


class _ContextoDePrueba(BaseHTTPMiddleware):
    """Ver `test_darwin_authorization_http.py`: publica `request.state.darwin_auth` desde un
    header de prueba, sin pasar por un token real."""

    async def dispatch(self, request: Request, call_next: t.Any) -> t.Any:
        user_header = request.headers.get("X-Test-User")
        scopes_header = request.headers.get("X-Test-Scopes", "")
        if user_header is None:
            request.state.darwin_auth = None
        else:
            scopes = frozenset(s for s in scopes_header.split(",") if s)
            actor = Principal(user_id=_id_de(user_header), email=user_header, scopes=scopes)
            request.state.darwin_auth = AuthContext(
                actor=actor, subject=actor, transport="bearer"
            )
        return await call_next(request)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def client() -> t.Iterator[TestClient]:
    import asyncio

    asyncio.run(dispose_engine())
    motor = init_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor, plugins=["rbac", "drbac"]))

    reset_identity()
    configure_identity(
        IdentityConfig(storage="sqlalchemy", secret_key=CLAVE),
        plugins=PluginRegistry([RbacPlugin(), DrbacPlugin()]),
        clock=FixedClock(AHORA),
        key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
    )

    app = FastAPI()
    app.add_middleware(_ContextoDePrueba)
    register_exception_handlers(
        app,
        mapping={
            **IDENTITY_EXCEPTION_STATUS_MAP,
            **RBAC_EXCEPTION_STATUS_MAP,
            **DRBAC_EXCEPTION_STATUS_MAP,
        },
        headers_for=identity_exception_headers,
        payload_for=identity_exception_payload,
    )
    app.include_router(build_drbac_router())

    with TestClient(app) as cliente:
        yield cliente

    reset_identity()
    asyncio.run(dispose_engine())


# ── check ──────────────────────────────────────────────────────────────────────
def test_check_sin_credencial_es_401(client: TestClient):
    respuesta = client.post("/auth/drbac/check", json={"items": [{"action": "invoice.read"}]})
    assert respuesta.status_code == 401


def test_check_concede_por_el_scope_retrocompatible(client: TestClient):
    respuesta = client.post(
        "/auth/drbac/check",
        json={"items": [{"action": "invoice.read", "resource_type": "invoice"}]},
        headers={"X-Test-User": "a@test.com", "X-Test-Scopes": "invoice.read"},
    )
    assert respuesta.status_code == 200
    assert respuesta.json() == [
        {"action": "invoice.read", "resource_type": "invoice", "resource_id": None, "allowed": True}
    ]


def test_check_una_politica_deny_le_gana_al_scope(client: TestClient):
    import asyncio

    asyncio.run(
        get_drbac_service().create_policy(
            scope_key="org:1",
            name="no-self-approval",
            rules=[
                {
                    "effect": "deny",
                    "actions": ["invoice.approve"],
                    "resource_type": "invoice",
                    "condition": {
                        "type": "eq",
                        "left": {"type": "var", "path": "resource.owner_id"},
                        "right": {"type": "var", "path": "subject.id"},
                    },
                }
            ],
        )
    )

    respuesta = client.post(
        "/auth/drbac/check",
        json={
            "items": [
                {
                    "action": "invoice.approve",
                    "resource_type": "invoice",
                    "owner_id": str(_id_de("a@test.com")),
                    "scope_path": "org:1",
                }
            ]
        },
        headers={"X-Test-User": "a@test.com", "X-Test-Scopes": "invoice.*"},
    )
    assert respuesta.status_code == 200
    # El scope retrocompatible concede `invoice.*`, pero la política de DRBAC deniega
    # explícitamente aprobar la propia factura: deny-overrides gana.
    assert respuesta.json()[0]["allowed"] is False


def test_check_deduplica_items_identicos(client: TestClient):
    respuesta = client.post(
        "/auth/drbac/check",
        json={
            "items": [
                {"action": "invoice.read", "resource_type": "invoice"},
                {"action": "invoice.read", "resource_type": "invoice"},
            ]
        },
        headers={"X-Test-User": "a@test.com", "X-Test-Scopes": "invoice.read"},
    )
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert len(cuerpo) == 2
    assert all(item["allowed"] for item in cuerpo)


def test_check_rechaza_mas_de_50_items(client: TestClient):
    respuesta = client.post(
        "/auth/drbac/check",
        json={"items": [{"action": "x.y"} for _ in range(51)]},
        headers={"X-Test-User": "a@test.com"},
    )
    assert respuesta.status_code == 422


# ── snapshot ──────────────────────────────────────────────────────────────────
def test_snapshot_sin_credencial_es_401(client: TestClient):
    respuesta = client.get("/auth/drbac/me/snapshot")
    assert respuesta.status_code == 401


def test_snapshot_incluye_solo_reglas_client_evaluable(client: TestClient):
    import asyncio

    asyncio.run(
        get_drbac_service().create_policy(
            scope_key="org:1",
            name="p",
            rules=[
                {
                    "effect": "allow",
                    "actions": ["invoice.update"],
                    "resource_type": "invoice",
                    "condition": {
                        "type": "in",
                        "left": {"type": "var", "path": "resource.status"},
                        "right": {"type": "const", "value": ["draft"]},
                    },
                    "client_evaluable": True,
                },
                {
                    "effect": "deny",
                    "actions": ["invoice.delete"],
                    "resource_type": "invoice",
                    "client_evaluable": False,
                },
            ],
        )
    )

    respuesta = client.get(
        "/auth/drbac/me/snapshot",
        params={"scope": "org:1"},
        headers={"X-Test-User": "a@test.com"},
    )
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert len(cuerpo["rules"]) == 1
    assert cuerpo["rules"][0]["actions"] == ["invoice.update"]
    assert cuerpo["scope"] == "org:1"
    assert "version" in cuerpo and "expires_at" in cuerpo


# ── policies ──────────────────────────────────────────────────────────────────
def test_crear_politica_sin_authz_manage_es_403(client: TestClient):
    respuesta = client.post(
        "/auth/drbac/policies",
        json={"name": "p"},
        headers={"X-Test-User": "a@test.com"},
    )
    assert respuesta.status_code == 403


def test_crear_politica_con_authz_manage_pero_sin_policy_write_es_403(client: TestClient):
    """HC-20: escribir políticas exige `authz.policy.write`, no alcanza `authz.manage`."""
    respuesta = client.post(
        "/auth/drbac/policies",
        json={"name": "p"},
        headers={"X-Test-User": "a@test.com", "X-Test-Scopes": "authz.manage"},
    )
    assert respuesta.status_code == 403


def test_crear_y_listar_politica_con_authz_policy_write(client: TestClient):
    headers_write = {"X-Test-User": "a@test.com", "X-Test-Scopes": "authz.policy.write"}
    headers_manage = {"X-Test-User": "a@test.com", "X-Test-Scopes": "authz.manage"}
    creada = client.post("/auth/drbac/policies", json={"name": "p"}, headers=headers_write)
    assert creada.status_code == 201
    assert creada.json()["name"] == "p"

    listado = client.get("/auth/drbac/policies", headers=headers_manage)
    assert listado.status_code == 200
    assert any(p["name"] == "p" for p in listado.json())


def test_actualizar_y_borrar_politica(client: TestClient):
    headers = {"X-Test-User": "a@test.com", "X-Test-Scopes": "authz.policy.write"}
    creada = client.post("/auth/drbac/policies", json={"name": "p"}, headers=headers).json()

    actualizada = client.patch(
        f"/auth/drbac/policies/{creada['id']}",
        json={"enabled": False},
        headers=headers,
    )
    assert actualizada.status_code == 200
    assert actualizada.json()["enabled"] is False

    borrada = client.delete(f"/auth/drbac/policies/{creada['id']}", headers=headers)
    assert borrada.status_code == 200
    assert borrada.json() == {"deleted": True}


def test_politica_inexistente_es_404(client: TestClient):
    headers = {"X-Test-User": "a@test.com", "X-Test-Scopes": "authz.manage"}
    respuesta = client.patch(
        f"/auth/drbac/policies/{uuid4()}", json={"enabled": False}, headers=headers
    )
    assert respuesta.status_code == 404


# ── bindings ──────────────────────────────────────────────────────────────────
def test_crear_binding_sin_authz_manage_es_403(client: TestClient):
    respuesta = client.post(
        "/auth/drbac/bindings",
        json={"subject_id": str(uuid4()), "role_name": "accountant"},
        headers={"X-Test-User": "a@test.com"},
    )
    assert respuesta.status_code == 403


def test_crear_listar_y_revocar_binding(client: TestClient):
    headers = {"X-Test-User": "a@test.com", "X-Test-Scopes": "authz.manage"}
    subject_id = str(uuid4())
    creado = client.post(
        "/auth/drbac/bindings",
        json={"subject_id": subject_id, "role_name": "accountant"},
        headers=headers,
    )
    assert creado.status_code == 201
    assert creado.json()["role_name"] == "accountant"

    listado = client.get(
        "/auth/drbac/bindings", params={"subject_id": subject_id}, headers=headers
    )
    assert listado.status_code == 200
    assert len(listado.json()) == 1

    revocado = client.delete(
        f"/auth/drbac/bindings/{creado.json()['id']}", headers=headers
    )
    assert revocado.status_code == 200
    assert revocado.json() == {"revoked": True}


# ── simulate ──────────────────────────────────────────────────────────────────
def test_simulate_sin_authz_debug_es_403(client: TestClient):
    respuesta = client.post(
        "/auth/drbac/simulate",
        json={"action": "invoice.read", "resource_type": "invoice"},
        headers={"X-Test-User": "a@test.com"},
    )
    assert respuesta.status_code == 403


def test_simulate_con_authz_debug_devuelve_explain(client: TestClient):
    respuesta = client.post(
        "/auth/drbac/simulate",
        json={"action": "invoice.read", "resource_type": "invoice"},
        headers={"X-Test-User": "a@test.com", "X-Test-Scopes": "authz.debug,invoice.read"},
    )
    assert respuesta.status_code == 200
    cuerpo = respuesta.json()
    assert cuerpo["allowed"] is True
    assert len(cuerpo["explain"]) == 1
