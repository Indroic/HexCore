"""
`rbac`: roles y permisos persistidos y asignables, con scope y expiración.

⚠️ **Fase F1 del plan de plugins de autorización: sólo dominio y persistencia.** Este paquete
todavía no aporta un `DarwinPlugin` — no hay `RbacPlugin`, ni servicio, ni resolver, ni router.
Eso es la Fase F2. Lo que ya existe y es estable:

- `domain.py`: las entidades (`RbacRole`, `RbacPermission`, `RoleAssignment`), los cuatro
  puertos de repositorio, y las excepciones.
- `matcher.py`: `CompiledPermissionSet`, para resolver `grants()` sin recorrer
  `Permission.grants` uno por uno.
- `orms/sqlalchemy/` y `orms/beanie/`: los seis mixins de SQL (cuatro documentos en Mongo — ver
  el docstring de `orms/beanie/repository.py`) y los adaptadores de los cuatro puertos.

Requiere `[darwin]` y, según el backend, `[darwin-sqlalchemy]` o `[darwin-beanie]`. No agrega
dependencias nuevas de ninguno de los dos: los mixins son SQLAlchemy puro y los documentos son
Beanie puro, igual que el resto de los plugins con tabla propia.
"""
from __future__ import annotations

import typing as t

from hexcore.darwin.plugins.rbac.domain import (
    GLOBAL_SCOPE,
    RBAC_EXCEPTION_STATUS_MAP,
    AbstractAuthzVersionRepository,
    AbstractRbacPermissionRepository,
    AbstractRbacRoleRepository,
    AbstractRbacUserRoleRepository,
    RbacError,
    RbacPermission,
    RbacRole,
    RoleAssignment,
    RoleNotFoundError,
)
from hexcore.darwin.plugins.rbac.matcher import CompiledPermissionSet

if t.TYPE_CHECKING:
    # Sólo para el checker: en runtime los resuelve el `__getattr__` de abajo, porque
    # importarlos arrastra sqlalchemy y nombrar el paquete no puede exigir el extra
    # `[darwin-sqlalchemy]`. Mismo patrón que la fachada de Darwin y que `organization`.
    from hexcore.darwin.plugins.rbac.orms.sqlalchemy.models_mixins import (
        AuthzVersionMixin as AuthzVersionMixin,
        RbacPermissionMixin as RbacPermissionMixin,
        RbacRoleMixin as RbacRoleMixin,
        RbacRoleParentMixin as RbacRoleParentMixin,
        RbacRolePermissionMixin as RbacRolePermissionMixin,
        RbacUserRoleMixin as RbacUserRoleMixin,
    )

__all__ = [
    "GLOBAL_SCOPE",
    "RbacRole",
    "RbacPermission",
    "RoleAssignment",
    "AbstractRbacRoleRepository",
    "AbstractRbacPermissionRepository",
    "AbstractRbacUserRoleRepository",
    "AbstractAuthzVersionRepository",
    "RbacError",
    "RoleNotFoundError",
    "RBAC_EXCEPTION_STATUS_MAP",
    "CompiledPermissionSet",
    "RbacRoleMixin",
    "RbacPermissionMixin",
    "RbacRolePermissionMixin",
    "RbacRoleParentMixin",
    "RbacUserRoleMixin",
    "AuthzVersionMixin",
]

_MIXINS = (
    "RbacRoleMixin",
    "RbacPermissionMixin",
    "RbacRolePermissionMixin",
    "RbacRoleParentMixin",
    "RbacUserRoleMixin",
    "AuthzVersionMixin",
)


def __getattr__(name: str) -> t.Any:
    """Los seis mixins, perezosos: importarlos arrastra sqlalchemy. Ver `organization`."""
    if name in _MIXINS:
        from hexcore.darwin.plugins.rbac.orms.sqlalchemy import models_mixins

        return getattr(models_mixins, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
