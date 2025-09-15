"""
Euphoria Kernel
Módulo base para compartir funcionalidades del kernel.
"""

from .hexcore.infrastructure import cli
from .hexcore import (
    IBaseRepository,
    IBaseTenantAwareRepository,
    DomainEvent,
    EntityCreatedEvent,
    EntityDeletedEvent,
    EntityUpdatedEvent,
    DTO,
    TokenClaims,
    BaseEntity,
    PermissionsEnum,
)

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
    "cli",
]
