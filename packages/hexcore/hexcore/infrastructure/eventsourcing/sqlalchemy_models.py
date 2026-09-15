"""
Las tablas concretas del event store. **Importar este módulo sí las registra** en
`Base.metadata`.

Esa es toda la diferencia con `sqlalchemy_models_mixins`, y es lo que hace que este módulo
tenga que estar listado en `ensure_framework_models_loaded()`: una tabla que existe en la
base y no está en el metadata recibe un `op.drop_table` de `alembic revision --autogenerate`,
sin avisar. Para el event store eso no es "perder una caché" — es perder la fuente de verdad.

Requiere el extra ``[sql]``.
"""
from __future__ import annotations

import typing as t

from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

from .sqlalchemy_models_mixins import (
    EventStoreMixin,
    ProjectionCheckpointMixin,
    SnapshotMixin,
)

if t.TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine

__all__ = [
    "DEFAULT_EVENT_TABLE",
    "DEFAULT_SNAPSHOT_TABLE",
    "DEFAULT_CHECKPOINT_TABLE",
    "EventStoreModel",
    "SnapshotModel",
    "ProjectionCheckpointModel",
    "EVENTSTORE_MODELS",
    "create_eventstore_tables",
    "drop_eventstore_tables",
]

DEFAULT_EVENT_TABLE = "hexcore_event_store"
DEFAULT_SNAPSHOT_TABLE = "hexcore_snapshots"
DEFAULT_CHECKPOINT_TABLE = "hexcore_projection_checkpoints"


class EventStoreModel(EventStoreMixin, Base):
    """La tabla de eventos por defecto (`hexcore_event_store`)."""

    __tablename__ = DEFAULT_EVENT_TABLE


class SnapshotModel(SnapshotMixin, Base):
    """La tabla de snapshots por defecto (`hexcore_snapshots`)."""

    __tablename__ = DEFAULT_SNAPSHOT_TABLE


class ProjectionCheckpointModel(ProjectionCheckpointMixin, Base):
    """La tabla de checkpoints por defecto (`hexcore_projection_checkpoints`)."""

    __tablename__ = DEFAULT_CHECKPOINT_TABLE


#: En orden de creación. No hay claves foráneas entre ellas —un snapshot de un stream cuyos
#: eventos se archivaron sigue siendo válido, y un checkpoint no depende de ninguna fila—,
#: así que el orden es sólo por legibilidad.
EVENTSTORE_MODELS: tuple[t.Any, ...] = (
    EventStoreModel,
    SnapshotModel,
    ProjectionCheckpointModel,
)


async def create_eventstore_tables(
    engine: "AsyncEngine | None" = None,
    *,
    models: t.Sequence[t.Any] = EVENTSTORE_MODELS,
) -> None:
    """
    Crea las tablas del event store si no existen.

    Atajo para entornos sin migraciones (tests, desarrollo, un script). En producción con
    Alembic, la migración equivalente de la tabla principal es::

        op.create_table(
            "hexcore_event_store",
            sa.Column("global_position", sa.BigInteger(), primary_key=True,
                      autoincrement=True),
            sa.Column("event_id", sa.Uuid(), nullable=False),
            sa.Column("stream_id", sa.String(255), nullable=False),
            sa.Column("stream_type", sa.String(128), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(512), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("occurred_on", sa.DateTime(timezone=True), nullable=False),
            sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        )
        op.create_unique_constraint(
            "uq_hexcore_event_store_stream_version",
            "hexcore_event_store", ["stream_id", "version"],
        )
        op.create_index(
            "ix_hexcore_event_store_stream",
            "hexcore_event_store", ["stream_id", "version"],
        )

    En PostgreSQL conviene que `payload` sea `postgresql.JSONB` — es lo que emite el
    `JSON_PORTABLE` del mixin — y `event_id` va con `unique=True`.
    """
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import get_engine

    target = engine or get_engine()
    async with target.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[modelo.__table__ for modelo in models],
        )


async def drop_eventstore_tables(
    engine: "AsyncEngine | None" = None,
    *,
    models: t.Sequence[t.Any] = EVENTSTORE_MODELS,
) -> None:
    """
    Borra las tablas del event store.

    **Es destructivo de una forma que casi nada más en este framework lo es**: el event store
    no es una proyección que se pueda reconstruir, es el registro de lo que pasó. Existe para
    tests y para tirar un entorno de desarrollo, y por eso no lo llama ningún lifespan.
    """
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import get_engine

    target = engine or get_engine()
    async with target.begin() as connection:
        await connection.run_sync(
            Base.metadata.drop_all,
            tables=[modelo.__table__ for modelo in reversed(list(models))],
        )
