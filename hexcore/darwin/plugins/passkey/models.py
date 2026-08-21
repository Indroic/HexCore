"""
Los modelos concretos de `passkey`. **Importar este módulo SÍ registra las dos tablas.**

Contraparte deliberada de `models_mixins`, igual que `hexcore.darwin.infrastructure.models` lo es
de los mixins del núcleo.
"""
from __future__ import annotations

import typing as t

from hexcore.darwin.plugins.passkey.models_mixins import (
    DEFAULT_PASSKEY_CHALLENGE_TABLE,
    DEFAULT_PASSKEY_TABLE,
    PasskeyChallengeMixin,
    PasskeyMixin,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

__all__ = [
    "PasskeyModel",
    "PasskeyChallengeModel",
    "PASSKEY_MODELS",
    "create_passkey_tables",
]


class PasskeyModel(PasskeyMixin, Base):
    """Tabla `darwin_passkey`. No hereda `BaseModel[T]`: ver el docstring del mixin."""

    __tablename__ = DEFAULT_PASSKEY_TABLE


class PasskeyChallengeModel(PasskeyChallengeMixin, Base):
    """Tabla `darwin_passkey_challenge`."""

    __tablename__ = DEFAULT_PASSKEY_CHALLENGE_TABLE


PASSKEY_MODELS = (PasskeyModel, PasskeyChallengeModel)


async def create_passkey_tables(
    engine: t.Any = None, *, models: t.Sequence[type] | None = None
) -> None:
    """
    Crea las tablas del plugin si no existen. Idempotente.

    Atajo para tests y desarrollo, igual que `create_identity_tables`. **En producción usá
    Alembic.** La migración equivalente de las credenciales::

        op.create_table(
            "darwin_passkey",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("credential_id", sa.String(512), nullable=False),
            sa.Column("public_key", sa.Text(), nullable=False),
            sa.Column("sign_count", sa.BigInteger(), nullable=False),
            sa.Column("name", sa.String(128), nullable=True),
            sa.Column("aaguid", sa.String(64), nullable=True),
            sa.Column("backed_up", sa.Boolean(), nullable=False),
            sa.Column("transports", sa.JSON(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_darwin_passkey"),
            sa.UniqueConstraint(
                "credential_id", name="uq_darwin_passkey_credential_id"
            ),
            sa.ForeignKeyConstraint(
                ["user_id"], ["darwin_user.id"], ondelete="CASCADE",
                name="fk_darwin_passkey_user_id_darwin_user",
            ),
        )
        op.create_index("ix_darwin_passkey_user_id", "darwin_passkey", ["user_id"])

    Y la de los desafíos::

        op.create_table(
            "darwin_passkey_challenge",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("challenge", sa.String(128), nullable=False),
            sa.Column("purpose", sa.String(16), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_darwin_passkey_challenge"),
            sa.UniqueConstraint(
                "challenge", name="uq_darwin_passkey_challenge_challenge"
            ),
        )

    Uso::

        await create_passkey_tables()
    """
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import get_engine

    objetivo: t.Sequence[t.Any] = models if models is not None else PASSKEY_MODELS
    tablas: list[t.Any] = [modelo.__table__ for modelo in objetivo]

    target = engine or get_engine()
    async with target.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=tablas)
