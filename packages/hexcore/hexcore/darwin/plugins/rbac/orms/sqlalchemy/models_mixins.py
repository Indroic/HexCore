"""
Los mixins de `rbac`. **Importarlos no registra ninguna tabla.**

Seis tablas: el rol, el catálogo de permisos, dos tablas de unión (permisos directos del rol y
de quién hereda), la asignación de rol a usuario, y el contador de versión por scope. Mismo
criterio que el núcleo y que `organization`: el consumidor declara las clases concretas en su
propio paquete ``models/``, porque un `--autogenerate` que no ve la tabla le emite
``op.drop_table``.

Uso, en el paquete del consumidor::

    # myapp/models/identity.py
    from hexcore.darwin.plugins.rbac import (
        AuthzVersionMixin,
        RbacPermissionMixin,
        RbacRoleMixin,
        RbacRoleParentMixin,
        RbacRolePermissionMixin,
        RbacUserRoleMixin,
    )
    from hexcore.sql import Base

    class RbacRole(RbacRoleMixin, Base):
        __tablename__ = "darwin_rbac_role"

    class RbacPermission(RbacPermissionMixin, Base):
        __tablename__ = "darwin_rbac_permission"

    class RbacRolePermission(RbacRolePermissionMixin, Base):
        __tablename__ = "darwin_rbac_role_permission"

    class RbacRoleParent(RbacRoleParentMixin, Base):
        __tablename__ = "darwin_rbac_role_parent"

    class RbacUserRole(RbacUserRoleMixin, Base):
        __tablename__ = "darwin_rbac_user_role"

    class AuthzVersion(AuthzVersionMixin, Base):
        __tablename__ = "darwin_authz_version"
"""
from __future__ import annotations

import typing as t
from datetime import datetime
from uuid import UUID as PythonUUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins import (
    DEFAULT_USER_TABLE,
    TimestampMixin,
)

__all__ = [
    "DEFAULT_ROLE_TABLE",
    "DEFAULT_PERMISSION_TABLE",
    "DEFAULT_ROLE_PERMISSION_TABLE",
    "DEFAULT_ROLE_PARENT_TABLE",
    "DEFAULT_USER_ROLE_TABLE",
    "DEFAULT_AUTHZ_VERSION_TABLE",
    "RbacRoleMixin",
    "RbacPermissionMixin",
    "RbacRolePermissionMixin",
    "RbacRoleParentMixin",
    "RbacUserRoleMixin",
    "AuthzVersionMixin",
]

DEFAULT_ROLE_TABLE = "darwin_rbac_role"
DEFAULT_PERMISSION_TABLE = "darwin_rbac_permission"
DEFAULT_ROLE_PERMISSION_TABLE = "darwin_rbac_role_permission"
DEFAULT_ROLE_PARENT_TABLE = "darwin_rbac_role_parent"
DEFAULT_USER_ROLE_TABLE = "darwin_rbac_user_role"
DEFAULT_AUTHZ_VERSION_TABLE = "darwin_authz_version"


class RbacRoleMixin(TimestampMixin):
    """
    Columnas de `rbac_role`.

    `UNIQUE(scope_key, name)`, no `UNIQUE(name)`: el mismo nombre de rol puede existir en dos
    scopes distintos —`"admin"` en `org:1` y en `org:2` son roles diferentes— y `scope_key=""`
    (global) participa del mismo constraint como un scope más.
    """

    __tablename__: t.ClassVar[str]

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    #: Sincronizado desde `RoleRegistry`. No editable por API — ver el docstring de `RbacRole`.
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            UniqueConstraint("scope_key", "name", name=f"uq_{nombre}_scope_name"),
        )


class RbacPermissionMixin:
    """
    Columnas de `rbac_permission`: el catálogo. Sin scope — un permiso (`"invoice.approve"`) es
    el mismo concepto en todos los tenants; lo que varía por scope es **quién** lo tiene, y eso
    lo guarda `rbac_role_permission` a través del rol.
    """

    __tablename__: t.ClassVar[str]

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (UniqueConstraint("key", name=f"uq_{nombre}_key"),)


class RbacRolePermissionMixin:
    """
    Columnas de `rbac_role_permission`: los permisos **directos** de un rol.

    Clave primaria compuesta `(role_id, permission_id)` y no un `id` propio: es una tabla de
    unión pura, sin atributos propios, y la clave compuesta es lo que hace que asignar el mismo
    permiso al mismo rol dos veces sea un `UPSERT` trivial en vez de un `UNIQUE` aparte.
    """

    __tablename__: t.ClassVar[str]
    __darwin_rbac_role_table__: t.ClassVar[str] = DEFAULT_ROLE_TABLE
    __darwin_rbac_permission_table__: t.ClassVar[str] = DEFAULT_PERMISSION_TABLE

    @declared_attr
    def role_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        return mapped_column(
            ForeignKey(f"{cls.__darwin_rbac_role_table__}.id", ondelete="CASCADE"),
            primary_key=True,
        )

    @declared_attr
    def permission_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        return mapped_column(
            ForeignKey(f"{cls.__darwin_rbac_permission_table__}.id", ondelete="CASCADE"),
            primary_key=True,
        )


class RbacRoleParentMixin:
    """
    Columnas de `rbac_role_parent`: de quién hereda un rol.

    Auto-referencia a `rbac_role` dos veces (`role_id`, `parent_id`). La detección de ciclos
    **no** vive acá: es del servicio (Fase F2), con el mismo DFS que `RoleRegistry` ya usa para
    los roles de código. Un `CHECK` de SQL no puede expresar "sin ciclos transitivos".
    """

    __tablename__: t.ClassVar[str]
    __darwin_rbac_role_table__: t.ClassVar[str] = DEFAULT_ROLE_TABLE

    @declared_attr
    def role_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        return mapped_column(
            ForeignKey(f"{cls.__darwin_rbac_role_table__}.id", ondelete="CASCADE"),
            primary_key=True,
        )

    @declared_attr
    def parent_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        return mapped_column(
            ForeignKey(f"{cls.__darwin_rbac_role_table__}.id", ondelete="CASCADE"),
            primary_key=True,
        )


class RbacUserRoleMixin:
    """
    Columnas de `rbac_user_role`: quién tiene qué rol, en qué scope, hasta cuándo.

    `UNIQUE(user_id, role_id, scope_key)` es lo que hace que asignar el mismo rol al mismo
    usuario en el mismo scope dos veces sea un error de integridad y no una fila duplicada que
    dos consultas concurrentes de "¿tiene este rol?" contarían distinto.
    """

    __tablename__: t.ClassVar[str]
    __darwin_user_table__: t.ClassVar[str] = DEFAULT_USER_TABLE
    __darwin_rbac_role_table__: t.ClassVar[str] = DEFAULT_ROLE_TABLE

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)

    @declared_attr
    def user_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        return mapped_column(
            ForeignKey(f"{cls.__darwin_user_table__}.id", ondelete="CASCADE"),
            nullable=False,
        )

    @declared_attr
    def role_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        return mapped_column(
            ForeignKey(f"{cls.__darwin_rbac_role_table__}.id", ondelete="CASCADE"),
            nullable=False,
        )

    scope_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")

    @declared_attr
    def granted_by(cls) -> Mapped[PythonUUID | None]:  # noqa: N805
        """
        Quién otorgó la asignación. Auditoría: sin esto, "¿quién le dio admin a X?" no tiene
        respuesta. Nullable porque un seed o una migración puede otorgar sin un actor humano.
        """
        return mapped_column(
            ForeignKey(f"{cls.__darwin_user_table__}.id", ondelete="SET NULL"),
            nullable=True,
        )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            UniqueConstraint(
                "user_id", "role_id", "scope_key", name=f"uq_{nombre}_user_role_scope"
            ),
            Index(f"ix_{nombre}_user_id_scope_key", "user_id", "scope_key"),
            Index(f"ix_{nombre}_role_id", "role_id"),
        )


class AuthzVersionMixin:
    """
    Columnas de `authz_version`: el contador monotónico por scope.

    `scope_key` es la **clave primaria**, no un `id` con `UNIQUE` aparte: la tabla no tiene
    ninguna otra identidad que el scope, y `bump()` hace un upsert sobre esa misma clave.
    """

    __tablename__: t.ClassVar[str]

    scope_key: Mapped[str] = mapped_column(String(255), primary_key=True, default="")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
