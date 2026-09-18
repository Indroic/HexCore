"""
`rbac`: roles y permisos persistidos y asignables, con scope y expiración.

Fase F2 del plan de plugins de autorización: acá aparece `RbacPlugin`, que junta lo que las
Fases F0 y F1 dejaron listo — el contrato de `AuthorizationEngine` y la persistencia de
`rbac` — en un plugin cableable.

**Los roles de código (`RoleRegistry`) siguen siendo la fuente de verdad de lo que un rol
significa.** El plugin los sincroniza a la tabla al arrancar (`RbacSeedStep`, `is_system=True`,
no editables por API) y agrega lo que `RoleRegistry` nunca tuvo: roles por tenant, asignación
con expiración, y un punto de decisión (`RbacAuthorizationProvider`) que se enchufa al mismo
`AuthorizationEngine` que cualquier otro provider.

`cli.py` aporta `rbac_cli`, un `typer.Typer` suelto con `sync`/`list`/`assign` — el núcleo
nunca importa un plugin por nombre, así que montarlo en tu propia app es cosa tuya (ver su
docstring).

Requiere `[darwin]`, `[api]` y, según el backend, `[darwin-sqlalchemy]` o `[darwin-beanie]`.

Uso::

    from hexcore.darwin import PluginRegistry, configure_identity
    from hexcore.darwin.domain.permissions import RoleRegistry
    from hexcore.darwin.plugins.rbac import RbacPlugin

    roles = (RoleRegistry()
        .register_role("viewer", permissions={"invoice.read"})
        .register_role("accountant", permissions={"invoice.approve"}, inherits={"viewer"}))

    rbac = RbacPlugin(registry=roles)
    configure_identity(
        IdentityConfig(),
        plugins=PluginRegistry([rbac]),
        # Puebla Principal.roles en el sign-in y en cada refresh — ver `resolver.py`.
        principals=rbac.principal_resolver(),
    )
"""
from __future__ import annotations

import threading
import typing as t
from dataclasses import dataclass
from datetime import timedelta

from hexcore.darwin.domain.plugins import DarwinPlugin
from hexcore.darwin.plugins.rbac.domain import (
    GLOBAL_SCOPE,
    RBAC_EXCEPTION_STATUS_MAP,
    AbstractAuthzVersionRepository,
    AbstractRbacPermissionRepository,
    AbstractRbacRoleRepository,
    AbstractRbacUserRoleRepository,
    AuthorizationChangedEvent,
    EscalationError,
    RbacError,
    RbacPermission,
    RbacRole,
    RoleAlreadyExistsError,
    RoleAssignment,
    RoleCycleError,
    RoleNotFoundError,
    SystemRoleImmutableError,
)
from hexcore.darwin.plugins.rbac.matcher import CompiledPermissionSet
from hexcore.darwin.plugins.rbac.resolver import EmbedMode, ScopeOf

if t.TYPE_CHECKING:
    # Sólo para el checker: en runtime los resuelve el `__getattr__` de abajo, porque
    # importarlos arrastra sqlalchemy y nombrar el plugin no puede exigir el extra
    # `[darwin-sqlalchemy]`. Mismo patrón que la fachada de Darwin y que `organization`.
    from hexcore.darwin.plugins.rbac.orms.sqlalchemy.models_mixins import (
        AuthzVersionMixin as AuthzVersionMixin,
        RbacPermissionMixin as RbacPermissionMixin,
        RbacRoleMixin as RbacRoleMixin,
        RbacRoleParentMixin as RbacRoleParentMixin,
        RbacRolePermissionMixin as RbacRolePermissionMixin,
        RbacUserRoleMixin as RbacUserRoleMixin,
    )
    from hexcore.darwin.domain.authorization import AuthorizationProvider
    from hexcore.darwin.domain.permissions import RoleRegistry
    from hexcore.darwin.plugins.rbac.provider import RbacAuthorizationProvider
    from hexcore.darwin.plugins.rbac.resolver import RbacPrincipalResolver
    from hexcore.darwin.plugins.rbac.service import RbacService

__all__ = [
    "GLOBAL_SCOPE",
    "RbacRole",
    "RbacPermission",
    "RoleAssignment",
    "AbstractRbacRoleRepository",
    "AbstractRbacPermissionRepository",
    "AbstractRbacUserRoleRepository",
    "AbstractAuthzVersionRepository",
    "AuthorizationChangedEvent",
    "RbacError",
    "RoleNotFoundError",
    "RoleAlreadyExistsError",
    "SystemRoleImmutableError",
    "RoleCycleError",
    "EscalationError",
    "RBAC_EXCEPTION_STATUS_MAP",
    "CompiledPermissionSet",
    "RbacRoleMixin",
    "RbacPermissionMixin",
    "RbacRolePermissionMixin",
    "RbacRoleParentMixin",
    "RbacUserRoleMixin",
    "AuthzVersionMixin",
    "RbacPlugin",
    "RbacSeedStep",
    "get_rbac_service",
]

_MIXINS = (
    "RbacRoleMixin",
    "RbacPermissionMixin",
    "RbacRolePermissionMixin",
    "RbacRoleParentMixin",
    "RbacUserRoleMixin",
    "AuthzVersionMixin",
)


@dataclass
class _Repos:
    """Los cuatro repositorios ya resueltos, para que `service()` y `authorization_providers()`
    compartan la misma instancia de cada uno en vez de construir una por separado."""

    roles: AbstractRbacRoleRepository
    permissions: AbstractRbacPermissionRepository
    user_roles: AbstractRbacUserRoleRepository
    versions: AbstractAuthzVersionRepository


class RbacPlugin(DarwinPlugin):
    """
    El plugin de roles y permisos persistidos.

    Args:
        registry: El `RoleRegistry` de código a sincronizar. Por default,
            `default_registry()` — el compartido del proceso.
        embed_in_token: Qué va al token además de los nombres de rol. Ver el docstring de
            `RbacPrincipalResolver`.
        cache_ttl: Vencimiento de la entrada de `PermissionMatrixCache` en el backend
            compartido (`ICache`). El LRU de proceso no vence por tiempo, sólo por tamaño.
        org_role_mapping: `{OrgRole: nombre_de_rol_rbac}`, para que el `OrgRole` de
            `organization` amplíe lo que el actor puede hacer. Ver el docstring de
            `RbacAuthorizationProvider._con_rol_de_organizacion`.
        scope_of: De qué scope resolver a un `User` en el sign-in. `None` (default) siempre
            resuelve el scope global — para tenancy, un consumidor pasa su propia función.
        seed_scope: El scope donde `RbacSeedStep` sincroniza el `RoleRegistry`. Global por
            default: los roles de código son la base de todos los tenants.
        include_router: Si aporta su router.
    """

    name = "rbac"
    #: Los nombres que devuelve `tables()`, para que el registro valide el conflicto de
    #: homónimos sin importar sqlalchemy. Un test verifica que coincidan.
    contributed_tables = (
        "RbacRoleMixin",
        "RbacPermissionMixin",
        "RbacRolePermissionMixin",
        "RbacRoleParentMixin",
        "RbacUserRoleMixin",
        "AuthzVersionMixin",
    )

    #: Antes que `organization` (80): la integración opcional resuelve un `OrgRole` que
    #: `organization` ya tiene que haber podido registrar. No hay `requires` porque la
    #: integración es best-effort y tolera que `organization` ni esté instalado.
    priority = 70

    def __init__(
        self,
        *,
        registry: "RoleRegistry | None" = None,
        embed_in_token: EmbedMode = "roles",
        cache_ttl: timedelta = timedelta(minutes=5),
        org_role_mapping: t.Mapping[str, str] | None = None,
        scope_of: ScopeOf | None = None,
        seed_scope: str = GLOBAL_SCOPE,
        role_repository: AbstractRbacRoleRepository | None = None,
        permission_repository: AbstractRbacPermissionRepository | None = None,
        user_role_repository: AbstractRbacUserRoleRepository | None = None,
        version_repository: AbstractAuthzVersionRepository | None = None,
        include_router: bool = True,
    ) -> None:
        self._registry = registry
        self._embed_in_token: EmbedMode = embed_in_token
        self._cache_ttl = cache_ttl
        self._org_role_mapping = org_role_mapping
        self._scope_of = scope_of
        self._seed_scope = seed_scope
        self._roles = role_repository
        self._permissions = permission_repository
        self._user_roles = user_role_repository
        self._versions = version_repository
        self._include_router = include_router

        self._lock = threading.RLock()
        self._repos: _Repos | None = None
        self._service: "RbacService | None" = None
        self._provider: "RbacAuthorizationProvider | None" = None
        self._resolver: "RbacPrincipalResolver | None" = None

    # ── Componentes, perezosos y cacheados ────────────────────────────────────
    def _resolved_repos(self) -> _Repos:
        with self._lock:
            if self._repos is None:
                from hexcore.darwin.plugins.storage import plugin_repositories

                modulo = plugin_repositories("rbac")
                self._repos = _Repos(
                    roles=self._roles or modulo.RbacRoleRepository(),
                    permissions=self._permissions or modulo.RbacPermissionRepository(),
                    user_roles=self._user_roles or modulo.RbacUserRoleRepository(),
                    versions=self._versions or modulo.AuthzVersionRepository(),
                )
            return self._repos

    def registry(self) -> "RoleRegistry":
        if self._registry is not None:
            return self._registry
        from hexcore.darwin.domain.permissions import default_registry

        return default_registry()

    def service(self) -> "RbacService":
        """
        El servicio, construido perezosamente desde el contenedor de identidad.

        Perezoso y cacheado con `RLock`, igual que el resto de los plugins con servicio
        propio: el plugin se instancia al declarar el registro —antes de
        `configure_identity`— así que construirlo en `__init__` obligaría a un orden de
        cableado que nadie tiene por qué recordar.
        """
        with self._lock:
            if self._service is None:
                from hexcore.darwin.application.container import get_identity_container
                from hexcore.darwin.plugins.rbac.service import RbacService

                contenedor = get_identity_container()
                repos = self._resolved_repos()
                self._service = RbacService(
                    roles=repos.roles,
                    permissions=repos.permissions,
                    user_roles=repos.user_roles,
                    versions=repos.versions,
                    clock=contenedor.clock(),
                    events=contenedor.events(),
                )
            return self._service

    def principal_resolver(self) -> "RbacPrincipalResolver":
        """
        El `AbstractPrincipalResolver` de este plugin, para pasarle a
        `configure_identity(principals=...)`.

        No es automático —el plugin no tiene forma de instalarse a sí mismo como resolver de
        principales, ese punto de extensión es del contenedor y no de `DarwinPlugin`— así que
        hace falta cablearlo a mano una vez.
        """
        with self._lock:
            if self._resolver is None:
                from hexcore.darwin.plugins.rbac.resolver import RbacPrincipalResolver

                self._resolver = RbacPrincipalResolver(
                    service=self.service(),
                    scope_of=self._scope_of,
                    embed_in_token=self._embed_in_token,
                )
            return self._resolver

    def reset(self) -> None:
        """Descarta lo cacheado. Para los tests, que reconfiguran el contenedor."""
        with self._lock:
            self._repos = None
            self._service = None
            self._provider = None
            self._resolver = None

    # ── Lo que aporta ─────────────────────────────────────────────────────────
    def authorization_providers(self) -> t.Sequence["AuthorizationProvider"]:
        with self._lock:
            if self._provider is None:
                from hexcore.darwin.plugins.rbac.cache import PermissionMatrixCache
                from hexcore.darwin.plugins.rbac.provider import RbacAuthorizationProvider

                self._provider = RbacAuthorizationProvider(
                    service=self.service(),
                    versions=self._resolved_repos().versions,
                    cache=PermissionMatrixCache(ttl=int(self._cache_ttl.total_seconds())),
                    org_role_mapping=self._org_role_mapping,
                )
            return [self._provider]

    def startup_steps(self) -> t.Sequence[t.Any]:
        return [RbacSeedStep(self, scope_key=self._seed_scope)]

    def tables(self) -> t.Mapping[str, type]:
        from hexcore.darwin.plugins.rbac.orms.sqlalchemy.models_mixins import (
            AuthzVersionMixin,
            RbacPermissionMixin,
            RbacRoleMixin,
            RbacRoleParentMixin,
            RbacRolePermissionMixin,
            RbacUserRoleMixin,
        )

        return {
            "RbacRoleMixin": RbacRoleMixin,
            "RbacPermissionMixin": RbacPermissionMixin,
            "RbacRolePermissionMixin": RbacRolePermissionMixin,
            "RbacRoleParentMixin": RbacRoleParentMixin,
            "RbacUserRoleMixin": RbacUserRoleMixin,
            "AuthzVersionMixin": AuthzVersionMixin,
        }

    def exception_status_map(self) -> t.Mapping[type[Exception], int]:
        return RBAC_EXCEPTION_STATUS_MAP

    def routers(self) -> t.Sequence[t.Any]:
        if not self._include_router:
            return ()

        from hexcore.darwin.plugins.rbac.router import build_rbac_router

        return [build_rbac_router()]


class RbacSeedStep:
    """
    `StartupStep`: sincroniza el `RoleRegistry` de código a la tabla al arrancar.

    Uso::

        lifespan = build_lifespan(SqlEngineStep(), IdentityStep(config), RbacSeedStep(rbac))
    """

    name = "darwin-rbac-seed"

    def __init__(self, plugin: RbacPlugin, *, scope_key: str = GLOBAL_SCOPE) -> None:
        self._plugin = plugin
        self._scope_key = scope_key

    async def start(self) -> None:
        await self._plugin.service().sync_system_roles(
            self._plugin.registry(), scope_key=self._scope_key
        )


def get_rbac_service() -> "RbacService":
    """
    El servicio del plugin registrado en este despliegue.

    Raises:
        RuntimeError: el plugin no está registrado, con la remediación copiable.
    """
    from hexcore.darwin.application.container import get_identity_container

    plugin = get_identity_container().plugins.get(RbacPlugin.name)
    if not isinstance(plugin, RbacPlugin):
        raise RuntimeError(
            "El plugin 'rbac' no está registrado en este despliegue.\n\n"
            "    from hexcore.darwin import PluginRegistry, configure_identity\n"
            "    from hexcore.darwin.plugins.rbac import RbacPlugin\n\n"
            "    configure_identity(config, plugins=PluginRegistry([RbacPlugin()]))"
        )
    return plugin.service()


def __getattr__(name: str) -> t.Any:
    """Los seis mixins, perezosos: importarlos arrastra sqlalchemy. Ver `organization`."""
    if name in _MIXINS:
        from hexcore.darwin.plugins.rbac.orms.sqlalchemy import models_mixins

        return getattr(models_mixins, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
