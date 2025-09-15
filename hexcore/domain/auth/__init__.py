"""
Submódulo de autenticación y permisos del kernel.
"""

from .permissions import (
    PermissionsRegistry,
)
from .value_objects import TokenClaims

__all__ = [
    "PermissionsRegistry",
    "TokenClaims",
]
