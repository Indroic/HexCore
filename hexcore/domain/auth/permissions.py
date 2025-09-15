from __future__ import annotations

from enum import Enum
from typing import Set, Dict, Optional



"""
Infraestructura orientada a objetos para registro y consulta dinámica de permisos.

Este módulo NO define permisos concretos. Cada proyecto/servidor debe registrar sus propios permisos
usando el método register_permission() de la clase PermissionsRegistry al iniciar.

Provee utilidades para:
- Registrar permisos (register_permission)
- Consultar permisos registrados (get_permissions_registry, get_all_permission_values, get_permission_by_name)
- Construir un Enum dinámico si se requiere (build_permissions_enum)

Ejemplo de uso:
    from hexcore.domain.auth.permissions import PermissionsRegistry
    permissions = PermissionsRegistry()
    permissions.register_permission("USERS_VIEW", "users.view")
    ...
    todos = permissions.get_all_permission_values()
"""


class PermissionsRegistry:
    """
    Clase para registrar y consultar permisos de forma dinámica.
    Cada instancia mantiene su propio registro de permisos.
    """
    def __init__(self):
        self._permissions_registry: Dict[str, str] = {}

    def register_permission(self, name: str, value: Optional[str] = None) -> None:
        """
        Registra un nuevo permiso en el sistema.
        name: nombre identificador (ej: 'USERS_INVITE')
        value: valor string (ej: 'users.invite'). Si no se pasa, se usa name.lower().
        """
        if value is None:
            value = name.lower()
        self._permissions_registry[name] = value

    def register_permissions(self, permissions: Dict[str, Optional[str]]) -> None:
        """
        Registra múltiples permisos en el sistema.
        """
        for name, value in permissions.items():
            self.register_permission(name, value)

    def get_permissions_registry(self) -> Dict[str, str]:
        """Devuelve el registro actual de permisos (nombre: valor)."""
        return dict(self._permissions_registry)

    def get_all_permission_values(self) -> Set[str]:
        """
        Devuelve un conjunto con todos los valores de los permisos registrados.
        """
        return set(self._permissions_registry.values())

    def get_permission_by_name(self, name: str) -> Optional[str]:
        """Devuelve el valor del permiso por su nombre."""
        return self._permissions_registry.get(name)

    def build_permissions_enum(self) -> Enum:
        """
        Construye un Enum dinámico con los permisos actuales.
        """
        return Enum("PermissionsEnum", {k: v for k, v in self._permissions_registry.items()}, type=str)
