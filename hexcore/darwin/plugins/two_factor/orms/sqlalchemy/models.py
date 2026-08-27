"""
El modelo concreto de `two_factor`. **Importar este módulo SÍ registra `darwin_two_factor`.**

Contraparte deliberada de `models_mixins`, igual que `hexcore.darwin.infrastructure.orms.sqlalchemy.models` lo
es de los mixins del núcleo. Importalo desde tu paquete ``models/`` si te alcanza el esquema por
defecto; declarate tu propia clase concreta a partir del mixin si necesitás extenderlo.
"""
from __future__ import annotations

import typing as t

from hexcore.darwin.plugins.two_factor.orms.sqlalchemy.models_mixins import (
    DEFAULT_TWO_FACTOR_TABLE,
    TwoFactorMixin,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

__all__ = ["TwoFactorModel", "TWO_FACTOR_MODELS", "create_two_factor_tables", "PLUGIN_MODELS"]


class TwoFactorModel(TwoFactorMixin, Base):
    """Tabla `darwin_two_factor`. No hereda `BaseModel[T]`: ver el docstring del mixin."""

    __tablename__ = DEFAULT_TWO_FACTOR_TABLE


TWO_FACTOR_MODELS = (TwoFactorModel,)


async def create_two_factor_tables(
    engine: t.Any = None, *, models: t.Sequence[type] | None = None
) -> None:
    """
    Crea la tabla del plugin si no existe. Idempotente.

    Atajo para tests y desarrollo, igual que `create_identity_tables`. **En producción usá
    Alembic**: esto es idempotente pero no versiona nada, así que un cambio de esquema más
    adelante no tiene desde dónde migrar. La migración equivalente::

        op.create_table(
            "darwin_two_factor",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("secret_encrypted", sa.Text(), nullable=False),
            sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_used_step", sa.BigInteger(), nullable=True),
            sa.Column("failed_attempts", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_darwin_two_factor"),
            sa.UniqueConstraint("user_id", name="uq_darwin_two_factor_user_id"),
            sa.ForeignKeyConstraint(
                ["user_id"], ["darwin_user.id"], ondelete="CASCADE",
                name="fk_darwin_two_factor_user_id_darwin_user",
            ),
        )

    Uso::

        await create_two_factor_tables()
    """
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import get_engine

    # `t.Any` y no `type`: `__table__` lo agrega el mapeo declarativo en tiempo de ejecución,
    # así que no está en el tipo estático de una `type` cualquiera. Mismo criterio que
    # `identity_tables`.
    objetivo: t.Sequence[t.Any] = (
        models if models is not None else TWO_FACTOR_MODELS
    )
    tablas: list[t.Any] = [modelo.__table__ for modelo in objetivo]

    target = engine or get_engine()
    async with target.begin() as connection:
        await connection.run_sync(Base.metadata.create_all, tables=tablas)


# ── El contrato de esquema ───────────────────────────────────────────
# El nombre neutro que busca `ensure_identity_schema_loaded`, igual que `PasskeyRepository` es
# el nombre neutro que busca `plugin_repositories`. Sin un nombre igual en los cuatro plugins,
# juntar los esquemas obligaba al nucleo a conocerlos por nombre — que es exactamente el
# acoplamiento que la separacion en extras saco.
PLUGIN_MODELS = TWO_FACTOR_MODELS
