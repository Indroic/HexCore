"""
Los modelos concretos de `rbac`. **Importar este módulo SÍ registra las seis tablas.**

Contraparte deliberada de `models_mixins`, igual que en el núcleo y en `organization`.
"""
from __future__ import annotations

import typing as t

from hexcore.darwin.plugins.rbac.orms.sqlalchemy.models_mixins import (
    DEFAULT_AUTHZ_VERSION_TABLE,
    DEFAULT_PERMISSION_TABLE,
    DEFAULT_ROLE_PARENT_TABLE,
    DEFAULT_ROLE_PERMISSION_TABLE,
    DEFAULT_ROLE_TABLE,
    DEFAULT_USER_ROLE_TABLE,
    AuthzVersionMixin,
    RbacPermissionMixin,
    RbacRoleMixin,
    RbacRoleParentMixin,
    RbacRolePermissionMixin,
    RbacUserRoleMixin,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

__all__ = [
    "PLUGIN_MODELS",
    "RbacRoleModel",
    "RbacPermissionModel",
    "RbacRolePermissionModel",
    "RbacRoleParentModel",
    "RbacUserRoleModel",
    "AuthzVersionModel",
    "RBAC_MODELS",
    "create_rbac_tables",
]


class RbacRoleModel(RbacRoleMixin, Base):
    """Tabla `darwin_rbac_role`."""

    __tablename__ = DEFAULT_ROLE_TABLE


class RbacPermissionModel(RbacPermissionMixin, Base):
    """Tabla `darwin_rbac_permission`."""

    __tablename__ = DEFAULT_PERMISSION_TABLE


class RbacRolePermissionModel(RbacRolePermissionMixin, Base):
    """Tabla `darwin_rbac_role_permission`."""

    __tablename__ = DEFAULT_ROLE_PERMISSION_TABLE


class RbacRoleParentModel(RbacRoleParentMixin, Base):
    """Tabla `darwin_rbac_role_parent`."""

    __tablename__ = DEFAULT_ROLE_PARENT_TABLE


class RbacUserRoleModel(RbacUserRoleMixin, Base):
    """Tabla `darwin_rbac_user_role`."""

    __tablename__ = DEFAULT_USER_ROLE_TABLE


class AuthzVersionModel(AuthzVersionMixin, Base):
    """Tabla `darwin_authz_version`."""

    __tablename__ = DEFAULT_AUTHZ_VERSION_TABLE


#: En orden de creación: `role_permission`, `role_parent` y `user_role` referencian a `role` (y
#: `user_role` también a `user`) por FK, así que crearlas antes falla en cualquier backend que
#: valide las FKs. `authz_version` no depende de nada y puede ir en cualquier lugar.
RBAC_MODELS = (
    RbacRoleModel,
    RbacPermissionModel,
    RbacRolePermissionModel,
    RbacRoleParentModel,
    RbacUserRoleModel,
    AuthzVersionModel,
)


async def create_rbac_tables(
    engine: t.Any = None, *, models: t.Sequence[type] | None = None
) -> None:
    """
    Crea las tablas del plugin si no existen. Idempotente.

    Atajo para tests y desarrollo, igual que `create_identity_tables`. **En producción usá
    Alembic.**

    Uso::

        await create_rbac_tables()
    """
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import get_engine

    objetivo: t.Sequence[t.Any] = models if models is not None else RBAC_MODELS
    tablas: list[t.Any] = [modelo.__table__ for modelo in objetivo]

    target = engine or get_engine()
    async with target.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=tablas)


# ── El contrato de esquema ───────────────────────────────────────────
# El nombre neutro que busca `ensure_identity_schema_loaded`. Ver el docstring homólogo en
# `organization/orms/sqlalchemy/models.py`.
PLUGIN_MODELS = RBAC_MODELS
