"""
`RbacAuthorizationProvider`: el `AuthorizationProvider` de `rbac` para `AuthorizationEngine`
(Fase F0).

Sólo concede — nunca deniega. `decide()` devuelve `allow` cuando el actor tiene el permiso
pedido y `not_applicable` en cualquier otro caso, incluido "el actor no es un `Principal`"
(un `SystemPrincipal` tiene sus propios grants enumerados y no pasa por acá). Restringir con un
`deny` explícito es trabajo de DRBAC (Fase F5): éste sólo da el piso de lo que RBAC concede.

El scope de la decisión sale de `request.resource.scope_path`, o `GLOBAL_SCOPE` si no hay
recurso. **Hay herencia de scope** (HC-18): un rol asignado en `"org:1"` también aplica en
`"org:1/proj:2"` — se evalúa a lo largo de `scope_chain(scope_path)`, de global al scope exacto,
y se unen los permisos de cada nivel. Antes de HC-18, `"org:1/proj:2"` se trataba como clave
exacta y un rol global (o de organización) no tenía efecto en ningún scope hijo.
"""
from __future__ import annotations

import logging
import typing as t

from hexcore.darwin.domain.authorization import AccessRequest, AuthorizationProvider, Decision
from hexcore.darwin.domain.context import Principal
from hexcore.darwin.plugins.rbac.domain import GLOBAL_SCOPE, scope_chain
from hexcore.darwin.plugins.rbac.matcher import CompiledPermissionSet

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.ports import AbstractClock
    from hexcore.darwin.plugins.rbac.cache import PermissionMatrixCache
    from hexcore.darwin.plugins.rbac.domain import AbstractAuthzVersionRepository
    from hexcore.darwin.plugins.rbac.service import RbacService

__all__ = ["RbacAuthorizationProvider"]

logger = logging.getLogger("hexcore.darwin.rbac")


class RbacAuthorizationProvider(AuthorizationProvider):
    """
    Uso::

        engine = AuthorizationEngine([RbacAuthorizationProvider(service=..., versions=...)])
    """

    name = "rbac"

    def __init__(
        self,
        *,
        service: "RbacService",
        versions: "AbstractAuthzVersionRepository",
        cache: "PermissionMatrixCache | None" = None,
        org_role_mapping: t.Mapping[str, str] | None = None,
        clock: "AbstractClock | None" = None,
    ) -> None:
        self._service = service
        self._versions = versions
        self._org_role_mapping = dict(org_role_mapping or {})
        self._clock = clock
        if cache is None:
            from hexcore.darwin.plugins.rbac.cache import PermissionMatrixCache

            cache = PermissionMatrixCache()
        self._cache = cache

    def _reloj(self) -> "AbstractClock":
        if self._clock is not None:
            return self._clock
        from hexcore.darwin.infrastructure.clock import SystemClock

        return SystemClock()

    async def decide(self, request: AccessRequest) -> Decision:
        actor = request.context.actor
        if not isinstance(actor, Principal):
            return Decision(effect="not_applicable", provider=self.name)

        scope_path = (
            request.resource.scope_path if request.resource is not None else GLOBAL_SCOPE
        )
        ahora = self._reloj().now()
        compilado = await self._compilado_para_cadena(actor.user_id, scope_path, ahora)

        if not compilado.grants(request.action) and self._org_role_mapping:
            compilado = await self._con_rol_de_organizacion(actor, scope_path, compilado)

        if compilado.grants(request.action):
            return Decision(effect="allow", provider=self.name)
        return Decision(effect="not_applicable", provider=self.name)

    async def _compilado_para_cadena(
        self, user_id: t.Any, scope_path: str, ahora: t.Any
    ) -> CompiledPermissionSet:
        """
        Une los permisos efectivos de cada scope de `scope_chain(scope_path)`.

        Cada nivel se cachea y se versiona por separado —`PermissionMatrixCache` sigue siendo
        por `(user_id, scope_key, version)`, uno por nivel—, así que revocar un rol en
        `"org:1"` invalida sólo esa entrada, no las de sus scopes hijos.
        """
        claves: set[str] = set()
        for scope_key in scope_chain(scope_path):
            version = await self._versions.get(scope_key)
            compilado_del_nivel = await self._cache.get_or_compute(
                user_id,
                scope_key,
                version,
                compute=lambda sk=scope_key: self._service.effective_permissions(
                    user_id, sk, at=ahora
                ),
            )
            if compilado_del_nivel.has_global_wildcard:
                return compilado_del_nivel
            claves |= compilado_del_nivel.exact
            claves |= {f"{p}.*" for p in compilado_del_nivel.wildcard_prefixes}
        return CompiledPermissionSet.compile(claves)

    async def _con_rol_de_organizacion(
        self, actor: Principal, scope_key: str, base: CompiledPermissionSet
    ) -> CompiledPermissionSet:
        """
        Integración opcional con `organization`, **sin que ese plugin sepa que existe**.

        `scope_key` se interpreta como el `organization_id` de `organization` cuando hay un
        `org_role_mapping` declarado: se le pregunta a `AbstractMemberRepository` el `OrgRole`
        del actor ahí, se lo traduce al nombre de un rol `rbac` (en el scope global — el mapeo
        es una convención de código, no de tenant), y se suman sus permisos efectivos a los
        que ya tenía. No cachea este resultado combinado: el cache versionado es de
        `RbacService`, y la membresía de `organization` no tiene un contador equivalente.

        Cualquier fallo —`organization` no instalado, `scope_key` no es un id de organización
        válido, lo que sea— se traga y devuelve `base` sin tocar: es una ampliación opcional,
        nunca puede ser la causa de que una decisión que antes andaba deje de andar.
        """
        try:
            from uuid import UUID

            from hexcore.darwin.plugins.storage import plugin_repositories

            miembros = plugin_repositories("organization").MemberRepository()
            miembro = await miembros.get(UUID(scope_key), actor.user_id)
            if miembro is None:
                return base

            nombre_rbac = self._org_role_mapping.get(str(miembro.role))
            if nombre_rbac is None:
                return base

            extra = await self._service.permission_keys_for_role_name(
                GLOBAL_SCOPE, nombre_rbac
            )
            if not extra:
                return base

            return CompiledPermissionSet.compile(
                {*base.exact, *extra}
                | {f"{p}.*" for p in base.wildcard_prefixes}
                | ({"*"} if base.has_global_wildcard else set())
            )
        except Exception:
            logger.debug(
                "La integración con `organization` no resolvió para el scope %r; se "
                "ignora y se sigue sólo con los roles de rbac.",
                scope_key,
                exc_info=True,
            )
            return base
