"""
El router de `rbac`. Requiere `[api]`.

Dos familias de rutas:

- **`/auth/rbac/me/permissions`**: cualquier sesión autenticada. Es lo que un cliente usa para
  pintar la interfaz de forma optimista — la autoridad sigue siendo `AuthorizationEngine`,
  esto es sólo un resumen.
- **El resto (roles, permisos, asignaciones)**: administrativas, protegidas con
  `require_permission("authz.manage", ...)` — el mismo motor de la Fase F0, así que un
  `deny` de DRBAC más adelante también las alcanza sin tocar este archivo.

**El `actor_id` de las mutaciones sale siempre del contexto**, nunca del cuerpo: es lo que hace
que la anti-escalada de `RbacService` signifique algo. Un `actor_id` que el cliente rellena es
un `actor_id` que el cliente puede mentir.
"""
# pyright: reportUnusedFunction=false
from __future__ import annotations

import typing as t
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from hexcore.darwin.plugins.rbac.domain import GLOBAL_SCOPE

__all__ = [
    "CreateRoleBody",
    "UpdateRoleBody",
    "SetPermissionsBody",
    "SetParentsBody",
    "AssignRoleBody",
    "RoleOut",
    "MePermissionsOut",
    "build_rbac_router",
]


class CreateRoleBody(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""


class UpdateRoleBody(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None


class SetPermissionsBody(BaseModel):
    permission_keys: list[str] = Field(default_factory=list)


class SetParentsBody(BaseModel):
    parent_ids: list[UUID] = Field(default_factory=lambda: t.cast("list[UUID]", []))


class AssignRoleBody(BaseModel):
    user_id: UUID
    role_id: UUID
    expires_at: str | None = None


class RoleOut(BaseModel):
    id: str
    scope_key: str
    name: str
    description: str
    is_system: bool


class MePermissionsOut(BaseModel):
    scope: str
    roles: list[str]
    permissions: list[str]


def build_rbac_router(
    *, prefix: str = "/auth/rbac", tags: t.Sequence[str] = ("rbac",)
) -> APIRouter:
    """
    Construye el router del plugin.

    Uso::

        from hexcore.darwin.plugins.rbac.router import build_rbac_router

        app = create_app(routers=[build_identity_router(), build_rbac_router()])
    """
    from hexcore.darwin.infrastructure.api.authorization import require_permission
    from hexcore.darwin.infrastructure.api.dependencies import provide_auth

    router = APIRouter(prefix=prefix, tags=list(tags))

    async def _cargar_rol_como_recurso(request: Request) -> t.Any:
        """
        `ResourceRef` para las rutas `/roles/{role_id}...`: el scope sale del rol mismo, no de
        una query — es lo que hace que la autorización se evalúe en el scope real del recurso
        y no en uno que el cliente podría declarar distinto.
        """
        from hexcore.darwin.domain.authorization import ResourceRef
        from hexcore.darwin.plugins.rbac import get_rbac_service

        role_id = UUID(request.path_params["role_id"])
        rol = await get_rbac_service().get_role(role_id)
        return ResourceRef(type="rbac_role", id=str(role_id), scope_path=rol.scope_key)

    async def _scope_de_la_query(request: Request) -> t.Any:
        from hexcore.darwin.domain.authorization import ResourceRef

        scope = request.query_params.get("scope", GLOBAL_SCOPE)
        return ResourceRef(type="rbac_role", scope_path=scope)

    # ── Lo del propio usuario ─────────────────────────────────────────────────
    @router.get("/me/permissions")
    async def mis_permisos(
        scope: str = GLOBAL_SCOPE, auth: t.Any = Depends(provide_auth)
    ) -> MePermissionsOut:
        """
        Roles y permisos efectivos del actor en `scope`. **Optimista para UI.**

        La autoridad es `AuthorizationEngine.decide()` en cada acción real; esto es un
        resumen para que el cliente no tenga que adivinar qué mostrar.
        """
        from hexcore.darwin.infrastructure.clock import SystemClock
        from hexcore.darwin.plugins.rbac import get_rbac_service

        servicio = get_rbac_service()
        ahora = SystemClock().now()
        ids_de_rol = await servicio.active_role_ids(auth.actor_id, scope, at=ahora)
        roles = await servicio.roles_by_ids(ids_de_rol)
        permisos = await servicio.effective_permission_keys(auth.actor_id, scope, at=ahora)
        return MePermissionsOut(
            scope=scope,
            roles=sorted(r.name for r in roles),
            permissions=sorted(permisos),
        )

    # ── Roles ─────────────────────────────────────────────────────────────────
    @router.post(
        "/roles",
        status_code=201,
        dependencies=[
            Depends(require_permission("authz.manage", resource=_scope_de_la_query))
        ],
    )
    async def crear_rol(
        payload: CreateRoleBody, scope: str = GLOBAL_SCOPE
    ) -> RoleOut:
        from hexcore.darwin.plugins.rbac import get_rbac_service

        rol = await get_rbac_service().create_role(
            scope_key=scope, name=payload.name, description=payload.description
        )
        return _rol_out(rol)

    @router.get(
        "/roles",
        dependencies=[
            Depends(require_permission("authz.manage", resource=_scope_de_la_query))
        ],
    )
    async def listar_roles(scope: str = GLOBAL_SCOPE) -> list[RoleOut]:
        from hexcore.darwin.plugins.rbac import get_rbac_service

        roles = await get_rbac_service().list_roles(scope)
        return [_rol_out(r) for r in roles]

    @router.patch(
        "/roles/{role_id}",
        dependencies=[Depends(require_permission("authz.manage", resource=_cargar_rol_como_recurso))],
    )
    async def actualizar_rol(role_id: UUID, payload: UpdateRoleBody) -> RoleOut:
        from hexcore.darwin.plugins.rbac import get_rbac_service

        rol = await get_rbac_service().update_role(
            role_id, name=payload.name, description=payload.description
        )
        return _rol_out(rol)

    @router.delete(
        "/roles/{role_id}",
        dependencies=[Depends(require_permission("authz.manage", resource=_cargar_rol_como_recurso))],
    )
    async def borrar_rol(role_id: UUID) -> dict[str, bool]:
        from hexcore.darwin.plugins.rbac import get_rbac_service

        borrado = await get_rbac_service().delete_role(role_id)
        return {"deleted": borrado}

    @router.put(
        "/roles/{role_id}/permissions",
        dependencies=[Depends(require_permission("authz.manage", resource=_cargar_rol_como_recurso))],
    )
    async def establecer_permisos(
        role_id: UUID, payload: SetPermissionsBody, auth: t.Any = Depends(provide_auth)
    ) -> dict[str, bool]:
        """**Anti-escalada**: 403 si `permission_keys` excede lo que el actor ya tiene."""
        from hexcore.darwin.plugins.rbac import get_rbac_service

        await get_rbac_service().set_role_permissions(
            actor_id=auth.actor_id, role_id=role_id, permission_keys=payload.permission_keys
        )
        return {"ok": True}

    @router.put(
        "/roles/{role_id}/parents",
        dependencies=[Depends(require_permission("authz.manage", resource=_cargar_rol_como_recurso))],
    )
    async def establecer_padres(
        role_id: UUID, payload: SetParentsBody, auth: t.Any = Depends(provide_auth)
    ) -> dict[str, bool]:
        """**Ciclos y anti-escalada**: 409 si forma un ciclo, 403 si excede al actor."""
        from hexcore.darwin.plugins.rbac import get_rbac_service

        await get_rbac_service().set_role_parents(
            actor_id=auth.actor_id, role_id=role_id, parent_ids=payload.parent_ids
        )
        return {"ok": True}

    # ── Asignaciones ──────────────────────────────────────────────────────────
    @router.post(
        "/assignments",
        status_code=201,
        dependencies=[
            Depends(require_permission("authz.manage", resource=_scope_de_la_query))
        ],
    )
    async def asignar(
        payload: AssignRoleBody,
        scope: str = GLOBAL_SCOPE,
        auth: t.Any = Depends(provide_auth),
    ) -> dict[str, bool]:
        """**Anti-escalada**: 403 si el rol otorga más de lo que el actor tiene en `scope`."""
        from datetime import datetime

        from hexcore.darwin.plugins.rbac import get_rbac_service

        vencimiento = (
            datetime.fromisoformat(payload.expires_at) if payload.expires_at else None
        )
        await get_rbac_service().assign_role(
            actor_id=auth.actor_id,
            user_id=payload.user_id,
            role_id=payload.role_id,
            scope_key=scope,
            expires_at=vencimiento,
        )
        return {"assigned": True}

    @router.delete(
        "/assignments",
        dependencies=[
            Depends(require_permission("authz.manage", resource=_scope_de_la_query))
        ],
    )
    async def revocar(
        user_id: UUID, role_id: UUID, scope: str = GLOBAL_SCOPE
    ) -> dict[str, bool]:
        from hexcore.darwin.plugins.rbac import get_rbac_service

        revocado = await get_rbac_service().revoke_role(
            user_id=user_id, role_id=role_id, scope_key=scope
        )
        return {"revoked": revocado}

    return router


def _rol_out(rol: t.Any) -> RoleOut:
    return RoleOut(
        id=str(rol.id),
        scope_key=rol.scope_key,
        name=rol.name,
        description=rol.description,
        is_system=rol.is_system,
    )
