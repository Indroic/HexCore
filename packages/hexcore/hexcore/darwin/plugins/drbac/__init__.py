"""
`drbac`: autorización contextual sobre `rbac`.

Fase F5 del plan de rbac/drbac: acá aparece `DrbacPlugin`, que junta el lenguaje de condiciones
y el registro de predicados de la Fase F4 con persistencia, el PDP (`pdp.py`), el PIP
(`pip.py`) y un `AuthorizationProvider` que se enchufa al mismo `AuthorizationEngine` que
`rbac`.

**`requires = ("rbac",)`**: DRBAC extiende RBAC, no lo reemplaza — los roles siguen siendo la
base, y DRBAC agrega condiciones, scope jerárquico y bindings de rol contextuales con
vencimiento. Eso sí, **ningún módulo de este plugin importa nada de `rbac`** (ver el docstring
de `domain.RoleBinding`): `requires` sólo ordena el registro, la integración real —expandir un
`role_name` contextual a sus permisos— la cablea el consumidor con `role_permissions`.

Requiere `[darwin]`, `[api]` y, según el backend, `[darwin-sqlalchemy]` o `[darwin-beanie]`.

Uso::

    from hexcore.darwin import PluginRegistry, configure_identity
    from hexcore.darwin.plugins.rbac import RbacPlugin
    from hexcore.darwin.plugins.drbac import DrbacPlugin

    rbac = RbacPlugin()
    drbac = DrbacPlugin(role_permissions=rbac.service().permission_keys_for_role_name)
    configure_identity(
        IdentityConfig(),
        plugins=PluginRegistry([rbac, drbac]),
        principals=rbac.principal_resolver(),
    )
"""
from __future__ import annotations

import threading
import typing as t
from dataclasses import dataclass

from hexcore.darwin.domain.plugins import DarwinPlugin
from hexcore.darwin.plugins.drbac.domain import (
    DRBAC_EXCEPTION_STATUS_MAP,
    GLOBAL_SCOPE,
    AbstractDrbacAuthzVersionRepository,
    AbstractDrbacPolicyRepository,
    AbstractDrbacRoleBindingRepository,
    DrbacAuthorizationChangedEvent,
    DrbacError,
    Policy,
    PolicyAlreadyExistsError,
    PolicyNotFoundError,
    RoleBinding,
    RoleBindingNotFoundError,
    Rule,
    scope_chain,
)
from hexcore.darwin.plugins.drbac.pdp import EvaluationLimits, RolePermissionsResolver
from hexcore.darwin.plugins.drbac.pip import PolicyInformationPoint, ResourceAttributeResolver
from hexcore.darwin.plugins.drbac.predicates import PredicateRegistry

if t.TYPE_CHECKING:
    # Sólo para el checker: en runtime los resuelve el `__getattr__` de abajo. Mismo patrón que
    # `rbac` y que `organization`.
    from hexcore.darwin.plugins.drbac.orms.sqlalchemy.models_mixins import (
        DrbacAuthzVersionMixin as DrbacAuthzVersionMixin,
        DrbacPolicyMixin as DrbacPolicyMixin,
        DrbacRoleBindingMixin as DrbacRoleBindingMixin,
        DrbacRuleMixin as DrbacRuleMixin,
    )
    from hexcore.darwin.domain.authorization import AuthorizationProvider
    from hexcore.darwin.plugins.drbac.pdp import PolicyDecisionPoint
    from hexcore.darwin.plugins.drbac.provider import DrbacAuthorizationProvider
    from hexcore.darwin.plugins.drbac.service import DrbacService

__all__ = [
    "GLOBAL_SCOPE",
    "scope_chain",
    "Rule",
    "Policy",
    "RoleBinding",
    "AbstractDrbacPolicyRepository",
    "AbstractDrbacRoleBindingRepository",
    "AbstractDrbacAuthzVersionRepository",
    "DrbacAuthorizationChangedEvent",
    "DrbacError",
    "PolicyNotFoundError",
    "PolicyAlreadyExistsError",
    "RoleBindingNotFoundError",
    "DRBAC_EXCEPTION_STATUS_MAP",
    "EvaluationLimits",
    "RolePermissionsResolver",
    "ResourceAttributeResolver",
    "PolicyInformationPoint",
    "PredicateRegistry",
    "DrbacPolicyMixin",
    "DrbacRuleMixin",
    "DrbacRoleBindingMixin",
    "DrbacAuthzVersionMixin",
    "DrbacPlugin",
    "get_drbac_service",
    "get_drbac_plugin",
]

_MIXINS = (
    "DrbacPolicyMixin",
    "DrbacRuleMixin",
    "DrbacRoleBindingMixin",
    "DrbacAuthzVersionMixin",
)


@dataclass
class _Repos:
    """Los tres repositorios ya resueltos, compartidos entre `service()` y `pdp()`."""

    policies: AbstractDrbacPolicyRepository
    bindings: AbstractDrbacRoleBindingRepository
    versions: AbstractDrbacAuthzVersionRepository


class DrbacPlugin(DarwinPlugin):
    """
    El plugin de autorización contextual.

    Args:
        resolvers: Los `ResourceAttributeResolver` del PIP, por `resource.type`.
        predicates: El `PredicateRegistry` a usar. Por default, `default_predicate_registry()`
            — el compartido del proceso.
        role_permissions: Cómo expandir un `RoleBinding.role_name` contextual a sus permisos.
            `None` (default): los bindings siguen visibles en `subject.roles` para que una
            condición los use, pero no conceden nada por sí solos. Ver el docstring de
            `pdp.RolePermissionsResolver`.
        limits: Los presupuestos de tamaño y tiempo de una decisión.
        include_router: Si aporta su router.
    """

    name = "drbac"
    #: Los roles son la base; DRBAC restringe o amplía condicionalmente lo que RBAC ya concede.
    requires = ("rbac",)
    #: Después de `rbac` (70), antes de `organization` (80).
    priority = 75

    contributed_tables = (
        "DrbacPolicyMixin",
        "DrbacRuleMixin",
        "DrbacRoleBindingMixin",
        "DrbacAuthzVersionMixin",
    )

    def __init__(
        self,
        *,
        resolvers: t.Mapping[str, ResourceAttributeResolver] | None = None,
        predicates: PredicateRegistry | None = None,
        role_permissions: RolePermissionsResolver | None = None,
        limits: EvaluationLimits = EvaluationLimits(),
        policy_repository: AbstractDrbacPolicyRepository | None = None,
        role_binding_repository: AbstractDrbacRoleBindingRepository | None = None,
        version_repository: AbstractDrbacAuthzVersionRepository | None = None,
        include_router: bool = True,
    ) -> None:
        self._resolvers = resolvers
        self._predicates = predicates
        self._role_permissions = role_permissions
        self._limits = limits
        self._policies = policy_repository
        self._bindings = role_binding_repository
        self._versions = version_repository
        self._include_router = include_router

        self._lock = threading.RLock()
        self._repos: _Repos | None = None
        self._service: "DrbacService | None" = None
        self._pdp: "PolicyDecisionPoint | None" = None
        self._provider: "DrbacAuthorizationProvider | None" = None

    # ── Componentes, perezosos y cacheados ────────────────────────────────────
    def _resolved_repos(self) -> _Repos:
        with self._lock:
            if self._repos is None:
                from hexcore.darwin.plugins.storage import plugin_repositories

                modulo = plugin_repositories("drbac")
                self._repos = _Repos(
                    policies=self._policies or modulo.PolicyRepository(),
                    bindings=self._bindings or modulo.RoleBindingRepository(),
                    versions=self._versions or modulo.AuthzVersionRepository(),
                )
            return self._repos

    def service(self) -> "DrbacService":
        """El servicio, construido perezosamente desde el contenedor de identidad. Mismo
        criterio que `RbacPlugin.service`."""
        with self._lock:
            if self._service is None:
                from hexcore.darwin.application.container import get_identity_container
                from hexcore.darwin.plugins.drbac.service import DrbacService

                contenedor = get_identity_container()
                repos = self._resolved_repos()
                self._service = DrbacService(
                    policies=repos.policies,
                    bindings=repos.bindings,
                    versions=repos.versions,
                    clock=contenedor.clock(),
                    events=contenedor.events(),
                )
            return self._service

    def pdp(self) -> "PolicyDecisionPoint":
        with self._lock:
            if self._pdp is None:
                from hexcore.darwin.plugins.drbac.pdp import PolicyDecisionPoint

                repos = self._resolved_repos()
                self._pdp = PolicyDecisionPoint(
                    policies=repos.policies,
                    bindings=repos.bindings,
                    versions=repos.versions,
                    pip=PolicyInformationPoint(resolvers=self._resolvers),
                    predicates=self._predicates,
                    role_permissions=self._role_permissions,
                    limits=self._limits,
                )
            return self._pdp

    def resolved_resource_types(self) -> frozenset[str]:
        """Los `resource.type` con un `ResourceAttributeResolver` propio.

        Lo usa el router para rechazar `attributes` en `/check` y `/simulate` para estos tipos
        — si el servidor ya sabe resolverlos, un `attributes` del cliente sólo podría pisar lo
        que el resolver calcula, y eso es exactamente lo que `PolicyInformationPoint` ya no
        deja pasar (HC-14). Rechazarlo acá, antes de evaluar, es más claro que dejar que se
        ignore en silencio.
        """
        return frozenset(self._resolvers or {})

    def reset(self) -> None:
        """Descarta lo cacheado. Para los tests."""
        with self._lock:
            self._repos = None
            self._service = None
            self._pdp = None
            self._provider = None

    # ── Lo que aporta ─────────────────────────────────────────────────────────
    def authorization_providers(self) -> t.Sequence["AuthorizationProvider"]:
        with self._lock:
            if self._provider is None:
                from hexcore.darwin.plugins.drbac.provider import DrbacAuthorizationProvider

                self._provider = DrbacAuthorizationProvider(pdp=self.pdp())
            return [self._provider]

    def tables(self) -> t.Mapping[str, type]:
        from hexcore.darwin.plugins.drbac.orms.sqlalchemy.models_mixins import (
            DrbacAuthzVersionMixin,
            DrbacPolicyMixin,
            DrbacRoleBindingMixin,
            DrbacRuleMixin,
        )

        return {
            "DrbacPolicyMixin": DrbacPolicyMixin,
            "DrbacRuleMixin": DrbacRuleMixin,
            "DrbacRoleBindingMixin": DrbacRoleBindingMixin,
            "DrbacAuthzVersionMixin": DrbacAuthzVersionMixin,
        }

    def exception_status_map(self) -> t.Mapping[type[Exception], int]:
        return DRBAC_EXCEPTION_STATUS_MAP

    def routers(self) -> t.Sequence[t.Any]:
        if not self._include_router:
            return ()

        from hexcore.darwin.plugins.drbac.router import build_drbac_router

        return [build_drbac_router()]


def get_drbac_plugin() -> "DrbacPlugin":
    """
    El plugin `drbac` registrado en este despliegue.

    Raises:
        RuntimeError: el plugin no está registrado, con la remediación copiable.
    """
    from hexcore.darwin.application.container import get_identity_container

    plugin = get_identity_container().plugins.get(DrbacPlugin.name)
    if not isinstance(plugin, DrbacPlugin):
        raise RuntimeError(
            "El plugin 'drbac' no está registrado en este despliegue.\n\n"
            "    from hexcore.darwin import PluginRegistry, configure_identity\n"
            "    from hexcore.darwin.plugins.drbac import DrbacPlugin\n\n"
            "    configure_identity(config, plugins=PluginRegistry([RbacPlugin(), DrbacPlugin()]))"
        )
    return plugin


def get_drbac_service() -> "DrbacService":
    """
    El servicio del plugin registrado en este despliegue.

    Raises:
        RuntimeError: el plugin no está registrado, con la remediación copiable.
    """
    from hexcore.darwin.application.container import get_identity_container

    plugin = get_identity_container().plugins.get(DrbacPlugin.name)
    if not isinstance(plugin, DrbacPlugin):
        raise RuntimeError(
            "El plugin 'drbac' no está registrado en este despliegue.\n\n"
            "    from hexcore.darwin import PluginRegistry, configure_identity\n"
            "    from hexcore.darwin.plugins.drbac import DrbacPlugin\n\n"
            "    configure_identity(config, plugins=PluginRegistry([RbacPlugin(), DrbacPlugin()]))"
        )
    return plugin.service()


def __getattr__(name: str) -> t.Any:
    """Los cuatro mixins, perezosos: importarlos arrastra sqlalchemy. Ver `rbac`."""
    if name in _MIXINS:
        from hexcore.darwin.plugins.drbac.orms.sqlalchemy import models_mixins

        return getattr(models_mixins, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
