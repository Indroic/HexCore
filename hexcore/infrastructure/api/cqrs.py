"""
Providers FastAPI para los buses CQRS.

Existen como funciones —y no como accesos directos al contenedor— por una única razón:
`app.dependency_overrides[provide_command_bus] = ...` en los tests. Si el endpoint
tocara el singleton, no habría nada que sobreescribir.

**Una sola fuente de verdad.** El contenedor que leen estos providers es el mismo que
usa el worker: se configura una vez con `configure_cqrs(...)` y tanto la app web como el
entrypoint del worker lo consumen. Duplicar la construcción es cómo se acaba con un
registry en el web que no tiene los handlers del worker.
"""
from __future__ import annotations

import threading
import typing as t

from hexcore.domain.cqrs.buses import (
    AbstractCommandBus,
    AbstractEventBus,
    AbstractQueryBus,
)

if t.TYPE_CHECKING:
    from hexcore.application.cqrs.factory import CQRSFactory
    from hexcore.application.cqrs.registry import HandlerRegistry
    from hexcore.domain.cqrs.serializer import ISerializer
    from hexcore.domain.cqrs.task_queues import ITaskEnqueuer
    from hexcore.infrastructure.workers.consumer import CQRSConsumer

__all__ = [
    "CQRSContainer",
    "configure_cqrs",
    "get_cqrs_container",
    "reset_cqrs",
    "provide_command_bus",
    "provide_query_bus",
    "provide_event_bus",
    "provide_registry",
]


class CQRSContainer:
    """
    Contenedor perezoso de los buses CQRS.

    Construye los buses la primera vez que se piden y los cachea. Perezoso a propósito:
    `configure_cqrs()` se puede llamar en import time sin tocar la BD ni el broker.
    """

    def __init__(
        self,
        factory: "CQRSFactory",
    ) -> None:
        self._factory = factory
        self._command_bus: AbstractCommandBus | None = None
        self._query_bus: AbstractQueryBus | None = None
        self._event_bus: AbstractEventBus | None = None
        self._lock = threading.RLock()

    @property
    def registry(self) -> "HandlerRegistry":
        return self._factory._registry  # noqa: SLF001 - misma capa lógica

    @property
    def serializer(self) -> "ISerializer":
        return self._factory.create_serializer()

    def command_bus(self) -> AbstractCommandBus:
        with self._lock:
            if self._command_bus is None:
                self._command_bus = t.cast(
                    AbstractCommandBus, self._factory.create_command_bus()
                )
            return self._command_bus

    def query_bus(self) -> AbstractQueryBus:
        with self._lock:
            if self._query_bus is None:
                self._query_bus = t.cast(
                    AbstractQueryBus, self._factory.create_query_bus()
                )
            return self._query_bus

    def event_bus(self) -> AbstractEventBus:
        with self._lock:
            if self._event_bus is None:
                self._event_bus = t.cast(
                    AbstractEventBus, self._factory.create_event_bus()
                )
            return self._event_bus

    def build_consumer(self) -> "CQRSConsumer":
        """
        Construye el `CQRSConsumer` del worker sobre **estos mismos** buses.

        Es lo que garantiza que el worker y la app web comparten registry y serializer.
        """
        from hexcore.infrastructure.workers.consumer import CQRSConsumer

        return CQRSConsumer(
            command_bus=self.command_bus(),
            event_bus=self.event_bus(),
            serializer=self.serializer,
        )


_container: CQRSContainer | None = None
_container_lock = threading.RLock()


def configure_cqrs(
    registry: "HandlerRegistry",
    *,
    config: t.Any = None,
    enqueuer: "ITaskEnqueuer | None" = None,
) -> CQRSContainer:
    """
    Configura el contenedor CQRS del proceso. Llamalo una vez, al arrancar.

    Args:
        registry: El registry con los handlers ya registrados.
        config: Un `CQRSConfig`. Por defecto, el de `ServerConfig.cqrs` si está, o
            `CQRSConfig()`.
        enqueuer: El `ITaskEnqueuer` para el Smart Routing. Sin él, un
            `@background_command` registrado hace fallar la construcción del bus (P0-5).

    Returns:
        El contenedor, por si querés usarlo directamente (p. ej. `build_consumer()`).
    """
    from hexcore.application.cqrs.config import CQRSConfig
    from hexcore.application.cqrs.factory import CQRSFactory

    global _container

    if config is None:
        from hexcore.config import LazyConfig

        config = getattr(LazyConfig.get_config(), "cqrs", None) or CQRSConfig()

    with _container_lock:
        _container = CQRSContainer(
            CQRSFactory(config, registry, enqueuer=enqueuer)
        )
        return _container


def get_cqrs_container() -> CQRSContainer:
    """
    El contenedor configurado.

    Raises:
        RuntimeError: Si nadie llamó a `configure_cqrs()`.
    """
    with _container_lock:
        if _container is None:
            raise RuntimeError(
                "El CQRS no está configurado. Llamá a "
                "hexcore.infrastructure.api.cqrs.configure_cqrs(registry, enqueuer=...) "
                "al arrancar la aplicación (o el worker), antes de resolver cualquier bus."
            )
        return _container


def reset_cqrs() -> None:
    """Descarta el contenedor. Para tests y para reconfigurar en un worker."""
    global _container

    with _container_lock:
        _container = None


# ── Dependencias FastAPI ──────────────────────────────────────────────────────
# Deliberadamente triviales: su valor está en ser un objeto sobre el que hacer
# `app.dependency_overrides[...] = ...`.


def provide_command_bus() -> AbstractCommandBus:
    """Dependencia FastAPI: el CommandBus del proceso."""
    return get_cqrs_container().command_bus()


def provide_query_bus() -> AbstractQueryBus:
    """Dependencia FastAPI: el QueryBus del proceso."""
    return get_cqrs_container().query_bus()


def provide_event_bus() -> AbstractEventBus:
    """Dependencia FastAPI: el EventBus del proceso."""
    return get_cqrs_container().event_bus()


def provide_registry() -> "HandlerRegistry":
    """Dependencia FastAPI: el `HandlerRegistry` del proceso."""
    return get_cqrs_container().registry
