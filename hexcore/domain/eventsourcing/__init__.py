"""
hexcore.domain.eventsourcing — los puertos del Event Sourcing.

Dominio puro: pydantic y stdlib, cero infraestructura. Los reexports son eager, sin fachada
perezosa, precisamente porque nada de aca depende de un extra — importar este paquete no
puede fallar por una dependencia que falte.

La superficie publica pensada para el consumidor esta en `hexcore.eventsourcing`, que sí es
perezosa porque ahi conviven los adaptadores.
"""
from __future__ import annotations

from .exceptions import (
    AggregateNotFoundError,
    ConcurrencyError,
    EventSourcingError,
    UnhandledEventError,
)
from .projections import AbstractCheckpointStore, AbstractProjection
from .snapshots import AbstractSnapshotStore, Snapshot
from .store import AbstractEventStore
from .stored import EXPECTED_VERSION_ANY, EXPECTED_VERSION_NO_STREAM, StoredEvent

__all__ = [
    # El evento persistido
    "StoredEvent",
    "EXPECTED_VERSION_ANY",
    "EXPECTED_VERSION_NO_STREAM",
    # Puertos
    "AbstractEventStore",
    "AbstractSnapshotStore",
    "Snapshot",
    "AbstractProjection",
    "AbstractCheckpointStore",
    # Excepciones
    "EventSourcingError",
    "ConcurrencyError",
    "AggregateNotFoundError",
    "UnhandledEventError",
]
