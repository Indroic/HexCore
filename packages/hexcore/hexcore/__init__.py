"""
Euphoria Kernel Core
Submódulo principal con entidades, eventos y repositorios.
"""
from __future__ import annotations

import typing as t

from .domain.base import BaseEntity
from .domain.events import (
    DomainEvent,
    EntityCreatedEvent,
    EntityDeletedEvent,
    EntityUpdatedEvent,
)
from .domain.repositories import IBaseRepository
from .infrastructure.repositories.base import (
    BaseSQLAlchemyRepository,
)
from .infrastructure import cli
from .infrastructure import cache
from .application.dtos.base import DTO
from . import config

if t.TYPE_CHECKING:
    # Sólo para el checker: en runtime los sirve el `__getattr__` de abajo, que avisa. Sin esto,
    # pyright reporta `reportUnsupportedDunderAll` porque los nombres están en `__all__` y no en
    # el módulo — y silenciarlo con un ignore esconde el caso real de un nombre mal escrito.
    from .domain.auth.permissions import PermissionsRegistry as PermissionsRegistry
    from .domain.auth.value_objects import TokenClaims as TokenClaims

from ._deprecation import deprecated_lazy_names

#: Los dos nombres que `hexcore.darwin` reemplaza.
#:
#: Siguen en `__all__` porque `from hexcore import TokenClaims` tiene que seguir funcionando —PEP
#: 562 cubre los `from`-imports— pero ya no se importan eager: los sirve el `__getattr__` de abajo,
#: que avisa en cada acceso.
#:
#: ⚠️ **No se aliasan a su reemplazo**, y esa es la decisión. `TokenClaims` tiene `client_id`
#: obligatorio, un default mutable en `scopes` y **no tiene `sid`** —sin el cual la revocación es
#: imposible por construcción—; `AccessTokenClaims` tiene otros campos y otros invariantes.
#: Devolver el nuevo donde el usuario espera el viejo rompería su código en la línea siguiente. Lo
#: que hace falta es que el viejo siga funcionando **y avise**.
_DEPRECADOS = {
    "PermissionsRegistry": "hexcore.darwin.RoleRegistry",
    "TokenClaims": "hexcore.darwin.AccessTokenClaims",
}


def _cargar_permissions_registry() -> t.Any:
    from .domain.auth.permissions import PermissionsRegistry

    return PermissionsRegistry


def _cargar_token_claims() -> t.Any:
    from .domain.auth.value_objects import TokenClaims

    return TokenClaims


__getattr__ = deprecated_lazy_names(
    __name__,
    _DEPRECADOS,
    {
        "PermissionsRegistry": _cargar_permissions_registry,
        "TokenClaims": _cargar_token_claims,
    },
)

__all__ = [
    "BaseEntity",
    "PermissionsRegistry",
    "TokenClaims",
    "DTO",
    "DomainEvent",
    "EntityCreatedEvent",
    "EntityDeletedEvent",
    "EntityUpdatedEvent",
    "IBaseRepository",
    "BaseSQLAlchemyRepository",
    "cli",
    "cache",
    "config",
]
