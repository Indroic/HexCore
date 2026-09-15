"""
Adaptadores del event store sobre MongoDB. Requiere el extra ``[mongo]``.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime

from hexcore.capabilities import require_extra

require_extra("beanie", para="el event store sobre MongoDB")

from pymongo import ASCENDING, ReturnDocument  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402

from hexcore.domain.cqrs.resolution import build_fqn  # noqa: E402
from hexcore.domain.events import DomainEvent  # noqa: E402
from hexcore.domain.eventsourcing.exceptions import ConcurrencyError  # noqa: E402
from hexcore.domain.eventsourcing.projections import (  # noqa: E402
    AbstractCheckpointStore,
)
from hexcore.domain.eventsourcing.snapshots import (  # noqa: E402
    AbstractSnapshotStore,
    Snapshot,
)
from hexcore.domain.eventsourcing.store import AbstractEventStore  # noqa: E402
from hexcore.domain.eventsourcing.stored import (  # noqa: E402
    EXPECTED_VERSION_ANY,
    StoredEvent,
)
from hexcore.infrastructure.eventsourcing.memory import stream_type_de  # noqa: E402

from .beanie_documents import (  # noqa: E402
    DEFAULT_COUNTER_COLLECTION,
    ProjectionCheckpointDocument,
    SnapshotDocument,
    StoredEventDocument,
)

if t.TYPE_CHECKING:
    from hexcore.domain.cqrs.serializer import AbstractSerializer

__all__ = [
    "BeanieEventStore",
    "BeanieSnapshotStore",
    "BeanieCheckpointStore",
]

#: El `_id` del documento contador dentro de `DEFAULT_COUNTER_COLLECTION`.
_CLAVE_DEL_CONTADOR = "event_store"


def _ahora() -> datetime:
    return datetime.now(UTC)


class BeanieEventStore(AbstractEventStore):
    """
    Event store sobre una colección de MongoDB.

    ## Lo que este adaptador no puede garantizar

    Hay que leerlo antes de elegirlo, porque las dos limitaciones son de MongoDB y no se
    arreglan del lado del código:

    1. **Sin replica set no hay transacción multi-documento.** Consecuencias concretas: (a)
       el `append` de N eventos **no es atómico** — un fallo a mitad deja el stream truncado,
       con versiones válidas pero incompletas; el `UNIQUE(stream_id, version)` evita
       duplicados, no huecos. Y (b) el `append` **no puede compartir transacción con el
       cambio de negocio**, así que el modo híbrido del Unit of Work (escribir el evento y el
       cambio juntos) pierde su garantía principal. **En Mongo el camino sano es Event
       Sourcing puro**: que el `append` *sea* el cambio, vía `EventSourcedRepository`.

       Con replica set configurado, pasale la sesión de la transacción de Mongo a cada
       operación con `session=`.

    2. **El contador de posiciones no es transaccional respecto del insert.** Se reserva el
       bloque con un `$inc` y después se escribe; si la escritura falla, esas posiciones
       quedan quemadas. O sea que `global_position` es monotónica **con huecos**, incluso sin
       concurrencia. No es un problema nuevo: el `Projector` y el `EventStoreRelay` ya lo
       compensan con `safety_window` y deduplicación por `event_id`, porque el backend de SQL
       tiene el mismo hueco por otra razón.

    Los índices los crea `init_eventstore_documents()`. **Sin esa llamada no hay unicidad de
    `(stream_id, version)`**, y entonces la concurrencia optimista no existe: dos escritores
    pasarían el chequeo de versión y los dos escribirían.
    """

    def __init__(
        self,
        *,
        serializer: "AbstractSerializer | None" = None,
        document: type[t.Any] = StoredEventDocument,
        counter_collection: str = DEFAULT_COUNTER_COLLECTION,
    ) -> None:
        super().__init__(serializer)
        self._document = document
        self._counter_collection = counter_collection

    async def append(
        self,
        stream_id: str,
        events: t.Sequence[DomainEvent],
        *,
        expected_version: int,
        stream_type: str | None = None,
        metadata: t.Mapping[str, t.Any] | None = None,
    ) -> list[StoredEvent]:
        if not events:
            return []

        categoria = stream_type or stream_type_de(stream_id)

        version_actual = await self.stream_version(stream_id)
        if expected_version != EXPECTED_VERSION_ANY and version_actual != expected_version:
            raise ConcurrencyError(stream_id, expected_version, version_actual)

        # Se reserva el bloque completo en **un solo** `$inc`, no uno por evento: es un
        # round-trip por `append` en vez de uno por evento, y hace que las posiciones de un
        # mismo append queden contiguas.
        ultima = await self._reservar_posiciones(len(events))
        primera = ultima - len(events) + 1

        momento = _ahora()
        documentos = [
            self._document(
                event_id=evento.event_id,
                stream_id=stream_id,
                stream_type=categoria,
                version=version_actual + desplazamiento,
                global_position=primera + desplazamiento - 1,
                event_type=build_fqn(type(evento)),
                payload=self._serializer.serialize_envelope(evento, metadata),
                occurred_on=evento.occurred_on,
                recorded_at=momento,
            )
            for desplazamiento, evento in enumerate(events, start=1)
        ]

        try:
            await self._document.insert_many(documentos)
        except DuplicateKeyError as error:
            # El chequeo de versión de más arriba es un TOCTOU. Quien de verdad impide dos
            # eventos con la misma versión es el índice único, y esto traduce su error.
            actual = await self.stream_version(stream_id)
            raise ConcurrencyError(stream_id, expected_version, actual) from error

        return [self._a_stored(documento) for documento in documentos]

    async def read_stream(
        self,
        stream_id: str,
        *,
        from_version: int = 1,
        to_version: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        criterio: dict[str, t.Any] = {
            "stream_id": stream_id,
            "version": {"$gte": from_version},
        }
        if to_version is not None:
            criterio["version"]["$lte"] = to_version

        consulta = self._document.find(criterio).sort([("version", ASCENDING)])
        if limit is not None:
            consulta = consulta.limit(limit)

        return [self._a_stored(documento) for documento in await consulta.to_list()]

    async def read_all(
        self,
        *,
        from_position: int = 0,
        limit: int = 500,
        stream_types: t.Sequence[str] | None = None,
    ) -> list[StoredEvent]:
        criterio: dict[str, t.Any] = {"global_position": {"$gt": from_position}}
        if stream_types:
            criterio["stream_type"] = {"$in": list(stream_types)}

        documentos = (
            await self._document.find(criterio)
            .sort([("global_position", ASCENDING)])
            .limit(limit)
            .to_list()
        )
        return [self._a_stored(documento) for documento in documentos]

    async def stream_version(self, stream_id: str) -> int:
        ultimo = (
            await self._document.find({"stream_id": stream_id})
            .sort([("version", -1)])
            .limit(1)
            .to_list()
        )
        return int(ultimo[0].version) if ultimo else 0

    # ── Interno ───────────────────────────────────────────────────────────────
    async def _reservar_posiciones(self, cuantas: int) -> int:
        """
        Reserva `cuantas` posiciones globales y devuelve la última.

        `find_one_and_update` con `$inc` es atómico en MongoDB, así que dos escritores
        concurrentes nunca reciben el mismo bloque. `upsert=True` crea el contador la primera
        vez, y `ReturnDocument.AFTER` devuelve el valor **ya incrementado** — con `BEFORE` se
        leería el anterior y dos appends simultáneos se pisarían las posiciones.
        """
        coleccion = self._document.get_motor_collection().database[
            self._counter_collection
        ]
        documento = await coleccion.find_one_and_update(
            {"_id": _CLAVE_DEL_CONTADOR},
            {"$inc": {"seq": cuantas}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        return int(documento["seq"])

    @staticmethod
    def _a_stored(documento: t.Any) -> StoredEvent:
        return StoredEvent(
            stream_id=documento.stream_id,
            stream_type=documento.stream_type,
            version=documento.version,
            global_position=documento.global_position,
            event_id=documento.event_id,
            event_type=documento.event_type,
            payload=dict(documento.payload or {}),
            occurred_on=documento.occurred_on,
            recorded_at=documento.recorded_at,
        )


class BeanieSnapshotStore(AbstractSnapshotStore):
    """Snapshots sobre MongoDB, con historial por `(stream_id, version)`."""

    def __init__(self, *, document: type[t.Any] = SnapshotDocument) -> None:
        self._document = document

    async def load(
        self, stream_id: str, *, max_version: int | None = None
    ) -> Snapshot | None:
        criterio: dict[str, t.Any] = {"stream_id": stream_id}
        if max_version is not None:
            criterio["version"] = {"$lte": max_version}

        encontrados = (
            await self._document.find(criterio).sort([("version", -1)]).limit(1).to_list()
        )
        if not encontrados:
            return None

        documento = encontrados[0]
        return Snapshot(
            stream_id=documento.stream_id,
            aggregate_type=documento.aggregate_type,
            version=documento.version,
            state=dict(documento.state or {}),
            taken_at=documento.taken_at,
        )

    async def save(self, snapshot: Snapshot) -> None:
        existente = await self._document.find_one(
            {"stream_id": snapshot.stream_id, "version": snapshot.version}
        )
        if existente is not None:
            # Idempotente: acumular filas con la misma versión haría crecer el historial sin
            # aportar ningún punto de retroceso nuevo.
            existente.aggregate_type = snapshot.aggregate_type
            existente.state = dict(snapshot.state)
            existente.taken_at = snapshot.taken_at
            await existente.save()
            return

        await self._document(
            stream_id=snapshot.stream_id,
            aggregate_type=snapshot.aggregate_type,
            version=snapshot.version,
            state=dict(snapshot.state),
            taken_at=snapshot.taken_at,
        ).insert()

    async def delete(self, stream_id: str) -> None:
        await self._document.find({"stream_id": stream_id}).delete()


class BeanieCheckpointStore(AbstractCheckpointStore):
    """Checkpoints sobre MongoDB, un documento por suscripción."""

    def __init__(self, *, document: type[t.Any] = ProjectionCheckpointDocument) -> None:
        self._document = document

    async def load(self, subscription: str) -> int:
        documento = await self._document.find_one({"subscription": subscription})
        return int(documento.position) if documento is not None else 0

    async def save(self, subscription: str, position: int) -> None:
        documento = await self._document.find_one({"subscription": subscription})
        if documento is None:
            await self._document(
                subscription=subscription, position=position
            ).insert()
            return

        documento.position = position
        documento.updated_at = _ahora()
        await documento.save()

    async def reset(self, subscription: str) -> None:
        await self._document.find({"subscription": subscription}).delete()
