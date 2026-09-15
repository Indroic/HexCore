"""
Columnas de las tablas del event store, sin `__tablename__` y sin `Base`.

**Importar este módulo no registra ninguna tabla.** Los mixins no heredan de `Base` ni
declaran `__tablename__`, así que se pueden importar para componer un modelo propio con otro
nombre de tabla sin que el default entre en `Base.metadata`. Es el patrón de
`hexcore/darwin/infrastructure/orms/sqlalchemy/models_mixins.py`, y las tablas concretas
están en `sqlalchemy_models.py`.

Requiere el extra ``[sql]``.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID as PythonUUID

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    Uuid,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

__all__ = [
    "BIGINT_PORTABLE",
    "JSON_PORTABLE",
    "EventStoreMixin",
    "SnapshotMixin",
    "ProjectionCheckpointMixin",
]

#: `JSONB` en PostgreSQL, `JSON` en el resto. `JSONB` a secas rompe en SQLite, y la suite
#: corre sobre SQLite. Mismo truco que usa Darwin.
JSON_PORTABLE = JSON().with_variant(JSONB(), "postgresql")

#: `BIGINT` en todos lados menos SQLite, donde es `INTEGER`.
#:
#: No es cosmético: **SQLite sólo autoincrementa una `INTEGER PRIMARY KEY`**. Una columna
#: declarada `BIGINT PRIMARY KEY` no recibe rowid implícito, así que el `INSERT` sin valor
#: falla con NOT NULL. Y la posición global tiene que ser `BIGINT` en producción, porque un
#: `INTEGER` de 32 bits se agota en 2.100 millones de eventos — que en un event store no es
#: una cifra teórica.
BIGINT_PORTABLE = BigInteger().with_variant(Integer(), "sqlite")


def _ahora() -> datetime:
    return datetime.now(UTC)


class EventStoreMixin:
    """
    La tabla de eventos: append-only, con orden por stream y orden global.

    **No hereda de `BaseModel[T]`**, y eso es una regla, no una preferencia.
    `SqlAlchemyUnitOfWork.collect_domain_entities()` recorre `session.new | dirty | deleted`,
    filtra por `isinstance(model, BaseModel)` y le pide `get_domain_entity()` a cada fila. Una
    fila de eventos no tiene entidad de dominio detrás, así que heredar de ahí produciría un
    `AttributeError` — y con el event store enganchado al UoW, esas filas están en
    `session.new` **durante el propio `commit()`**, con la transacción a medio camino. Es el
    mismo motivo que documenta `CronJobModel`.

    Ninguna columna se llama `metadata`: ese nombre pisa `Base.metadata` y rompe el registro
    declarativo entero. Darwin usa `audit_metadata` por lo mismo; acá el sobre viaja dentro
    de `payload`, así que no hace falta una columna aparte.
    """

    __tablename__: t.ClassVar[str]

    #: La posición en el orden global, y la clave primaria.
    #:
    #: Es la PK en vez de un `id` propio porque un event store se lee casi siempre en orden
    #: global —el relay y las proyecciones no hacen otra cosa—, y tenerla como PK hace que
    #: ese recorrido use el índice agrupado en lugar de uno secundario.
    @declared_attr
    def global_position(cls) -> Mapped[int]:  # noqa: N805
        return mapped_column(BIGINT_PORTABLE, primary_key=True, autoincrement=True)

    #: El `event_id` del `DomainEvent`. **Único**: es la clave de idempotencia con la que el
    #: relay y las proyecciones deduplican al reprocesar una ventana, y también lo que
    #: convierte un reintento de escritura tras un timeout en un error en vez de en un
    #: duplicado silencioso.
    event_id: Mapped[PythonUUID] = mapped_column(Uuid, nullable=False, unique=True)

    stream_id: Mapped[str] = mapped_column(String(255), nullable=False)
    stream_type: Mapped[str] = mapped_column(String(128), nullable=False)

    #: La posición dentro del stream, 1-based. Ver el `UniqueConstraint` de `__table_args__`.
    version: Mapped[int] = mapped_column(Integer, nullable=False)

    #: El FQN del tipo. Duplica `payload["__type__"]` para poder indexarlo: filtrar por tipo
    #: dentro de un JSON no usa índice en ningún motor sin un índice de expresión aparte.
    event_type: Mapped[str] = mapped_column(String(512), nullable=False)

    occurred_on: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_ahora
    )

    @declared_attr
    def payload(cls) -> Mapped[dict[str, t.Any]]:  # noqa: N805
        # `declared_attr` porque un default mutable tiene que construirse por clase.
        return mapped_column(JSON_PORTABLE, nullable=False, default=dict)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            # **La garantía real de la concurrencia optimista.** El `SELECT MAX(version)`
            # que hace el store antes de escribir sirve para dar un error claro, pero es un
            # TOCTOU: entre la lectura y el INSERT cabe otra transacción. Lo que de verdad
            # impide dos eventos con la misma versión es este constraint, y por eso el
            # adaptador traduce su `IntegrityError` a `ConcurrencyError`.
            UniqueConstraint("stream_id", "version", name=f"uq_{nombre}_stream_version"),
            # Reconstruir un agregado: leer un stream desde una versión.
            Index(f"ix_{nombre}_stream", "stream_id", "version"),
            # `read_all(stream_types=...)`: recorrer el orden global filtrando por categoría.
            Index(f"ix_{nombre}_type_position", "stream_type", "global_position"),
            Index(f"ix_{nombre}_event_type", "event_type"),
        )


class SnapshotMixin:
    """
    Snapshots del estado de un agregado.

    Se guarda **historial**: la clave es `(stream_id, version)`, no `stream_id` solo. Un
    snapshot corrupto —por un bug en `snapshot_state()` o por un campo que cambió de forma—
    se recupera borrándolo y volviendo al anterior. Pisando, ese anterior no existiría.
    """

    __tablename__: t.ClassVar[str]

    @declared_attr
    def id(cls) -> Mapped[int]:  # noqa: N805
        return mapped_column(BIGINT_PORTABLE, primary_key=True, autoincrement=True)

    stream_id: Mapped[str] = mapped_column(String(255), nullable=False)

    #: El FQN del agregado. Detecta el `stream_id` reusado para otro tipo: restaurar un
    #: `Pedido` desde el snapshot de un `Usuario` daría un objeto que pydantic acepta y que el
    #: negocio no entiende.
    aggregate_type: Mapped[str] = mapped_column(String(512), nullable=False)

    version: Mapped[int] = mapped_column(Integer, nullable=False)
    taken_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_ahora
    )

    @declared_attr
    def state(cls) -> Mapped[dict[str, t.Any]]:  # noqa: N805
        return mapped_column(JSON_PORTABLE, nullable=False, default=dict)

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            # Guardar dos veces la misma versión es idempotente: el adaptador actualiza en
            # vez de acumular filas que no aportan ningún punto de retroceso nuevo.
            UniqueConstraint("stream_id", "version", name=f"uq_{nombre}_stream_version"),
            Index(f"ix_{nombre}_stream", "stream_id", "version"),
        )


class ProjectionCheckpointMixin:
    """
    Hasta dónde leyó cada suscripción.

    Una fila por suscripción, con el nombre como clave primaria: no hay historial que
    guardar, y el valor se pisa en cada avance.
    """

    __tablename__: t.ClassVar[str]

    subscription: Mapped[str] = mapped_column(String(255), primary_key=True)
    position: Mapped[int] = mapped_column(BIGINT_PORTABLE, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_ahora, onupdate=_ahora
    )
