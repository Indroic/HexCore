"""
`EventSourcedRepository`: cargar y guardar agregados contra el event store.

Es la pieza que hace que el resto del código de aplicación no tenga que saber que hay un
event store abajo. Un caso de uso hace `pedido = await repo.get(id)`, llama a un método de
negocio, y hace `await repo.save(pedido)`; lo que pasa en el medio —el replay, el snapshot,
la versión esperada— es asunto de acá.
"""
from __future__ import annotations

import logging
import typing as t
from datetime import UTC, datetime
from uuid import UUID

from hexcore.domain.cqrs.resolution import build_fqn
from hexcore.domain.eventsourcing.aggregate import AggregateRoot
from hexcore.domain.eventsourcing.exceptions import AggregateNotFoundError
from hexcore.domain.eventsourcing.snapshots import AbstractSnapshotStore, Snapshot
from hexcore.domain.eventsourcing.store import AbstractEventStore
from hexcore.domain.eventsourcing.stored import StoredEvent

__all__ = ["EventSourcedRepository"]

logger = logging.getLogger("hexcore.eventsourcing.repository")

TAggregate = t.TypeVar("TAggregate", bound=AggregateRoot)


class EventSourcedRepository(t.Generic[TAggregate]):
    """
    Repositorio de agregados event-sourced.

    **No hereda de `IBaseRepository` ni de `BaseSQLAlchemyRepository`**, y las dos cosas son
    deliberadas.

    Lo segundo es la trampa: `discover_sql_repositories()` recoge todo subtipo de
    `BaseSQLAlchemyRepository` y `_inject_repositories()` lo pega con `setattr` en **todos**
    los Unit of Work del consumidor. Un repositorio de framework que herede de ahí aparece,
    sin que nadie lo pida, como un atributo de cada UoW — y encima puede chocar por nombre
    con uno del consumidor. Es el mismo motivo que documenta `SqlAlchemyCronJobRepository`.

    Lo primero es más simple: `IBaseRepository[T]` está atado a `BaseEntity` y su superficie
    es CRUD. `list_all()` sobre un event store sería recorrer todos los streams y
    reconstruir cada agregado, y `delete()` sería reescribir el pasado. Ninguna de las dos
    operaciones significa lo mismo acá, así que heredarlas para no implementarlas sería
    prometer una compatibilidad que no existe.
    """

    def __init__(
        self,
        aggregate_type: type[TAggregate],
        *,
        store: AbstractEventStore,
        snapshots: AbstractSnapshotStore | None = None,
        snapshot_every: int = 0,
    ) -> None:
        """
        Args:
            aggregate_type: La clase del agregado. De ella salen `__stream_type__` y el
                `load_from_history` que reconstruye.
            store: El almacén de eventos.
            snapshots: Opcional. Sin él, cada `get()` relee el stream entero — que para un
                agregado de vida corta es lo correcto y más simple.
            snapshot_every: Cada cuántas versiones tomar un snapshot. 0 lo desactiva.
        """
        if snapshot_every < 0:
            raise ValueError("snapshot_every no puede ser negativo.")
        if snapshot_every and snapshots is None:
            raise ValueError(
                "snapshot_every está configurado pero no hay snapshot store: los snapshots "
                "no se tomarían y el agregado releería el stream entero en cada get(), que "
                "es justo lo que se quiso evitar. Pasá snapshots= o dejá snapshot_every=0."
            )

        self._aggregate_type = aggregate_type
        self._store = store
        self._snapshots = snapshots
        self._snapshot_every = snapshot_every

    # ── Lectura ───────────────────────────────────────────────────────────────
    async def get(self, aggregate_id: UUID | str) -> TAggregate:
        """
        Reconstruye el agregado desde su historial.

        Con snapshot store configurado: carga el snapshot más reciente y lee sólo los eventos
        posteriores. Sin él, lee desde el evento 1.

        Raises:
            AggregateNotFoundError: Si el stream no tiene eventos y no hay snapshot.
        """
        agregado = await self.find(aggregate_id)
        if agregado is None:
            raise AggregateNotFoundError(
                self._aggregate_type.__name__,
                self._aggregate_type.stream_id_for(aggregate_id),
            )
        return agregado

    async def find(self, aggregate_id: UUID | str) -> TAggregate | None:
        """Como `get()`, pero devuelve `None` en vez de lanzar."""
        stream_id = self._aggregate_type.stream_id_for(aggregate_id)

        snapshot = await self._cargar_snapshot(stream_id)
        desde = snapshot.version + 1 if snapshot is not None else 1
        eventos_persistidos = await self._store.read_stream(stream_id, from_version=desde)

        if snapshot is None and not eventos_persistidos:
            return None

        eventos = [self._store.rehydrate(persistido) for persistido in eventos_persistidos]
        identificador = (
            aggregate_id if isinstance(aggregate_id, UUID) else UUID(str(aggregate_id))
        )
        return self._aggregate_type.load_from_history(
            eventos, snapshot=snapshot, aggregate_id=identificador
        )

    async def exists(self, aggregate_id: UUID | str) -> bool:
        """Si el agregado tiene historial. No lo reconstruye."""
        return await self._store.stream_exists(
            self._aggregate_type.stream_id_for(aggregate_id)
        )

    # ── Escritura ─────────────────────────────────────────────────────────────
    async def save(self, aggregate: TAggregate) -> list[StoredEvent]:
        """
        Persiste los eventos pendientes del agregado.

        La versión esperada es `aggregate.version`, o sea la **confirmada**: la que tenía el
        stream cuando se lo cargó. Si otro escritor lo avanzó en el medio, el almacén rechaza
        el append.

        `mark_events_as_committed()` se llama **sólo si el append salió bien**. Ese orden es
        el que hace reintentable un conflicto: ante un `ConcurrencyError`, el agregado
        conserva sus eventos pendientes y su versión, así que el caso de uso puede recargar y
        reaplicar la operación de negocio.

        Returns:
            Los eventos persistidos. Vacío si no había nada pendiente.

        Raises:
            ConcurrencyError: Si otro escritor avanzó el stream.
        """
        pendientes = aggregate.uncommitted_events
        if not pendientes:
            return []

        escritos = await self._store.append(
            aggregate.stream_id,
            pendientes,
            expected_version=aggregate.version,
            stream_type=type(aggregate).__stream_type__,
        )
        aggregate.mark_events_as_committed()

        await self._quizas_snapshotear(aggregate)
        return escritos

    # ── Snapshots ─────────────────────────────────────────────────────────────
    async def _cargar_snapshot(self, stream_id: str) -> Snapshot | None:
        if self._snapshots is None:
            return None

        snapshot = await self._snapshots.load(stream_id)
        if snapshot is None:
            return None

        esperado = build_fqn(self._aggregate_type)
        if snapshot.aggregate_type != esperado:
            # El `stream_id` se reusó para otro tipo de agregado. Restaurar igual daría un
            # objeto que pydantic acepta y que el negocio no entiende, así que se ignora el
            # snapshot y se relee el stream: más lento, pero correcto.
            logger.warning(
                "El snapshot de '%s' es de '%s' y se esperaba '%s': se ignora y se relee el "
                "stream completo.",
                stream_id,
                snapshot.aggregate_type,
                esperado,
            )
            return None

        return snapshot

    async def _quizas_snapshotear(self, aggregate: TAggregate) -> None:
        if self._snapshots is None or self._snapshot_every <= 0:
            return
        if aggregate.version % self._snapshot_every != 0:
            return

        try:
            await self._snapshots.save(
                Snapshot(
                    stream_id=aggregate.stream_id,
                    aggregate_type=build_fqn(self._aggregate_type),
                    version=aggregate.version,
                    state=aggregate.snapshot_state(),
                    taken_at=datetime.now(UTC),
                )
            )
        except Exception:
            # Se loguea y no se propaga. Un snapshot es una optimización de lectura: los
            # eventos ya están escritos y el agregado se puede reconstruir sin él. Dejar que
            # el fallo suba convertiría un `save()` exitoso en uno fallido, y el llamador
            # reintentaría una operación de negocio que **ya ocurrió**.
            logger.exception(
                "No se pudo guardar el snapshot de '%s' en la version %d. Los eventos si se "
                "escribieron; el agregado se va a reconstruir desde el historial.",
                aggregate.stream_id,
                aggregate.version,
            )
