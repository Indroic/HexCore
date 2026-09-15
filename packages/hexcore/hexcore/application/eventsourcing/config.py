"""
Configuración declarativa del Event Store, integrada en `ServerConfig`.
"""
from __future__ import annotations

import typing as t

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["SnapshotConfig", "ProjectionsConfig", "EventStoreConfig"]


class SnapshotConfig(BaseModel):
    """Los snapshots. Desactivados por defecto: son una optimización, no un requisito."""

    #: Dotted path del `AbstractSnapshotStore`. `None` desactiva los snapshots.
    backend: str | None = None

    #: Cada cuántas versiones tomar uno. 0 lo desactiva aunque haya backend.
    every: int = 0

    options: dict[str, t.Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)


class ProjectionsConfig(BaseModel):
    """
    Las proyecciones y el proyector que las alimenta.

    `projections` son dotted paths, **explícitos**. No hay discovery, y no es un olvido: es
    la misma decisión que documenta `ServerConfig.repository_discovery_paths` al pasar a
    discovery explícito en v2, y acá la consecuencia sería peor. Una proyección descubierta
    por accidente entra en el `rebuild()`, y `rebuild()` llama a `reset()`: borraría un modelo
    de lectura que nadie quiso tocar.
    """

    subscription: str = "default"
    projections: list[str] = Field(default_factory=list)

    #: Dotted path del `AbstractCheckpointStore`. `None` usa el que corresponda al backend
    #: del almacén, o el de memoria.
    checkpoint_backend: str | None = None

    batch_size: int = 500
    poll_interval: float = 0.5

    #: Cuántas posiciones releer por detrás del checkpoint. Ver
    #: `AbstractEventStore.read_all`: la posición global es monotónica pero no contigua.
    safety_window: int = 0

    on_error: t.Literal["stop", "skip"] = "stop"

    model_config = ConfigDict(frozen=True)


class EventStoreConfig(BaseModel):
    """
    Configuración global del Event Store.

    Va como campo propio de `ServerConfig`, **hermano de `cqrs` y `darwin` y no dentro de
    `CQRSConfig`**. `CQRSConfig` está congelado y modela tres buses; el event store es útil
    sin CQRS, y meterlo ahí haría que `CQRSConfig(enabled=False)` apagara el almacén de la
    verdad — que es exactamente el tipo de acoplamiento que hace que una configuración
    razonable produzca pérdida de datos.

    Ejemplo en el `config.py` del proyecto::

        from hexcore.config import ServerConfig
        from hexcore.eventsourcing import EventStoreConfig, ProjectionsConfig

        config = ServerConfig(
            event_store=EventStoreConfig(
                backend="hexcore.eventsourcing.SqlAlchemyEventStore",
                projections=ProjectionsConfig(
                    projections=["app.proyecciones.ResumenDePedidos"],
                    safety_window=50,
                ),
                relay_enabled=True,
            ),
        )
    """

    enabled: bool = True

    #: Dotted path del `AbstractEventStore`. `None` usa `InMemoryEventStore`, que **no
    #: persiste nada**: es el default porque no exige ningún extra, no porque sirva en
    #: producción.
    backend: str | None = None

    options: dict[str, t.Any] = Field(default_factory=dict)

    #: Dotted path del serializador. `None` comparte el de CQRS si hay contenedor
    #: configurado, y si no usa `PydanticSerializer`.
    #:
    #: **Tiene que ser el mismo que el del bus.** Si divergen, un evento escrito por el store
    #: y otro publicado por el bus no tienen el mismo formato, y el día que haya que releer el
    #: historial con el consumidor del bus el payload no se va a poder reconstruir.
    serializer: str | None = None

    snapshots: SnapshotConfig = Field(default_factory=SnapshotConfig)
    projections: ProjectionsConfig = Field(default_factory=ProjectionsConfig)

    #: Si el `EventStoreRelay` publica al bus lo que se escribe en el almacén.
    #:
    #: Con esto en `True`, el Unit of Work **no** debe publicar
    #: (`publish_after_commit=False`), o cada evento sale dos veces. `configure_event_store()`
    #: lo expone en `publish_after_commit_recomendado` para que el llamador no tenga que
    #: recordar la regla.
    relay_enabled: bool = False

    relay_subscription: str = "relay"
    relay_batch_size: int = 200

    model_config = ConfigDict(frozen=True)
