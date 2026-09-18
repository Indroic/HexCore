"""
Los mixins de `drbac`. **Importarlos no registra ninguna tabla.**

Cuatro tablas: la política, sus reglas (tabla aparte, por `position` — al revés que Mongo, que
las embebe: ver el docstring de `orms/beanie/repository.py`), los bindings de rol contextual, y
el contador de versión propio del plugin (no el de `rbac`: ver el docstring de
`domain.AbstractDrbacAuthzVersionRepository`). Mismo criterio que el resto de Darwin: el
consumidor declara las clases concretas en su paquete ``models/``.

Uso, en el paquete del consumidor::

    # myapp/models/identity.py
    from hexcore.darwin.plugins.drbac import (
        DrbacAuthzVersionMixin,
        DrbacPolicyMixin,
        DrbacRoleBindingMixin,
        DrbacRuleMixin,
    )
    from hexcore.sql import Base

    class DrbacPolicy(DrbacPolicyMixin, Base):
        __tablename__ = "darwin_drbac_policy"

    class DrbacRule(DrbacRuleMixin, Base):
        __tablename__ = "darwin_drbac_rule"

    class DrbacRoleBinding(DrbacRoleBindingMixin, Base):
        __tablename__ = "darwin_drbac_role_binding"

    class DrbacAuthzVersion(DrbacAuthzVersionMixin, Base):
        __tablename__ = "darwin_drbac_authz_version"
"""
from __future__ import annotations

import typing as t
from datetime import datetime
from uuid import UUID as PythonUUID, uuid4

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins import (
    DEFAULT_USER_TABLE,
    JSON_PORTABLE,
    TimestampMixin,
)

__all__ = [
    "DEFAULT_POLICY_TABLE",
    "DEFAULT_RULE_TABLE",
    "DEFAULT_ROLE_BINDING_TABLE",
    "DEFAULT_AUTHZ_VERSION_TABLE",
    "DrbacPolicyMixin",
    "DrbacRuleMixin",
    "DrbacRoleBindingMixin",
    "DrbacAuthzVersionMixin",
]

DEFAULT_POLICY_TABLE = "darwin_drbac_policy"
DEFAULT_RULE_TABLE = "darwin_drbac_rule"
DEFAULT_ROLE_BINDING_TABLE = "darwin_drbac_role_binding"
DEFAULT_AUTHZ_VERSION_TABLE = "darwin_drbac_authz_version"


class DrbacPolicyMixin(TimestampMixin):
    """`UNIQUE(scope_key, name)`, mismo criterio que `RbacRoleMixin`: el mismo nombre de
    política puede existir en dos scopes distintos."""

    __tablename__: t.ClassVar[str]
    __darwin_user_table__: t.ClassVar[str] = DEFAULT_USER_TABLE

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)
    scope_key: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(String(512), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    @declared_attr
    def created_by(cls) -> Mapped[PythonUUID | None]:  # noqa: N805
        return mapped_column(
            ForeignKey(f"{cls.__darwin_user_table__}.id", ondelete="SET NULL"), nullable=True
        )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (UniqueConstraint("scope_key", "name", name=f"uq_{nombre}_scope_name"),)


class DrbacRuleMixin:
    """
    Columnas de `drbac_rule`.

    `actions`/`condition` van en `JSON_PORTABLE`: `actions` es una lista corta de strings, y
    `condition` es el AST de `conditions.py` volcado con `dump_condition` — la misma forma que
    viaja a la API y al cliente TypeScript (Fase F6), así que guardarlo tal cual evita un
    formato intermedio que sólo esta tabla entendería.
    """

    __tablename__: t.ClassVar[str]
    __darwin_drbac_policy_table__: t.ClassVar[str] = DEFAULT_POLICY_TABLE

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)

    @declared_attr
    def policy_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        return mapped_column(
            ForeignKey(f"{cls.__darwin_drbac_policy_table__}.id", ondelete="CASCADE"),
            nullable=False,
        )

    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    effect: Mapped[str] = mapped_column(String(8), nullable=False)
    actions: Mapped[list[str]] = mapped_column(JSON_PORTABLE, nullable=False, default=list)
    resource_type: Mapped[str] = mapped_column(String(128), nullable=False)
    condition: Mapped[dict[str, t.Any] | None] = mapped_column(JSON_PORTABLE, nullable=True)
    client_evaluable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            Index(f"ix_{nombre}_policy_id_position", "policy_id", "position"),
            Index(f"ix_{nombre}_resource_type", "resource_type"),
        )


class DrbacRoleBindingMixin:
    """Columnas de `drbac_role_binding`. Sin FK a ningún rol de `rbac` — ver el docstring de
    `domain.RoleBinding`: `role_name` es un string suelto."""

    __tablename__: t.ClassVar[str]
    __darwin_user_table__: t.ClassVar[str] = DEFAULT_USER_TABLE

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)

    @declared_attr
    def subject_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        return mapped_column(
            ForeignKey(f"{cls.__darwin_user_table__}.id", ondelete="CASCADE"), nullable=False
        )

    role_name: Mapped[str] = mapped_column(String(128), nullable=False)
    scope_path: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    condition: Mapped[dict[str, t.Any] | None] = mapped_column(JSON_PORTABLE, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @declared_attr
    def granted_by(cls) -> Mapped[PythonUUID | None]:  # noqa: N805
        return mapped_column(
            ForeignKey(f"{cls.__darwin_user_table__}.id", ondelete="SET NULL"), nullable=True
        )

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            Index(f"ix_{nombre}_subject_id_scope_path", "subject_id", "scope_path"),
            Index(f"ix_{nombre}_expires_at", "expires_at"),
        )


class DrbacAuthzVersionMixin:
    """Columnas de `drbac_authz_version`: el contador monotónico por scope, propio del plugin.
    Mismo criterio que `rbac.AuthzVersionMixin` — ver el docstring de
    `domain.AbstractDrbacAuthzVersionRepository`."""

    __tablename__: t.ClassVar[str]

    scope_key: Mapped[str] = mapped_column(String(255), primary_key=True, default="")
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
