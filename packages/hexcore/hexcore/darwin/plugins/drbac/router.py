"""
El router de `drbac`. Requiere `[api]`.

Tres familias de rutas:

- **`POST /auth/drbac/check`** y **`GET /auth/drbac/me/snapshot`**: cualquier sesión
  autenticada. `check` es la fuente de verdad —corre `AuthorizationEngine.decide()` de
  verdad, con RBAC, DRBAC y el scope retrocompatible combinados—; `snapshot` es sólo un resumen
  optimista de las reglas `client_evaluable` (Fase F6), sin datos sensibles.
- **`/policies` y `/bindings`**: administrativas, protegidas con
  `require_permission("authz.manage", ...)`.
- **`POST /simulate`**: como `check`, pero devuelve además qué reglas se evaluaron y con qué
  resultado. Protegida con `authz.debug`: el `explain` es información interna de la política
  —lo que `Decision.reason` deliberadamente no expone en un 403 real— y sólo para quien está
  depurando, no para el flujo normal de la app.

**El `actor_id`/`subject_id` de las mutaciones de `/bindings` sale del cuerpo cuando referencia
a *otro* usuario (es la naturaleza de un binding: alguien se lo da a alguien más), pero el
contexto de la decisión en `check`/`simulate` siempre es el actor autenticado — nunca el que el
cliente podría declarar.**
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

import json
import typing as t
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from hexcore.darwin.plugins.drbac.conditions import Condition
from hexcore.darwin.plugins.drbac.domain import GLOBAL_SCOPE, Rule

#: Vencimiento optimista de `/me/snapshot` — mismo criterio que `rbac`'s `/me/permissions`.
_SNAPSHOT_HINT_TTL = timedelta(seconds=60)

#: Cuántos ítems admite `POST /check` de una — ver el docstring del módulo.
_MAX_CHECK_ITEMS = 50

__all__ = [
    "RuleIn",
    "RuleOut",
    "CreatePolicyBody",
    "UpdatePolicyBody",
    "PolicyOut",
    "CreateBindingBody",
    "BindingOut",
    "CheckItem",
    "CheckResultOut",
    "SnapshotRuleOut",
    "SnapshotOut",
    "SimulateOut",
    "build_drbac_router",
]


class RuleIn(BaseModel):
    effect: t.Literal["allow", "deny"]
    actions: list[str] = Field(min_length=1)
    resource_type: str = Field(min_length=1)
    condition: Condition | None = None
    client_evaluable: bool = False


class RuleOut(BaseModel):
    id: str
    position: int
    effect: str
    actions: list[str]
    resource_type: str
    condition: dict[str, t.Any] | None
    client_evaluable: bool


class CreatePolicyBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    enabled: bool = True
    priority: int = 100
    rules: list[RuleIn] = Field(default_factory=lambda: t.cast("list[RuleIn]", []))


class UpdatePolicyBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    enabled: bool | None = None
    priority: int | None = None
    rules: list[RuleIn] | None = None


class PolicyOut(BaseModel):
    id: str
    scope_key: str
    name: str
    description: str
    enabled: bool
    priority: int
    rules: list[RuleOut]


class CreateBindingBody(BaseModel):
    subject_id: UUID
    role_name: str = Field(min_length=1, max_length=128)
    condition: Condition | None = None
    expires_at: str | None = None


class BindingOut(BaseModel):
    id: str
    subject_id: str
    role_name: str
    scope_path: str
    expires_at: str | None


class CheckItem(BaseModel):
    action: str = Field(min_length=1)
    resource_type: str = ""
    resource_id: str | None = None
    owner_id: str | None = None
    scope_path: str = GLOBAL_SCOPE
    attributes: dict[str, t.Any] = Field(default_factory=dict)


class CheckBody(BaseModel):
    items: list[CheckItem] = Field(min_length=1, max_length=_MAX_CHECK_ITEMS)


class CheckResultOut(BaseModel):
    action: str
    resource_type: str
    resource_id: str | None
    allowed: bool


class SnapshotRuleOut(BaseModel):
    effect: str
    actions: list[str]
    resource_type: str
    condition: dict[str, t.Any] | None


class SnapshotOut(BaseModel):
    scope: str
    version: int
    expires_at: str
    rules: list[SnapshotRuleOut]


class SimulateOut(BaseModel):
    allowed: bool
    explain: list[dict[str, t.Any]]


def _clave_del_item(item: CheckItem) -> str:
    """Para deduplicar ítems idénticos del lote sin depender de que sus campos sean hasheables."""
    return json.dumps(item.model_dump(), sort_keys=True, default=str)


def build_drbac_router(
    *, prefix: str = "/auth/drbac", tags: t.Sequence[str] = ("drbac",)
) -> APIRouter:
    """
    Uso::

        from hexcore.darwin.plugins.drbac.router import build_drbac_router

        app = create_app(routers=[build_identity_router(), build_drbac_router()])
    """
    from hexcore.darwin.domain.authorization import AccessRequest, ResourceRef
    from hexcore.darwin.infrastructure.api.authorization import require_permission
    from hexcore.darwin.infrastructure.api.dependencies import provide_auth

    router = APIRouter(prefix=prefix, tags=list(tags))

    async def _scope_de_la_query(request: Request) -> t.Any:
        scope = request.query_params.get("scope", GLOBAL_SCOPE)
        return ResourceRef(type="drbac_policy", scope_path=scope)

    async def _cargar_policy_como_recurso(request: Request) -> t.Any:
        from hexcore.darwin.plugins.drbac import get_drbac_service

        policy_id = UUID(request.path_params["policy_id"])
        politica = await get_drbac_service().get_policy(policy_id)
        return ResourceRef(type="drbac_policy", id=str(policy_id), scope_path=politica.scope_key)

    # ── check / snapshot ──────────────────────────────────────────────────────
    @router.post("/check")
    async def check(payload: CheckBody, auth: t.Any = Depends(provide_auth)) -> list[CheckResultOut]:
        """
        La decisión real de `AuthorizationEngine` para cada ítem, en el mismo orden que llegó.
        Deduplica ítems idénticos: se evalúan una sola vez.
        """
        from hexcore.darwin.application.container import get_identity_container

        engine = get_identity_container().authorizer()
        resueltos: dict[str, bool] = {}

        for item in payload.items:
            clave = _clave_del_item(item)
            if clave in resueltos:
                continue
            resource = ResourceRef(
                type=item.resource_type,
                id=item.resource_id,
                owner_id=UUID(item.owner_id) if item.owner_id else None,
                scope_path=item.scope_path,
                attributes=item.attributes,
            )
            decision = await engine.decide(
                AccessRequest(context=auth, action=item.action, resource=resource)
            )
            resueltos[clave] = decision.allowed

        return [
            CheckResultOut(
                action=item.action,
                resource_type=item.resource_type,
                resource_id=item.resource_id,
                allowed=resueltos[_clave_del_item(item)],
            )
            for item in payload.items
        ]

    @router.get("/me/snapshot")
    async def mi_snapshot(
        scope: str = GLOBAL_SCOPE, auth: t.Any = Depends(provide_auth)
    ) -> SnapshotOut:
        """
        Las reglas `client_evaluable` de `scope` y sus ancestros. **Optimista para UI, y sin
        nada sensible**: ni bindings, ni políticas deshabilitadas, ni reglas con `Predicate`
        (nunca son `client_evaluable` — ver `DrbacService._preparar_reglas`).
        """
        from hexcore.darwin.infrastructure.clock import SystemClock
        from hexcore.darwin.plugins.drbac import get_drbac_service
        from hexcore.darwin.plugins.drbac.domain import scope_chain

        servicio = get_drbac_service()
        ahora = SystemClock().now()

        reglas: list[SnapshotRuleOut] = []
        version_total = 0
        for capa in scope_chain(scope):
            version_total += await servicio.authz_version(capa)
            for politica in await servicio.list_policies(capa):
                if not politica.enabled:
                    continue
                for regla in politica.rules:
                    if not regla.client_evaluable:
                        continue
                    reglas.append(
                        SnapshotRuleOut(
                            effect=regla.effect,
                            actions=list(regla.actions),
                            resource_type=regla.resource_type,
                            condition=regla.condition.model_dump(mode="json")
                            if regla.condition is not None
                            else None,
                        )
                    )

        return SnapshotOut(
            scope=scope,
            version=version_total,
            expires_at=(ahora + _SNAPSHOT_HINT_TTL).isoformat(),
            rules=reglas,
        )

    # ── Políticas ─────────────────────────────────────────────────────────────
    @router.post(
        "/policies",
        status_code=201,
        dependencies=[Depends(require_permission("authz.manage", resource=_scope_de_la_query))],
    )
    async def crear_politica(
        payload: CreatePolicyBody, scope: str = GLOBAL_SCOPE
    ) -> PolicyOut:
        from hexcore.darwin.plugins.drbac import get_drbac_service

        politica = await get_drbac_service().create_policy(
            scope_key=scope,
            name=payload.name,
            description=payload.description,
            enabled=payload.enabled,
            priority=payload.priority,
            rules=[r.model_dump() for r in payload.rules],
        )
        return _policy_out(politica)

    @router.get(
        "/policies",
        dependencies=[Depends(require_permission("authz.manage", resource=_scope_de_la_query))],
    )
    async def listar_politicas(scope: str = GLOBAL_SCOPE) -> list[PolicyOut]:
        from hexcore.darwin.plugins.drbac import get_drbac_service

        politicas = await get_drbac_service().list_policies(scope)
        return [_policy_out(p) for p in politicas]

    @router.patch(
        "/policies/{policy_id}",
        dependencies=[Depends(require_permission("authz.manage", resource=_cargar_policy_como_recurso))],
    )
    async def actualizar_politica(policy_id: UUID, payload: UpdatePolicyBody) -> PolicyOut:
        from hexcore.darwin.plugins.drbac import get_drbac_service

        politica = await get_drbac_service().update_policy(
            policy_id,
            name=payload.name,
            description=payload.description,
            enabled=payload.enabled,
            priority=payload.priority,
            rules=[r.model_dump() for r in payload.rules] if payload.rules is not None else None,
        )
        return _policy_out(politica)

    @router.delete(
        "/policies/{policy_id}",
        dependencies=[Depends(require_permission("authz.manage", resource=_cargar_policy_como_recurso))],
    )
    async def borrar_politica(policy_id: UUID) -> dict[str, bool]:
        from hexcore.darwin.plugins.drbac import get_drbac_service

        borrada = await get_drbac_service().delete_policy(policy_id)
        return {"deleted": borrada}

    # ── Bindings ──────────────────────────────────────────────────────────────
    @router.post(
        "/bindings",
        status_code=201,
        dependencies=[Depends(require_permission("authz.manage", resource=_scope_de_la_query))],
    )
    async def crear_binding(
        payload: CreateBindingBody,
        scope: str = GLOBAL_SCOPE,
        auth: t.Any = Depends(provide_auth),
    ) -> BindingOut:
        from datetime import datetime

        from hexcore.darwin.plugins.drbac import get_drbac_service

        vencimiento = (
            datetime.fromisoformat(payload.expires_at) if payload.expires_at else None
        )
        ligadura = await get_drbac_service().create_binding(
            subject_id=payload.subject_id,
            role_name=payload.role_name,
            scope_path=scope,
            condition=payload.condition,
            expires_at=vencimiento,
            granted_by=auth.actor_id,
        )
        return _binding_out(ligadura)

    @router.get(
        "/bindings",
        dependencies=[Depends(require_permission("authz.manage", resource=_scope_de_la_query))],
    )
    async def listar_bindings(subject_id: UUID) -> list[BindingOut]:
        from hexcore.darwin.plugins.drbac import get_drbac_service

        ligaduras = await get_drbac_service().list_bindings_for_subject(subject_id)
        return [_binding_out(b) for b in ligaduras]

    @router.delete(
        "/bindings/{binding_id}",
        dependencies=[Depends(require_permission("authz.manage", resource=_scope_de_la_query))],
    )
    async def revocar_binding(binding_id: UUID) -> dict[str, bool]:
        from hexcore.darwin.plugins.drbac import get_drbac_service

        revocado = await get_drbac_service().revoke_binding(binding_id)
        return {"revoked": revocado}

    # ── Simulación ────────────────────────────────────────────────────────────
    @router.post(
        "/simulate",
        dependencies=[Depends(require_permission("authz.debug", resource=_scope_de_la_query))],
    )
    async def simular(payload: CheckItem, auth: t.Any = Depends(provide_auth)) -> SimulateOut:
        """Como un ítem de `check`, pero con el detalle de qué reglas se evaluaron y cómo."""
        from hexcore.darwin.application.container import get_identity_container
        from hexcore.darwin.plugins.drbac.domain import scope_chain

        resource = ResourceRef(
            type=payload.resource_type,
            id=payload.resource_id,
            owner_id=UUID(payload.owner_id) if payload.owner_id else None,
            scope_path=payload.scope_path,
            attributes=payload.attributes,
        )
        engine = get_identity_container().authorizer()
        decision = await engine.decide(
            AccessRequest(context=auth, action=payload.action, resource=resource)
        )
        return SimulateOut(
            allowed=decision.allowed,
            explain=[
                {
                    "provider": decision.provider,
                    "effect": decision.effect,
                    "policy_id": decision.policy_id,
                    "scope_chain": list(scope_chain(payload.scope_path)),
                }
            ],
        )

    return router


def _policy_out(politica: t.Any) -> PolicyOut:
    return PolicyOut(
        id=str(politica.id),
        scope_key=politica.scope_key,
        name=politica.name,
        description=politica.description,
        enabled=politica.enabled,
        priority=politica.priority,
        rules=[_rule_out(r) for r in politica.rules],
    )


def _rule_out(regla: Rule) -> RuleOut:
    return RuleOut(
        id=str(regla.id),
        position=regla.position,
        effect=regla.effect,
        actions=list(regla.actions),
        resource_type=regla.resource_type,
        condition=regla.condition.model_dump(mode="json") if regla.condition is not None else None,
        client_evaluable=regla.client_evaluable,
    )


def _binding_out(ligadura: t.Any) -> BindingOut:
    return BindingOut(
        id=str(ligadura.id),
        subject_id=str(ligadura.subject_id),
        role_name=ligadura.role_name,
        scope_path=ligadura.scope_path,
        expires_at=ligadura.expires_at.isoformat() if ligadura.expires_at else None,
    )
