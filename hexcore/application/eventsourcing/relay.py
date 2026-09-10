"""
`EventStoreRelay`: publica al bus lo que ya está en el almacén.

Es la mitad de salida del patrón outbox. La otra mitad la pone el Unit of Work al escribir el
evento en la misma transacción que el cambio: eso garantiza que el hecho quedó registrado. El
relay garantiza que además se entrega.

**Con SQL, la tabla del event store *es* el outbox.** No hace falta una segunda tabla, y esa
es la mitad de la razón por la que `StoredEvent.payload` guarda el sobre completo de
`serialize_envelope()` en vez de sólo los datos: republicar es reconstruir el sobre y
publicarlo, con el actor y el `request_id` originales intactos.
"""
from __future__ import annotations

import asyncio
import logging
import typing as t
from collections import deque

from hexcore.domain.cqrs.buses import AbstractEventBus
from hexcore.domain.cqrs.exceptions import DeserializationError
from hexcore.domain.eventsourcing.projections import AbstractCheckpointStore
from hexcore.domain.eventsourcing.store import AbstractEventStore
from hexcore.domain.eventsourcing.stored import StoredEvent

__all__ = ["EventStoreRelay"]

logger = logging.getLogger("hexcore.eventsourcing.relay")

PoliticaDeError = t.Literal["stop", "skip"]


class EventStoreRelay:
    """
    Lee el orden global del almacén y publica cada evento al bus.

    ## Lo que garantiza, y lo que no

    **At-least-once.** El checkpoint se guarda *después* de publicar el lote, así que una
    caída entre el publish y el save republica ese lote al reiniciar. El orden inverso sería
    peor: perdería eventos para siempre, porque al reiniciar el checkpoint diría que ya se
    entregaron.

    **Exactly-once no se ofrece, y no es una limitación de esta implementación.** Publicar en
    un broker y marcar el progreso en la base son dos sistemas distintos; hacerlos atómicos
    exige una transacción distribuida que ni el broker ni la base soportan acá. Prometerlo
    sería mentir. Los handlers tienen que ser idempotentes, y `event_id` es la clave con la
    que deduplicar.

    ## La doble publicación

    Con un relay corriendo, el UoW **no** debe publicar: `SqlAlchemyUnitOfWork(...,
    publish_after_commit=False)`. Con los dos activos cada evento sale dos veces, en silencio,
    y con handlers no idempotentes eso hace daño real.
    """

    def __init__(
        self,
        *,
        store: AbstractEventStore,
        bus: AbstractEventBus,
        checkpoints: AbstractCheckpointStore,
        subscription: str = "relay",
        batch_size: int = 200,
        poll_interval: float = 0.5,
        safety_window: int = 0,
        stream_types: t.Sequence[str] | None = None,
        on_error: PoliticaDeError = "stop",
    ) -> None:
        """
        Args:
            store: De dónde se leen los eventos.
            bus: A dónde se publican.
            checkpoints: Dónde se recuerda el avance. **Con su propia suscripción**, distinta
                de la de cualquier proyector: el relay y las proyecciones avanzan a ritmos
                distintos, y compartir clave haría que se pisaran el progreso.
            subscription: La clave del checkpoint.
            batch_size: Cuántos eventos se leen por vuelta.
            poll_interval: Cuánto espera `run_forever()` cuando no hay nada nuevo.
            safety_window: Cuántas posiciones releer por detrás del checkpoint. Ver
                `AbstractEventStore.read_all` para por qué hace falta.
            stream_types: Publicar sólo estas categorías. Por defecto, todas.
            on_error: Qué hacer si `publish()` falla. `"stop"` deja el checkpoint en el último
                evento publicado con éxito y propaga.
        """
        if batch_size <= 0:
            raise ValueError("batch_size tiene que ser positivo.")
        if safety_window < 0:
            raise ValueError("safety_window no puede ser negativo.")

        self._store = store
        self._bus = bus
        self._checkpoints = checkpoints
        self._subscription = subscription
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._safety_window = safety_window
        self._stream_types = list(stream_types) if stream_types else None
        self._on_error: PoliticaDeError = on_error
        self._detenido = asyncio.Event()
        self._vistos: deque[str] = deque(maxlen=max(safety_window, 1) * 2)
        self._vistos_indice: set[str] = set()

    async def run_once(self) -> int:
        """
        Publica un lote y devuelve cuántos eventos salieron.

        Devuelve 0 cuando no hay nada nuevo, que es la señal que usa `run_forever()` para
        dormir. Un lote parcialmente publicado deja el checkpoint en el último que salió bien.
        """
        checkpoint = await self._checkpoints.load(self._subscription)
        desde = max(0, checkpoint - self._safety_window)

        lote = await self._store.read_all(
            from_position=desde,
            limit=self._batch_size,
            stream_types=self._stream_types,
        )
        if not lote:
            return 0

        publicados = 0
        posicion = checkpoint
        fallo: Exception | None = None

        for persistido in lote:
            if self._ya_publicado(persistido, checkpoint):
                posicion = max(posicion, persistido.global_position)
                continue

            try:
                await self._publicar(persistido)
            except Exception as error:
                if self._on_error == "stop":
                    fallo = error
                    break
                logger.exception(
                    "No se pudo publicar el evento de la posicion %d (%s) y "
                    "on_error='skip': se saltea y no se va a reintentar.",
                    persistido.global_position,
                    persistido.event_type,
                )
                posicion = max(posicion, persistido.global_position)
                self._marcar_visto(persistido)
                continue

            publicados += 1
            posicion = max(posicion, persistido.global_position)
            self._marcar_visto(persistido)

        if posicion > checkpoint:
            # **Después** de publicar, nunca antes. Ver el docstring de la clase.
            await self._checkpoints.save(self._subscription, posicion)

        if fallo is not None:
            raise fallo

        return publicados

    async def run_forever(self) -> None:
        """Publica indefinidamente, esperando `poll_interval` cuando no hay nada nuevo."""
        self._detenido.clear()
        while not self._detenido.is_set():
            publicados = await self.run_once()
            if publicados == 0:
                try:
                    await asyncio.wait_for(
                        self._detenido.wait(), timeout=self._poll_interval
                    )
                except TimeoutError:
                    continue

    def stop(self) -> None:
        """Pide la detención. `run_forever()` sale al terminar el lote en curso."""
        self._detenido.set()

    # ── Interno ───────────────────────────────────────────────────────────────
    async def _publicar(self, persistido: StoredEvent) -> None:
        try:
            evento = self._store.rehydrate(persistido)
        except DeserializationError as error:
            raise DeserializationError(
                f"no se pudo reconstruir el evento de la posicion "
                f"{persistido.global_position} (tipo '{persistido.event_type}', stream "
                f"'{persistido.stream_id}'): {error}"
            ) from error

        await self._bus.publish(evento)

    def _ya_publicado(self, persistido: StoredEvent, checkpoint: int) -> bool:
        if persistido.global_position > checkpoint:
            return False
        return str(persistido.event_id) in self._vistos_indice

    def _marcar_visto(self, persistido: StoredEvent) -> None:
        if self._safety_window <= 0:
            return
        clave = str(persistido.event_id)
        if len(self._vistos) == self._vistos.maxlen:
            self._vistos_indice.discard(self._vistos[0])
        self._vistos.append(clave)
        self._vistos_indice.add(clave)
