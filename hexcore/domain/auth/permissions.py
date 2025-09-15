from __future__ import annotations
from enum import Enum

# --- Prefijos de Permisos ---
_ROLES = "roles"
_USERS = "users"
_TENANTS = "tenants"

_LOGISTICS_INVENTORY = "logistics.inventory"
_LOGISTICS_PRODUCTS = "logistics.products"

# --- Sufijos Comunes ---

_VIEW = "view"
_CREATE = "create"
_EDIT = "edit"
_DELETE = "delete"


class PermissionsEnum(str, Enum):
    """
    Catálogo central de todos los permisos del sistema.
    Esta es la ÚNICA fuente de la verdad para los permisos.
    El valor (string) es lo que se almacena en la base de datos.
    El formato es: <dominio>.<entidad>.<accion>
    """

    # -- SUPERUSER PERMISSION --

    SUPERUSER = "__all__"  # Permiso especial que otorga todos los permisos del sistema

    # --- Dominio: IAM (Identidad y Acceso) ---
    ROLES_VIEW = f"{_ROLES}.{_VIEW}"
    ROLES_CREATE = f"{_ROLES}.{_CREATE}"
    ROLES_EDIT = f"{_ROLES}.{_EDIT}"
    ROLES_DELETE = f"{_ROLES}.{_DELETE}"

    USERS_CREATE = f"{_USERS}.{_CREATE}"
    USERS_VIEW = f"{_USERS}.{_VIEW}"
    USERS_INVITE = f"{_USERS}.invite"
    USERS_EDIT = f"{_USERS}.{_EDIT}"
    USERS_DELETE = f"{_USERS}.{_DELETE}"
    USERS_ADD_ROLE = f"{_USERS}.add.rol"

    TENANTS_VIEW = f"{_TENANTS}.{_VIEW}"
    TENANTS_EDIT = f"{_TENANTS}.{_EDIT}"

    # --- Dominio: Logistics (Logística) ---
    LOGISTICS_INVENTORY_VIEW = f"{_LOGISTICS_INVENTORY}.{_VIEW}"
    LOGISTICS_INVENTORY_ADJUST = f"{_LOGISTICS_INVENTORY}.adjust"
    LOGISTICS_PRODUCTS_MANAGE = f"{_LOGISTICS_PRODUCTS}.manage"


# Permisos que No puede tener un propietario
OWNER_EXCLUDED_PERMISSIONS = {
    PermissionsEnum.TENANTS_EDIT,
    PermissionsEnum.TENANTS_VIEW,
}


def get_all_permission_values() -> set[str]:
    """
    Devuelve un conjunto con todos los valores de los permisos definidos en PermissionsEnum.
    Ideal para usar en el comando de sincronización.
    """
    return {p.value for p in PermissionsEnum}


def get_owner_permissions() -> set[str]:
    """
    Devuelve los permisos de un propietario.
    """
    return {p.value for p in PermissionsEnum} - OWNER_EXCLUDED_PERMISSIONS
