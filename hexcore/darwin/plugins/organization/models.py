"""
Los modelos concretos de `organization`. **Importar este módulo SÍ registra las tres tablas.**

Contraparte deliberada de `models_mixins`, igual que `hexcore.darwin.infrastructure.models` lo es
de los mixins del núcleo.
"""
from __future__ import annotations

import typing as t

from hexcore.darwin.plugins.organization.models_mixins import (
    DEFAULT_INVITATION_TABLE,
    DEFAULT_MEMBER_TABLE,
    DEFAULT_ORGANIZATION_TABLE,
    InvitationMixin,
    MemberMixin,
    OrganizationMixin,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

__all__ = [
    "OrganizationModel",
    "MemberModel",
    "InvitationModel",
    "ORGANIZATION_MODELS",
    "create_organization_tables",
]


class OrganizationModel(OrganizationMixin, Base):
    """Tabla `darwin_organization`. No hereda `BaseModel[T]`: ver el docstring del mixin."""

    __tablename__ = DEFAULT_ORGANIZATION_TABLE


class MemberModel(MemberMixin, Base):
    """Tabla `darwin_member`."""

    __tablename__ = DEFAULT_MEMBER_TABLE


class InvitationModel(InvitationMixin, Base):
    """Tabla `darwin_invitation`."""

    __tablename__ = DEFAULT_INVITATION_TABLE


#: En orden de creación: `member` e `invitation` referencian a `organization` por FK, así que
#: crearlas antes falla en cualquier backend que valide las FKs.
ORGANIZATION_MODELS = (OrganizationModel, MemberModel, InvitationModel)


async def create_organization_tables(
    engine: t.Any = None, *, models: t.Sequence[type] | None = None
) -> None:
    """
    Crea las tablas del plugin si no existen. Idempotente.

    Atajo para tests y desarrollo, igual que `create_identity_tables`. **En producción usá
    Alembic.** La migración equivalente de las membresías, que es la que lleva los índices que
    importan::

        op.create_table(
            "darwin_member",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("organization_id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("role", sa.String(32), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_darwin_member"),
            sa.UniqueConstraint(
                "organization_id", "user_id", name="uq_darwin_member_org_user"
            ),
            sa.ForeignKeyConstraint(
                ["organization_id"], ["darwin_organization.id"], ondelete="CASCADE",
                name="fk_darwin_member_organization_id_darwin_organization",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"], ["darwin_user.id"], ondelete="CASCADE",
                name="fk_darwin_member_user_id_darwin_user",
            ),
        )
        op.create_index("ix_darwin_member_user_id", "darwin_member", ["user_id"])
        # El índice del conteo de owners, que es la consulta de la invariante mas importante.
        op.create_index(
            "ix_darwin_member_organization_id_role",
            "darwin_member",
            ["organization_id", "role"],
        )

    Uso::

        await create_organization_tables()
    """
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import get_engine

    objetivo: t.Sequence[t.Any] = (
        models if models is not None else ORGANIZATION_MODELS
    )
    tablas: list[t.Any] = [modelo.__table__ for modelo in objetivo]

    target = engine or get_engine()
    async with target.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=tablas)
