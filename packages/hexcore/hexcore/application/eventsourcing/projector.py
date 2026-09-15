"""
`Projector`: recorre el orden global del almacén y alimenta las proyecciones.

Un proyector es un bucle simple con dos propiedades que no lo son: nunca avanza el checkpoint
por delante de lo que aplicó, y puede volver a empezar desde cero. La primera es lo que hace
que un fallo sea recuperable; la segunda es lo que permite cambiar la forma de un modelo de
lectura sin escribir una migración.
"""
from __future__ import annotations

import asyncio
import logging
import typing as t
from collections import deque

from hexcore.domain.cqrs.exceptions import DeserializationError
from hexcore.domain.cqrs.resolution import resolve_dotted
from hexcore.domain.eventsourcing.projections import (
    AbstractCheckpointStore,
    AbstractProjection,
)
from hexcore.domain.eventsourcing.store import AbstractEventStore
from hexcore.domain.eventsourcing.stored import StoredEvent

__all__ = ["Projector"]

logger = logging.getLogger("hexcore.eventsourcing.projector")

PoliticaDeError = t.Literal["stop", "skip"]


class Projector:
    """
    Aplica los eventos del almacén a un conjunto de proyecciones, en orden global.

    Todas las proyecciones de un mismo proyector comparten checkpoint y avanzan juntas. Ese
    agrupamiento no es una simplificación: es lo que mantiene consistentes entre sí a los
    modelos de lectura que se consultan juntos. Dos proyecciones que tienen que cuadrar van
    en el mismo proyector; dos que no, en proyectores distintos con suscripciones distintas.
    """

    def __init__(
        self,
        *,
        store: AbstractEventStore,
        checkpoints: AbstractCheckpointStore,
        projections: t.Sequence[AbstractProjection],
        subscription: str = "default",
        batch_size: int = 500,
        poll_interval: float = 0.5,
        safety_window: int = 0,
        on_error: PoliticaDeError = "stop",
    ) -> None:
        """
        Args:
            store: De dónde se leen los eventos.
            checkpoints: Dónde se recuerda hasta dónde se leyó.
            projections: Las proyecciones a alimentar, en orden de aplicación.
            subscription: La clave del checkpoint. Dos proyectores con la misma clave se
                pisan el avance mutuamente, así que cada grupo necesita la suya.
            batch_size: Cuántos eventos se leen por vuelta.
            poll_interval: Cuánto espera `run_forever()` cuando no hay nada nuevo.
            safety_window: Cuántas posiciones releer por detrás del checkpoint en cada
                arranque. Ver más abajo.
            on_error: Qué hacer cuando una proyección falla.

        **`safety_window` es la respuesta al hueco de la posición global.** Una secuencia se
        toma al insertar y no al comitear, así que la transacción que reservó la posición 10
        puede comitear después de la que reservó la 11: un lector que ya pasó por la 11 nunca
        vería la 10. Releer una ventana hacia atrás le da a esas escrituras rezagadas la
        chance de aparecer. El precio es que los eventos de la ventana se reprocesan, y por
        eso **las proyecciones tienen que ser idempotentes** — dentro de una misma corrida se
        deduplica por `event_id`, pero tras un reinicio la ventana se relee entera.

        Un valor razonable es el doble de las escrituras concurrentes que se esperan; 0 lo
        desactiva, que es lo correcto con un almacén de un solo escritor o con
        `ordering="serialized"`.
        """
        if batch_size <= 0:
            raise ValueError("batch_size tiene que ser positivo.")
        if safety_window < 0:
            raise ValueError("safety_window no puede ser negativo.")

        self._store = store
        self._checkpoints = checkpoints
        self._projections = list(projections)
        self._subscription = subscription
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._safety_window = safety_window
        self._on_error: PoliticaDeError = on_error
        self._detenido = asyncio.Event()
        # Sólo tiene que cubrir la ventana: fuera de ella, `from_position` ya garantiza que
        # nada se repite. Acotarlo evita que un proyector de vida larga acumule ids sin fin.
        self._vistos: deque[str] = deque(maxlen=max(safety_window, 1) * 2)
        self._vistos_indice: set[str] = set()
        self._tipos: dict[str, type | None] = {}

    # ── Ejecución ─────────────────────────────────────────────────────────────
    async def catch_up(self) -> int:
        """
        Procesa hasta agotar el almacén y vuelve.

        Returns:
            Cuántos eventos se aplicaron.

        Raises:
            Lo que haya lanzado una proyección, si `on_error="stop"`. El checkpoint queda en
            el último evento aplicado con éxito, así que reintentar retoma exactamente ahí.
        """
        procesados = 0
        checkpoint = await self._checkpoints.load(self._subscription)
        posicion = max(0, checkpoint - self._safety_window)

        while not self._detenido.is_set():
            lote = await self._store.read_all(
                from_position=posicion, limit=self._batch_size
            )
            if not lote:
                break

            aplicados, posicion, fallo = await self._procesar_lote(lote, checkpoint)
            procesados += aplicados
            if aplicados:
                checkpoint = max(checkpoint, posicion)
                await self._checkpoints.save(self._subscription, checkpoint)
            if fallo is not None:
                raise fallo

        return procesados

    async def run_forever(self) -> None:
        """
        Procesa indefinidamente, esperando `poll_interval` cuando no hay nada nuevo.

        `stop()` lo corta. Un error de una proyección con `on_error="stop"` **también** lo
        corta, propagando: un proyector que sigue girando sobre un evento que no puede
        aplicar no avanza y llena el log, y quien lo lanzó tiene que enterarse.
        """
        self._detenido.clear()
        while not self._detenido.is_set():
            procesados = await self.catch_up()
            if procesados == 0:
                try:
                    await asyncio.wait_for(
                        self._detenido.wait(), timeout=self._poll_interval
                    )
                except TimeoutError:
                    continue

    async def rebuild(self) -> int:
        """
        Vacía las proyecciones y las reconstruye desde el principio del almacén.

        Es la operación que hace innecesarias las migraciones de un modelo de lectura: si su
        forma cambia, se reconstruye. Y por eso mismo **es destructiva**: llama a `reset()`
        en cada proyección antes de empezar.

        Una proyección que escribe y no implementa `reset()` produce un rebuild incorrecto —
        los datos viejos quedan mezclados con los nuevos — y este método no puede detectarlo,
        porque `reset()` tiene un default no-op para las proyecciones sin estado propio.
        """
        for proyeccion in self._projections:
            await proyeccion.reset()

        await self._checkpoints.reset(self._subscription)
        self._vistos.clear()
        self._vistos_indice.clear()
        return await self.catch_up()

    def stop(self) -> None:
        """Pide la detención. `run_forever()` sale en cuanto termina el lote en curso."""
        self._detenido.set()

    # ── Interno ───────────────────────────────────────────────────────────────
    async def _procesar_lote(
        self, lote: t.Sequence[StoredEvent], checkpoint: int
    ) -> tuple[int, int, Exception | None]:
        """
        Aplica un lote.

        Returns:
            `(aplicados, última posición confirmada, error a propagar)`. La posición
            devuelta es la del último evento que se aplicó **con éxito**: el checkpoint no
            puede adelantarse al trabajo hecho, o un reintento saltearía el evento que falló.
        """
        aplicados = 0
        posicion = checkpoint

        for persistido in lote:
            if self._ya_procesado(persistido, checkpoint):
                posicion = max(posicion, persistido.global_position)
                continue

            try:
                await self._aplicar(persistido)
            except Exception as error:
                if self._on_error == "stop":
                    return aplicados, posicion, error
                logger.exception(
                    "La proyeccion fallo en la posicion %d (%s) y on_error='skip': se "
                    "saltea. El modelo de lectura queda incompleto.",
                    persistido.global_position,
                    persistido.event_type,
                )
                posicion = max(posicion, persistido.global_position)
                self._marcar_visto(persistido)
                continue

            aplicados += 1
            posicion = max(posicion, persistido.global_position)
            self._marcar_visto(persistido)

        return aplicados, posicion, None

    async def _aplicar(self, persistido: StoredEvent) -> None:
        interesadas = [
            proyeccion
            for proyeccion in self._projections
            if self._le_interesa(proyeccion, persistido)
        ]
        if not interesadas:
            return

        try:
            evento = self._store.rehydrate(persistido)
        except DeserializationError as error:
            # Caso real en un almacén con años de historia: la clase se renombró o se borró.
            # El mensaje nombra el tipo y la posición porque sin eso un rebuild que falla no
            # dice sobre qué, y hay que ir a buscarlo a mano en la tabla.
            raise DeserializationError(
                f"no se pudo reconstruir el evento de la posicion "
                f"{persistido.global_position} (tipo '{persistido.event_type}', stream "
                f"'{persistido.stream_id}'): {error}"
            ) from error

        for proyeccion in interesadas:
            await proyeccion.apply(evento, persistido)

    def _le_interesa(
        self, proyeccion: AbstractProjection, persistido: StoredEvent
    ) -> bool:
        """
        Si la proyección declara `handles`, se filtra por jerarquía **antes** de deserializar.

        Deserializar un evento que ninguna proyección quiere es trabajo puro, y en un rebuild
        de años de historia esa diferencia decide si tarda minutos u horas. Así que el filtro
        trabaja sobre el `event_type`, que es una columna, resolviendo el FQN a su clase una
        sola vez por tipo.

        Se resuelve el tipo en vez de recorrer `__subclasses__()` de lo declarado, que sería
        más directo y está mal: una subclase que vive en un módulo todavía no importado no
        aparece ahí, así que la proyección dejaría de recibir sus eventos **en silencio**, y
        el conjunto de eventos que recibe dependería del orden de imports del proceso.

        Un tipo que no se puede resolver —la clase se renombró o se borró— no se filtra: se
        deja pasar para que falle en `rehydrate` con el mensaje que nombra la posición y el
        tipo. Descartarlo acá lo volvería invisible.
        """
        if not proyeccion.handles:
            return True

        tipo = self._resolver(persistido.event_type)
        if tipo is None:
            return True

        return any(issubclass(tipo, declarado) for declarado in proyeccion.handles)

    def _resolver(self, event_type: str) -> type | None:
        """El FQN a su clase, cacheado: en un rebuild esto se pregunta una vez por evento."""
        if event_type in self._tipos:
            return self._tipos[event_type]

        try:
            resuelto = resolve_dotted(event_type)
        except LookupError:
            resuelto = None

        tipo = resuelto if isinstance(resuelto, type) else None
        self._tipos[event_type] = tipo
        return tipo

    def _ya_procesado(self, persistido: StoredEvent, checkpoint: int) -> bool:
        """
        Si el evento cae en la ventana de seguridad y ya se aplicó en esta corrida.

        Fuera de la ventana no hace falta preguntar: `from_position` es exclusivo, así que
        nada de lo que llega por encima del checkpoint se vio antes.
        """
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
