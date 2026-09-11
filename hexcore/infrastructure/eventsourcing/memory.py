"""
Adaptadores en memoria del event store, los snapshots y los checkpoints.

**Son adaptadores de verdad, no dobles de prueba.** Viven en `infrastructure/`, se pueden
elegir por configuración y respetan el contrato completo — incluida la concurrencia
optimista, que es justo lo que un fake tiende a no implementar. Esa es la diferencia que
importa: un contrato probado contra un doble permisivo no está probado.

Lo que no dan es durabilidad. Sirven para tests, para desarrollo y para un proceso único que
no necesita sobrevivir a su propio reinicio.
"""
from __future__ import annotations

import asyncio
import typing as t
from datetime import UTC, datetime

from hexcore.domain.cqrs.resolution import build_fqn
from hexcore.domain.events import DomainEvent
from hexcore.domain.eventsourcing.exceptions import ConcurrencyError
from hexcore.domain.eventsourcing.projections import AbstractCheckpointStore
from hexcore.domain.eventsourcing.snapshots import AbstractSnapshotStore, Snapshot
from hexcore.domain.eventsourcing.store import AbstractEventStore
from hexcore.domain.eventsourcing.stored import (
    EXPECTED_VERSION_ANY,
    StoredEvent,
)

if t.TYPE_CHECKING:
    from hexcore.domain.cqrs.serializer import AbstractSerializer

__all__ = [
    "InMemoryEventStore",
    "InMemorySnapshotStore",
    "InMemoryCheckpointStore",
    "stream_type_de",
]


def stream_type_de(stream_id: str) -> str:
    """
    La categoría que se deduce de un `stream_id`.

    El formato canónico es `f"{stream_type}-{aggregate_id}"`, y como el id suele ser un UUID
    —que lleva guiones— se corta en el **primero**, no en el último. Un `stream_id` que no
    siga el formato se devuelve entero: es un nombre de categoría tan válido como cualquiera,
    y adivinar de más sería peor que no adivinar.
    """
    return stream_id.split("-", 1)[0]


def _ahora() -> datetime:
    return datetime.now(UTC)


class InMemoryEventStore(AbstractEventStore):
    """
    Event store en memoria, con orden global y concurrencia optimista reales.

    El `asyncio.Lock` no es decorativo. `append` comprueba la versión y después escribe, y en
    el medio hay `await`s: sin el lock, dos corrutinas que apuntan a la misma versión pasan
    las dos la comprobación antes de que cualquiera escriba, y el `expected_version` no
    protege de nada. Es exactamente la carrera que el test de contrato provoca con
    `asyncio.gather`, y sin lock la detecta.
    """

    def __init__(self, serializer: "AbstractSerializer | None" = None) -> None:
        super().__init__(serializer)
        self._streams: dict[str, list[StoredEvent]] = {}
        self._todos: list[StoredEvent] = []
        self._lock = asyncio.Lock()

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
            # No-op, y **sin** comprobar la versión: guardar un agregado que no cambió no
            # tiene por qué fallar por concurrencia.
            return []

        categoria = stream_type or stream_type_de(stream_id)

        async with self._lock:
            existentes = self._streams.setdefault(stream_id, [])
            version_actual = len(existentes)

            if expected_version != EXPECTED_VERSION_ANY and version_actual != expected_version:
                raise ConcurrencyError(stream_id, expected_version, version_actual)

            momento = _ahora()
            nuevos: list[StoredEvent] = []
            for desplazamiento, evento in enumerate(events, start=1):
                nuevos.append(
                    StoredEvent(
                        stream_id=stream_id,
                        stream_type=categoria,
                        version=version_actual + desplazamiento,
                        global_position=len(self._todos) + desplazamiento,
                        event_id=evento.event_id,
                        event_type=build_fqn(type(evento)),
                        payload=self._serializer.serialize_envelope(evento, metadata),
                        occurred_on=evento.occurred_on,
                        recorded_at=momento,
                    )
                )

            existentes.extend(nuevos)
            self._todos.extend(nuevos)
            return nuevos

    async def read_stream(
        self,
        stream_id: str,
        *,
        from_version: int = 1,
        to_version: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        seleccion = [
            persistido
            for persistido in self._streams.get(stream_id, ())
            if persistido.version >= from_version
            and (to_version is None or persistido.version <= to_version)
        ]
        return seleccion[:limit] if limit is not None else seleccion

    async def read_all(
        self,
        *,
        from_position: int = 0,
        limit: int = 500,
        stream_types: t.Sequence[str] | None = None,
    ) -> list[StoredEvent]:
        categorias = set(stream_types) if stream_types else None
        seleccion: list[StoredEvent] = []
        for persistido in self._todos:
            if persistido.global_position <= from_position:
                continue
            if categorias is not None and persistido.stream_type not in categorias:
                continue
            seleccion.append(persistido)
            if len(seleccion) >= limit:
                break
        return seleccion

    async def stream_version(self, stream_id: str) -> int:
        return len(self._streams.get(stream_id, ()))

    # ── Utilidades de test ────────────────────────────────────────────────────
    def clear(self) -> None:
        """Vacía el almacén. Para reusar la instancia entre casos de un test."""
        self._streams.clear()
        self._todos.clear()

    def __len__(self) -> int:
        return len(self._todos)


class InMemorySnapshotStore(AbstractSnapshotStore):
    """
    Snapshots en memoria, **con historial**: `save` agrega, no pisa.

    Es lo que dice el puerto, y conviene que el adaptador de referencia lo cumpla: si el más
    simple pisara, un test escrito contra él pasaría y el mismo código fallaría contra el de
    SQL al pedir un `max_version` anterior.
    """

    def __init__(self) -> None:
        self._por_stream: dict[str, list[Snapshot]] = {}

    async def load(
        self, stream_id: str, *, max_version: int | None = None
    ) -> Snapshot | None:
        candidatos = [
            snapshot
            for snapshot in self._por_stream.get(stream_id, ())
            if max_version is None or snapshot.version <= max_version
        ]
        if not candidatos:
            return None
        return max(candidatos, key=lambda snapshot: snapshot.version)

    async def save(self, snapshot: Snapshot) -> None:
        guardados = self._por_stream.setdefault(snapshot.stream_id, [])
        # Guardar dos veces la misma versión es idempotente: se reemplaza. Acumular
        # duplicados haría crecer el historial sin aportar ningún punto de retroceso nuevo.
        guardados[:] = [
            existente for existente in guardados if existente.version != snapshot.version
        ]
        guardados.append(snapshot)

    async def delete(self, stream_id: str) -> None:
        self._por_stream.pop(stream_id, None)


class InMemoryCheckpointStore(AbstractCheckpointStore):
    """Checkpoints en memoria, uno por suscripción."""

    def __init__(self) -> None:
        self._posiciones: dict[str, int] = {}

    async def load(self, subscription: str) -> int:
        return self._posiciones.get(subscription, 0)

    async def save(self, subscription: str, position: int) -> None:
        self._posiciones[subscription] = position

    async def reset(self, subscription: str) -> None:
        self._posiciones.pop(subscription, None)
