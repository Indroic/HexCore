"""
`EventStoreFactory`: construye los adaptadores desde la configuración declarativa.

Mismo patrón que `CQRSFactory`: dotted paths resueltos en tiempo de construcción, no de uso,
para que un backend mal escrito falle al arrancar y no en la primera escritura.
"""
from __future__ import annotations

import typing as t

from hexcore.domain.cqrs.resolution import resolve_dotted
from hexcore.domain.eventsourcing.projections import (
    AbstractCheckpointStore,
    AbstractProjection,
)
from hexcore.domain.eventsourcing.snapshots import AbstractSnapshotStore
from hexcore.domain.eventsourcing.store import AbstractEventStore

from .config import EventStoreConfig
from .projector import Projector
from .relay import EventStoreRelay

if t.TYPE_CHECKING:
    from hexcore.domain.cqrs.buses import AbstractEventBus
    from hexcore.domain.cqrs.serializer import AbstractSerializer

__all__ = ["EventStoreFactory"]


def _importar(dotted: str, que: str) -> t.Any:
    """
    Resuelve un dotted path con un error que dice qué se estaba construyendo.

    `resolve_dotted` y no un `rsplit(".", 1)`: maneja qualnames anidados
    (`app.modulo.Externa.Interna`), que es justo donde el split ingenuo falla.
    """
    try:
        return resolve_dotted(dotted)
    except LookupError as error:
        raise ValueError(
            f"No se pudo resolver {que} '{dotted}': {error}"
        ) from error


class EventStoreFactory:
    """
    Construye el almacén, los snapshots, los checkpoints, las proyecciones y el relay.

    Cachea lo que construye: pedirle dos veces el almacén devuelve el mismo objeto. Es lo que
    hace que el `Projector` y el `EventStoreRelay` lean del mismo sitio sin que el llamador
    tenga que pasárselo a mano.
    """

    def __init__(
        self,
        config: EventStoreConfig,
        serializer: "AbstractSerializer | None" = None,
    ) -> None:
        self._config = config
        self._serializer = serializer
        self._store: AbstractEventStore | None = None
        self._snapshots: AbstractSnapshotStore | None = None
        self._checkpoints: AbstractCheckpointStore | None = None

    # ── Serializador ──────────────────────────────────────────────────────────
    def create_serializer(self) -> "AbstractSerializer":
        """
        El serializador, cacheado.

        Prioridad: el que se inyectó al construir la factory (normalmente el de CQRS, para
        que no diverjan), después el dotted path de la config, y si no `PydanticSerializer`.
        """
        if self._serializer is not None:
            return self._serializer

        if self._config.serializer:
            self._serializer = _importar(self._config.serializer, "el serializador")()
        else:
            from hexcore.infrastructure.cqrs.pydantic_serializer import (
                PydanticSerializer,
            )

            self._serializer = PydanticSerializer()

        return self._serializer

    # ── Almacén ───────────────────────────────────────────────────────────────
    def create_store(self, **extra: t.Any) -> AbstractEventStore:
        """
        El event store, cacheado.

        Sin `backend` configurado devuelve `InMemoryEventStore`, que **no persiste nada**.
        Es el default porque no exige ningún extra; no porque sirva en producción.
        """
        if self._store is not None:
            return self._store

        opciones = {**self._config.options, **extra}
        opciones.setdefault("serializer", self.create_serializer())

        if self._config.backend:
            clase = _importar(self._config.backend, "el backend del event store")
        else:
            from hexcore.infrastructure.eventsourcing.memory import InMemoryEventStore

            clase = InMemoryEventStore

        self._store = t.cast(AbstractEventStore, clase(**opciones))
        return self._store

    # ── Snapshots ─────────────────────────────────────────────────────────────
    def create_snapshot_store(self) -> AbstractSnapshotStore | None:
        """El almacén de snapshots, o `None` si no hay ninguno configurado."""
        if self._snapshots is not None:
            return self._snapshots
        if not self._config.snapshots.backend:
            return None

        clase = _importar(
            self._config.snapshots.backend, "el backend de snapshots"
        )
        self._snapshots = t.cast(
            AbstractSnapshotStore, clase(**self._config.snapshots.options)
        )
        return self._snapshots

    # ── Checkpoints ───────────────────────────────────────────────────────────
    def create_checkpoint_store(self) -> AbstractCheckpointStore:
        """
        El almacén de checkpoints, cacheado. Sin backend configurado, el de memoria.

        ⚠️ El de memoria **pierde el avance al reiniciar el proceso**, así que un proyector
        vuelve a procesar el almacén entero en cada arranque. Para desarrollo está bien; en
        producción hay que configurar uno persistente, y suele convenir el del mismo backend
        que el almacén.
        """
        if self._checkpoints is not None:
            return self._checkpoints

        if self._config.projections.checkpoint_backend:
            clase = _importar(
                self._config.projections.checkpoint_backend,
                "el backend de checkpoints",
            )
        else:
            from hexcore.infrastructure.eventsourcing.memory import (
                InMemoryCheckpointStore,
            )

            clase = InMemoryCheckpointStore

        self._checkpoints = t.cast(AbstractCheckpointStore, clase())
        return self._checkpoints

    # ── Proyecciones ──────────────────────────────────────────────────────────
    def create_projections(self) -> list[AbstractProjection]:
        """
        Instancia las proyecciones de la config.

        Sólo admite proyecciones construibles sin argumentos, igual que
        `BusConfig.middlewares`. Una que necesite dependencias se instancia a mano y se le
        pasa la lista al `Projector`.
        """
        instancias: list[AbstractProjection] = []
        for dotted in self._config.projections.projections:
            clase = _importar(dotted, "la proyección")
            try:
                instancias.append(t.cast(AbstractProjection, clase()))
            except TypeError as error:
                raise ValueError(
                    f"La proyección '{dotted}' no se puede construir sin argumentos: "
                    f"{error}. Instanciala a mano y pasale la lista al Projector."
                ) from error
        return instancias

    def create_projector(self, **extra: t.Any) -> Projector:
        """El proyector, con las proyecciones y el checkpoint store de la config."""
        proyecciones = self._config.projections
        return Projector(
            store=self.create_store(),
            checkpoints=self.create_checkpoint_store(),
            projections=self.create_projections(),
            subscription=proyecciones.subscription,
            batch_size=proyecciones.batch_size,
            poll_interval=proyecciones.poll_interval,
            safety_window=proyecciones.safety_window,
            on_error=proyecciones.on_error,
            **extra,
        )

    # ── Relay ─────────────────────────────────────────────────────────────────
    def create_relay(self, bus: "AbstractEventBus", **extra: t.Any) -> EventStoreRelay:
        """
        El relay, con **su propia suscripción**, distinta de la del proyector.

        Comparten almacén y no checkpoint: avanzan a ritmos distintos, y compartir clave haría
        que se pisaran el progreso — el que corriera más rápido dejaría al otro sin ver los
        eventos que se salteó.
        """
        return EventStoreRelay(
            store=self.create_store(),
            bus=bus,
            checkpoints=self.create_checkpoint_store(),
            subscription=self._config.relay_subscription,
            batch_size=self._config.relay_batch_size,
            safety_window=self._config.projections.safety_window,
            **extra,
        )
