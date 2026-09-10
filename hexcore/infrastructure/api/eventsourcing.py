"""
Contenedor y providers FastAPI del Event Store.

Calcado de `hexcore/infrastructure/api/cqrs.py`, y por el mismo motivo: los providers existen
como funciones —y no como accesos directos al contenedor— para que
`app.dependency_overrides[provide_event_store] = ...` funcione en los tests. Si el endpoint
tocara el singleton, no habría nada que sobreescribir.

**Una sola fuente de verdad.** El contenedor que leen estos providers es el mismo que usa el
proyector y el que usa el relay. Duplicar la construcción es cómo se termina con un almacén
en la web y otro en el worker, cada uno con su propio checkpoint.
"""
from __future__ import annotations

import threading
import typing as t

from hexcore.domain.eventsourcing.projections import AbstractCheckpointStore
from hexcore.domain.eventsourcing.snapshots import AbstractSnapshotStore
from hexcore.domain.eventsourcing.store import AbstractEventStore

if t.TYPE_CHECKING:
    from hexcore.application.eventsourcing.config import EventStoreConfig
    from hexcore.application.eventsourcing.factory import EventStoreFactory
    from hexcore.application.eventsourcing.projector import Projector
    from hexcore.application.eventsourcing.relay import EventStoreRelay
    from hexcore.domain.cqrs.buses import AbstractEventBus
    from hexcore.domain.cqrs.serializer import AbstractSerializer

__all__ = [
    "EventStoreContainer",
    "configure_event_store",
    "get_event_store_container",
    "reset_event_store",
    "provide_event_store",
    "provide_snapshot_store",
    "provide_checkpoint_store",
    "provide_projector",
]


class EventStoreContainer:
    """
    Contenedor perezoso del event store y sus piezas.

    Construye cada cosa la primera vez que se pide y la cachea. Perezoso a propósito:
    `configure_event_store()` se puede llamar en import time sin tocar la base ni Redis.
    """

    def __init__(self, factory: "EventStoreFactory", config: "EventStoreConfig") -> None:
        self._factory = factory
        self._config = config
        self._projector: "Projector | None" = None
        self._lock = threading.RLock()

    @property
    def config(self) -> "EventStoreConfig":
        return self._config

    @property
    def publish_after_commit_recomendado(self) -> bool:
        """
        Qué debería pasarle el llamador al Unit of Work.

        `False` cuando el relay está activo. Existe para que la regla no dependa de que
        alguien la recuerde: con el relay corriendo **y** el UoW publicando, cada evento sale
        dos veces —en silencio— y con handlers no idempotentes eso hace daño real.
        """
        return not self._config.relay_enabled

    def store(self) -> AbstractEventStore:
        with self._lock:
            return self._factory.create_store()

    def snapshots(self) -> AbstractSnapshotStore | None:
        with self._lock:
            return self._factory.create_snapshot_store()

    def checkpoints(self) -> AbstractCheckpointStore:
        with self._lock:
            return self._factory.create_checkpoint_store()

    def projector(self) -> "Projector":
        with self._lock:
            if self._projector is None:
                self._projector = self._factory.create_projector()
            return self._projector

    def relay(self, bus: "AbstractEventBus") -> "EventStoreRelay":
        """
        El relay. **No se cachea**, a diferencia del proyector.

        Toma el bus como argumento, y un contenedor cacheado ligado a un bus concreto sería
        una trampa: pedirlo con otro bus devolvería el primero, publicando en el lugar
        equivocado sin ningún error.
        """
        return self._factory.create_relay(bus)


_contenedor: EventStoreContainer | None = None
_lock = threading.RLock()


def configure_event_store(
    *,
    config: "EventStoreConfig | None" = None,
    serializer: "AbstractSerializer | None" = None,
) -> EventStoreContainer:
    """
    Configura el contenedor global. Llamalo una vez, al arrancar.

    Args:
        config: La configuración. Si no viene, se lee `ServerConfig.event_store`, y si ahí
            tampoco hay nada se usan los defaults —que incluyen el almacén **en memoria**, o
            sea sin persistencia.
        serializer: El serializador a compartir. **Pasale el de CQRS** si tenés CQRS
            configurado: si divergen, un evento escrito por el store y otro publicado por el
            bus no tienen el mismo formato.

    Returns:
        El contenedor, para poder usarlo sin volver a pedirlo.
    """
    from hexcore.application.eventsourcing.config import EventStoreConfig
    from hexcore.application.eventsourcing.factory import EventStoreFactory

    global _contenedor

    resuelta: EventStoreConfig
    if config is not None:
        resuelta = config
    else:
        from hexcore.config import LazyConfig

        # `ServerConfig.event_store` esta tipado `t.Any` -- anotarlo de verdad obligaria a
        # importar este modulo desde `hexcore.config`, que ya arrastra medio framework --,
        # asi que el checker no puede estrechar la asignacion. La variable local anotada es
        # lo que le devuelve el tipo.
        declarada = LazyConfig.get_config().event_store
        resuelta = declarada if isinstance(declarada, EventStoreConfig) else EventStoreConfig()

    if serializer is None:
        serializer = _serializador_de_cqrs()

    with _lock:
        _contenedor = EventStoreContainer(
            EventStoreFactory(resuelta, serializer), resuelta
        )
    return _contenedor


def _serializador_de_cqrs() -> "AbstractSerializer | None":
    """
    El serializador del contenedor de CQRS, si hay uno configurado.

    Compartirlo es lo que evita que el almacén y el bus diverjan de formato. Si CQRS no está
    configurado no es un error: la factory cae a `PydanticSerializer`, que es el mismo que
    usaría CQRS por defecto.
    """
    try:
        from hexcore.infrastructure.api.cqrs import get_cqrs_container

        return get_cqrs_container()._factory.create_serializer()  # pyright: ignore[reportPrivateUsage]
    except Exception:
        return None


def get_event_store_container() -> EventStoreContainer:
    """
    El contenedor global.

    Raises:
        RuntimeError: Si no se llamó a `configure_event_store()`. Es un error explícito y no
            un contenedor por defecto: un almacén en memoria creado en silencio parece
            funcionar y no persiste nada.
    """
    if _contenedor is None:
        raise RuntimeError(
            "El event store no está configurado. Llamá a configure_event_store() al "
            "arrancar la aplicación."
        )
    return _contenedor


def reset_event_store() -> None:
    """
    Descarta el contenedor global.

    Para tests y para el arranque de un worker. `tests/conftest.py` lo llama entre casos: sin
    eso, el contenedor de un test contamina al siguiente — que es exactamente el defecto que
    tenía `ServerConfig.event_bus` con su instancia compartida.
    """
    global _contenedor
    with _lock:
        _contenedor = None


# ── Providers de FastAPI ──────────────────────────────────────────────────────
def provide_event_store() -> AbstractEventStore:
    return get_event_store_container().store()


def provide_snapshot_store() -> AbstractSnapshotStore | None:
    return get_event_store_container().snapshots()


def provide_checkpoint_store() -> AbstractCheckpointStore:
    return get_event_store_container().checkpoints()


def provide_projector() -> "Projector":
    return get_event_store_container().projector()
