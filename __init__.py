"""
Euphoria Kernel
Módulo base para compartir funcionalidades del kernel.
"""

from .hexcore.infrastructure import cli
from .hexcore import (
    IBaseRepository,
    DomainEvent,
    EntityCreatedEvent,
    EntityDeletedEvent,
    EntityUpdatedEvent,
    DTO,
    TokenClaims,
    BaseEntity,
    PermissionsRegistry

)

__all__ = [
    "BaseEntity",
    "TokenClaims",
    "DTO",
    "DomainEvent",
    "EntityCreatedEvent",
    "EntityDeletedEvent",
    "EntityUpdatedEvent",
    "IBaseRepository",
    "cli",
    "PermissionsRegistry",
]
