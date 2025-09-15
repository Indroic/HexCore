"""
Euphoria Kernel Core
Submódulo principal con entidades, eventos y repositorios.
"""

from .domain.base import BaseEntity
from .domain.auth.permissions import PermissionsEnum
from .domain.auth.value_objects import TokenClaims
from .application.dtos.base import DTO
from .domain.events import (
    DomainEvent,
    EntityCreatedEvent,
    EntityDeletedEvent,
    EntityUpdatedEvent,
)
from .infrastructure.repositories.base import (
    IBaseRepository,
    IBaseTenantAwareRepository,
    BaseSQLRepository,
    BaseSQLTenantAwareRepository,
)
from .infrastructure import cli
from .infrastructure import cache
from . import config

__all__ = [
    "BaseEntity",
    "PermissionsEnum",
    "TokenClaims",
    "DTO",
    "DomainEvent",
    "EntityCreatedEvent",
    "EntityDeletedEvent",
    "EntityUpdatedEvent",
    "IBaseRepository",
    "IBaseTenantAwareRepository",
    "BaseSQLRepository",
    "BaseSQLTenantAwareRepository",
    "cli",
    "cache",
    "config",
]
