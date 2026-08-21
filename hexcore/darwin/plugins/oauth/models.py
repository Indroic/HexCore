"""
El modelo concreto de `oauth`. **Importar este módulo SÍ registra `darwin_oauth_state`.**

Contraparte deliberada de `models_mixins`, igual que `hexcore.darwin.infrastructure.models` lo es
de los mixins del núcleo.
"""
from __future__ import annotations

import typing as t

from hexcore.darwin.plugins.oauth.models_mixins import (
    DEFAULT_OAUTH_STATE_TABLE,
    OAuthStateMixin,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

__all__ = ["OAuthStateModel", "OAUTH_MODELS", "create_oauth_tables"]


class OAuthStateModel(OAuthStateMixin, Base):
    """Tabla `darwin_oauth_state`. No hereda `BaseModel[T]`: ver el docstring del mixin."""

    __tablename__ = DEFAULT_OAUTH_STATE_TABLE


OAUTH_MODELS = (OAuthStateModel,)


async def create_oauth_tables(
    engine: t.Any = None, *, models: t.Sequence[type] | None = None
) -> None:
    """
    Crea la tabla del plugin si no existe. Idempotente.

    Atajo para tests y desarrollo, igual que `create_identity_tables`. **En producción usá
    Alembic.** La migración equivalente::

        op.create_table(
            "darwin_oauth_state",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("provider_id", sa.String(64), nullable=False),
            sa.Column("state_hash", sa.String(64), nullable=False),
            sa.Column("code_verifier_encrypted", sa.Text(), nullable=False),
            sa.Column("redirect_uri", sa.String(2048), nullable=False),
            sa.Column("link_user_id", sa.Uuid(), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_darwin_oauth_state"),
            sa.UniqueConstraint("state_hash", name="uq_darwin_oauth_state_state_hash"),
            sa.ForeignKeyConstraint(
                ["link_user_id"], ["darwin_user.id"], ondelete="CASCADE",
                name="fk_darwin_oauth_state_link_user_id_darwin_user",
            ),
        )
        op.create_index(
            "ix_darwin_oauth_state_expires_at", "darwin_oauth_state", ["expires_at"]
        )

    Uso::

        await create_oauth_tables()
    """
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import get_engine

    objetivo: t.Sequence[t.Any] = models if models is not None else OAUTH_MODELS
    tablas: list[t.Any] = [modelo.__table__ for modelo in objetivo]

    target = engine or get_engine()
    async with target.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=tablas)
