"""
Euphoria Kernel Core
Submódulo principal con entidades, eventos y repositorios.
"""

from .domain.base import BaseEntity
from .domain.auth.permissions import PermissionsRegistry
from .domain.auth.value_objects import TokenClaims
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
