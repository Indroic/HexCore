"""
Los documentos del event store en MongoDB. Requiere el extra ``[mongo]``.

**No heredan de `BaseDocument`**, por el mismo par de razones que documenta el backend de
Beanie de Darwin, y acá pesan igual:

1. `is_root = True` en Beanie significa herencia de una sola colección: eventos, snapshots y
   checkpoints terminarían todos mezclados en la misma.
2. `use_cache = True`, con diez minutos de expiración. Un log de eventos leído de una caché
   diría que el stream está en una versión que ya no tiene — y sobre esa versión rancia se
   decide si un `append` entra o se rechaza por concurrencia. Cachear el estado de un
   almacén append-only no es una optimización, es un defecto.

Así que heredan `Document` directo y declaran su propio `Settings`, con `use_cache = False`
**explícito** para que quede escrito y no dependa del default de nadie.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID

from hexcore.capabilities import require_extra

require_extra("beanie", para="el event store sobre MongoDB")

import pymongo  # noqa: E402
from beanie import Document  # noqa: E402
from pydantic import Field  # noqa: E402
from pymongo import IndexModel  # noqa: E402

__all__ = [
    "DEFAULT_EVENT_COLLECTION",
    "DEFAULT_SNAPSHOT_COLLECTION",
    "DEFAULT_CHECKPOINT_COLLECTION",
    "DEFAULT_COUNTER_COLLECTION",
    "StoredEventDocument",
    "SnapshotDocument",
    "ProjectionCheckpointDocument",
    "EVENTSTORE_DOCUMENTS",
    "init_eventstore_documents",
]

DEFAULT_EVENT_COLLECTION = "hexcore_event_store"
DEFAULT_SNAPSHOT_COLLECTION = "hexcore_snapshots"
DEFAULT_CHECKPOINT_COLLECTION = "hexcore_projection_checkpoints"

#: La colección del contador de posiciones globales. No es un `Document` de Beanie: se toca
#: con `find_one_and_update` crudo, porque lo que hace falta es un `$inc` atómico y no un
#: modelo.
DEFAULT_COUNTER_COLLECTION = "hexcore_eventstore_counters"


def _ahora() -> datetime:
    return datetime.now(UTC)


class StoredEventDocument(Document):
    """Un evento persistido."""

    event_id: UUID
    stream_id: str
    stream_type: str
    version: int
    global_position: int
    event_type: str
    payload: dict[str, t.Any] = Field(default_factory=dict)
    occurred_on: datetime
    recorded_at: datetime = Field(default_factory=_ahora)

    class Settings:
        name = DEFAULT_EVENT_COLLECTION
        use_cache = False
        indexes = [
            # **La garantía real de la concurrencia optimista.** El `count` previo que hace
            # el adaptador sirve para dar un error claro, pero es un TOCTOU; esto es lo que
            # de verdad impide dos eventos con la misma versión, y su `DuplicateKeyError` es
            # lo que se traduce a `ConcurrencyError`.
            IndexModel(
                [("stream_id", pymongo.ASCENDING), ("version", pymongo.ASCENDING)],
                unique=True,
                name="uq_stream_version",
            ),
            IndexModel(
                [("global_position", pymongo.ASCENDING)],
                unique=True,
                name="uq_global_position",
            ),
            IndexModel(
                [("stream_type", pymongo.ASCENDING), ("global_position", pymongo.ASCENDING)],
                name="ix_type_position",
            ),
            # Idempotencia del relay y de las proyecciones al reprocesar una ventana.
            IndexModel([("event_id", pymongo.ASCENDING)], unique=True, name="uq_event_id"),
        ]


class SnapshotDocument(Document):
    """El estado de un agregado en una versión. Se guarda historial, no se pisa."""

    stream_id: str
    aggregate_type: str
    version: int
    state: dict[str, t.Any] = Field(default_factory=dict)
    taken_at: datetime = Field(default_factory=_ahora)

    class Settings:
        name = DEFAULT_SNAPSHOT_COLLECTION
        use_cache = False
        indexes = [
            IndexModel(
                [("stream_id", pymongo.ASCENDING), ("version", pymongo.ASCENDING)],
                unique=True,
                name="uq_stream_version",
            ),
        ]


class ProjectionCheckpointDocument(Document):
    """Hasta dónde leyó cada suscripción."""

    subscription: str
    position: int = 0
    updated_at: datetime = Field(default_factory=_ahora)

    class Settings:
        name = DEFAULT_CHECKPOINT_COLLECTION
        use_cache = False
        indexes = [
            IndexModel(
                [("subscription", pymongo.ASCENDING)],
                unique=True,
                name="uq_subscription",
            ),
        ]


EVENTSTORE_DOCUMENTS: tuple[type[Document], ...] = (
    StoredEventDocument,
    SnapshotDocument,
    ProjectionCheckpointDocument,
)


async def init_eventstore_documents(
    database: t.Any = None,
    *,
    documents: t.Sequence[type[Document]] = EVENTSTORE_DOCUMENTS,
    client: t.Any = None,
) -> None:
    """
    Registra los documentos en Beanie y crea sus índices.

    Sin esto los documentos existen como clases y **no** se pueden usar: Beanie necesita el
    `init_beanie` para ligarlos a una colección. Y sin los índices no hay unicidad de
    `(stream_id, version)`, o sea que la concurrencia optimista no existiría — el adaptador
    haría el `count`, dos escritores pasarían el chequeo y los dos escribirían la misma
    versión.

    Args:
        database: La base. Si no viene, se abre un cliente con `ServerConfig.mongo_uri` y se
            usa su base por defecto, igual que `init_beanie_documents()` del framework.
        documents: Los documentos a registrar. Por defecto, los tres del event store.
        client: Un `AsyncMongoClient` propio, para reusar un pool.

    ⚠️ **`init_beanie` no es acumulativo**: llamarlo dos veces sobre la misma base reemplaza
    el registro de la primera. Si tu aplicacion ya inicializa sus propios documentos, sumale
    `EVENTSTORE_DOCUMENTS` a esa llamada en vez de invocar esta aparte -- o el event store
    quedara sin registrar y sus operaciones fallaran al usarse.

    Calcado de `init_identity_documents` de Darwin.
    """
    # `pyright: ignore` narrow y con motivo, que es la politica de la casa: ni `beanie` ni
    # `pymongo` shippean stubs completos, asi que sus simbolos llegan parcialmente
    # desconocidos.
    from beanie import init_beanie  # pyright: ignore[reportUnknownVariableType]

    if database is None:
        from pymongo import AsyncMongoClient

        from hexcore.config import LazyConfig

        motor = client or AsyncMongoClient(  # pyright: ignore[reportUnknownVariableType]
            LazyConfig.get_config().mongo_uri
        )
        # `database` ya esta declarado `t.Any` en la firma, pero pyright lo re-estrecha con
        # lo que devuelve `motor`, que es desconocido. La reasignacion explicita lo contiene.
        database = t.cast(t.Any, motor.get_default_database())

    await init_beanie(database=database, document_models=list(documents))
