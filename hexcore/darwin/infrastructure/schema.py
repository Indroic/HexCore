"""
Creación de tablas y validación del modelo de usuario.

`create_identity_tables` es el atajo para desarrollo y tests, con el `op.create_table`
equivalente en el docstring — mismo patrón que `create_cron_tables`. En producción se usa
Alembic.

`validate_user_model` falla al **arrancar** y no en el primer login. Mismo criterio que
`CQRSFactory._assert_enqueuer_for_background_commands`: un error de cableado descubierto en
el primer request de producción ya llegó tarde.
"""
from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = [
    "create_identity_tables",
    "drop_identity_tables",
    "identity_tables",
    "validate_user_model",
    "ensure_identity_schema_loaded",
]


def identity_tables(models: t.Sequence[type] | None = None) -> list[t.Any]:
    """
    Los objetos `Table` de los modelos de identidad.

    Args:
        models: Los modelos a considerar. Por defecto, los seis concretos de `models.py`.
            Pasá los tuyos si extendiste el esquema.
    """
    from hexcore.darwin.infrastructure.models import IDENTITY_MODELS

    objetivo: t.Sequence[t.Any] = (
        models if models is not None else IDENTITY_MODELS
    )
    # `t.Any` y no `type`: `__table__` lo agrega el mapeo declarativo de SQLAlchemy en
    # tiempo de ejecución, así que no está en el tipo estático de una `type` cualquiera. El
    # contrato lo garantiza `validate_user_model`.
    return [modelo.__table__ for modelo in objetivo]


async def create_identity_tables(
    engine: "AsyncEngine | None" = None,
    *,
    models: t.Sequence[type] | None = None,
) -> None:
    """
    Crea las tablas de identidad si no existen. Idempotente.

    Atajo para entornos sin migraciones: tests, desarrollo, un script. **En producción usá
    Alembic.** La migración equivalente de la tabla de usuarios::

        op.create_table(
            "darwin_user",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("email", sa.String(320), nullable=False),
            sa.Column("email_verified", sa.Boolean(), nullable=False),
            sa.Column("name", sa.String(255), nullable=True),
            sa.Column("image", sa.String(2048), nullable=True),
            sa.Column("token_generation", sa.Integer(), nullable=False),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("extra", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_darwin_user"),
            sa.UniqueConstraint("email", name="uq_darwin_user_email"),
        )
        op.create_index("ix_darwin_user_created_at", "darwin_user", ["created_at"])

    Y la de sesiones, que es la que lleva los dos principales::

        op.create_table(
            "darwin_session",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("actor_user_id", sa.Uuid(), nullable=False),
            sa.Column("subject_user_id", sa.Uuid(), nullable=False),
            sa.Column("token_hash", sa.String(64), nullable=False),
            sa.Column("family_id", sa.Uuid(), nullable=False),
            sa.Column("transport", sa.String(16), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            ...
            sa.ForeignKeyConstraint(
                ["actor_user_id"], ["darwin_user.id"], ondelete="CASCADE",
                name="fk_darwin_session_actor_user_id_darwin_user",
            ),
            sa.UniqueConstraint("token_hash", name="uq_darwin_session_token_hash"),
        )

    Uso::

        await create_identity_tables()
    """
    from hexcore.infrastructure.repositories.orms.sqlalchemy import Base
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import get_engine

    target = engine or get_engine()
    async with target.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all, tables=identity_tables(models)
        )


async def drop_identity_tables(
    engine: "AsyncEngine | None" = None,
    *,
    models: t.Sequence[type] | None = None,
) -> None:
    """
    Borra las tablas de identidad. **Sólo para tests.**

    En orden inverso al de `IDENTITY_MODELS`, porque `session` y `account` referencian a
    `user` por FK y borrarla primero falla en cualquier backend que valide las FKs.
    """
    from hexcore.infrastructure.repositories.orms.sqlalchemy import Base
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import get_engine

    target = engine or get_engine()
    async with target.begin() as connection:
        await connection.run_sync(
            Base.metadata.drop_all, tables=list(reversed(identity_tables(models)))
        )


def validate_user_model(model: type) -> None:
    """
    Valida el modelo de usuario configurado. Falla al arrancar, no en el primer login.

    Rechaza tres cosas:

    1. **Heredar `BaseModel[T]`.** Es la regla 1 de `models_mixins`: el UoW le pediría una
       entidad de dominio y explotaría *después* del commit, dejando la fila escrita y
       devolviendo 500.
    2. **No componer `UserMixin`.** Sin sus columnas, los flujos de auth fallan con un
       `AttributeError` en runtime en vez de un error claro acá.
    3. **No estar mapeado.** Una clase que no llegó a `Base` no tiene tabla.

    Raises:
        TypeError: con el reemplazo copiable en el mensaje.

    Uso::

        validate_user_model(config.user_model)
    """
    from hexcore.darwin.infrastructure.models_mixins import UserMixin
    from hexcore.infrastructure.repositories.orms.sqlalchemy import Base, BaseModel

    nombre = getattr(model, "__name__", repr(model))

    if issubclass(model, BaseModel):
        raise TypeError(
            f"'{nombre}' hereda de `hexcore.sql.BaseModel`, y los modelos de identidad no "
            f"pueden.\n\n"
            f"`BaseModel.get_domain_entity()` no tiene default, y el Unit of Work lo llama "
            f"para todo `BaseModel` que la sesión tenga trackeado. Una fila insertada sin "
            f"`set_domain_entity()` hace explotar `commit()` DESPUÉS de que la transacción "
            f"ya se confirmó, y nada rollbackea: queda la fila escrita y el usuario recibe "
            f"un 500.\n\n"
            f"Heredá del mixin y de `Base`:\n\n"
            f"    from hexcore.darwin import UserMixin\n"
            f"    from hexcore.sql import Base\n\n"
            f"    class {nombre}(UserMixin, Base):\n"
            f'        __tablename__ = "darwin_user"\n'
        )

    if not issubclass(model, UserMixin):
        raise TypeError(
            f"'{nombre}' no compone `UserMixin`, así que le faltan las columnas que los "
            f"flujos de autenticación leen (email, email_verified, token_generation, "
            f"locked_until).\n\n"
            f"    from hexcore.darwin import UserMixin\n\n"
            f"    class {nombre}(UserMixin, Base):\n"
            f'        __tablename__ = "darwin_user"\n'
        )

    if not issubclass(model, Base):
        raise TypeError(
            f"'{nombre}' no hereda de `hexcore.sql.Base`, así que no está mapeado a ninguna "
            f"tabla. Agregá `Base` a sus bases: `class {nombre}(UserMixin, Base):`"
        )


def ensure_identity_schema_loaded() -> list[str]:
    """
    Importa los modelos de identidad para que entren en `Base.metadata`.

    Para el `env.py` de Alembic, junto a `ensure_framework_models_loaded()`. Sin esto, un
    proyecto que use el esquema por defecto sin importarlo desde su paquete ``models/``
    tendría las tablas en la base y ausentes del metadata — y `--autogenerate` les emitiría
    ``op.drop_table``.

    Devuelve una lista vacía si el extra `[darwin]` no está instalado: sin sqlalchemy no hay
    metadata que poblar, y eso no es un error.

    Uso, en el `env.py`::

        ensure_framework_models_loaded()
        ensure_identity_schema_loaded()
        import_all_models(models)
        target_metadata = Base.metadata
    """
    import importlib

    try:
        importlib.import_module("hexcore.darwin.infrastructure.models")
    except ImportError:
        return []
    return ["hexcore.darwin.infrastructure.models"]
