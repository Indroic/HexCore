"""
`RbacPrincipalResolver`: puebla `Principal.roles`/`scopes` desde `RbacService`.

Es la pieza que conecta el plugin con `AbstractPrincipalResolver` (Fase 5 del núcleo):
`SessionService` lo llama en el sign-in y en cada rotación de refresh, así que quitarle un rol
a alguien tiene efecto sin esperar a que cierre sesión — la contracara documentada ahí es que
esto corre en un camino caliente-ish, y por eso `RbacService.effective_permission_keys` es lo
único que se llama, nunca una consulta N+1 por rol.

`embed_in_token` decide qué tan gordo sale el token:

- `"roles"` (default): sólo los **nombres** de rol van a `Principal.roles`/al JWT. Los
  permisos se resuelven server-side, por `RbacAuthorizationProvider`, con cache. Es lo que
  mantiene el token chico incluso con roles que acumulan muchos permisos.
- `"roles_and_permissions"`: además, los permisos efectivos van a `Principal.scopes` (y de ahí
  al claim `scopes` del token), para que `ScopeAuthorizationProvider` —el camino
  retrocompatible de la Fase F0— también los vea sin pasar por el motor nuevo. Sirve para migrar
  gradualmente un despliegue que todavía usa `require_scopes` en algunas rutas.
"""
from __future__ import annotations

import typing as t

from hexcore.darwin.domain.ports import AbstractPrincipalResolver, ResolvedPrincipal
from hexcore.darwin.plugins.rbac.domain import GLOBAL_SCOPE

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.entities import User
    from hexcore.darwin.domain.ports import AbstractClock
    from hexcore.darwin.plugins.rbac.service import RbacService

__all__ = ["RbacPrincipalResolver"]

EmbedMode = t.Literal["roles", "roles_and_permissions"]

#: De qué scope resolver al usuario, si no es el global. Recibe el `User` para que un
#: consumidor con tenancy pueda leer el tenant de sus propios campos (`user.extra["org_id"]`,
#: por ejemplo) sin que este módulo sepa nada de ese esquema.
ScopeOf = t.Callable[["User"], str]


class RbacPrincipalResolver(AbstractPrincipalResolver):
    """
    Uso::

        configure_identity(
            IdentityConfig(),
            principals=RbacPrincipalResolver(service=rbac_plugin.service()),
        )
    """

    def __init__(
        self,
        *,
        service: "RbacService",
        scope_of: ScopeOf | None = None,
        embed_in_token: EmbedMode = "roles",
        clock: "AbstractClock | None" = None,
    ) -> None:
        self._service = service
        self._scope_of = scope_of
        self._embed = embed_in_token
        self._clock = clock

    def _reloj(self) -> "AbstractClock":
        if self._clock is not None:
            return self._clock
        from hexcore.darwin.infrastructure.clock import SystemClock

        return SystemClock()

    async def resolve(self, user: "User") -> ResolvedPrincipal:
        scope_key = self._scope_of(user) if self._scope_of is not None else GLOBAL_SCOPE
        ahora = self._reloj().now()

        ids_de_rol = await self._service.active_role_ids(user.id, scope_key, at=ahora)
        roles = frozenset(rol.name for rol in await self._service.roles_by_ids(ids_de_rol))

        scopes: frozenset[str] = frozenset()
        if self._embed == "roles_and_permissions":
            claves = await self._service.effective_permission_keys(
                user.id, scope_key, at=ahora
            )
            scopes = frozenset(claves)

        return ResolvedPrincipal(roles=roles, scopes=scopes)
