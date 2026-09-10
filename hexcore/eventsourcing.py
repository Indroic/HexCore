"""
hexcore.eventsourcing — la superficie pública del Event Sourcing.

Un import obvio por tarea. Fachada propia y no `hexcore.cqrs` porque ésa ya expone medio
centenar de nombres y modela otra cosa: los buses de mensajes. El event store es útil sin
CQRS y al revés.

Es perezosa (PEP 562), y eso es lo que hace que convivan aquí adaptadores con dependencias
distintas: `SqlAlchemyEventStore` sólo exige `[sql]` **en el momento en que lo pedís**,
`BeanieEventStore` sólo `[mongo]`, y `RedisEventStore` sólo `[redis]`. Importar este módulo
no exige ninguno.

Uso típico::

    from hexcore.eventsourcing import AggregateRoot, when, EventSourcedRepository

    class Pedido(AggregateRoot):
        total: int

        @when(PedidoPagado)
        def _pagar(self, evento: PedidoPagado) -> None:
            self.total += evento.monto

    repo = EventSourcedRepository(Pedido, store=store)
    pedido = await repo.get(pedido_id)
"""
from __future__ import annotations

import typing as t

# name -> (módulo, atributo)
_EXPORTS: dict[str, tuple[str, str]] = {
    # ── El agregado ───────────────────────────────────────────────────────────
    "AggregateRoot": ("hexcore.domain.eventsourcing.aggregate", "AggregateRoot"),
    "when": ("hexcore.domain.eventsourcing.aggregate", "when"),
    "EventRecorder": ("hexcore.domain.base", "EventRecorder"),
    # ── El evento persistido ──────────────────────────────────────────────────
    "StoredEvent": ("hexcore.domain.eventsourcing.stored", "StoredEvent"),
    "EXPECTED_VERSION_ANY": (
        "hexcore.domain.eventsourcing.stored",
        "EXPECTED_VERSION_ANY",
    ),
    "EXPECTED_VERSION_NO_STREAM": (
        "hexcore.domain.eventsourcing.stored",
        "EXPECTED_VERSION_NO_STREAM",
    ),
    # ── Puertos ───────────────────────────────────────────────────────────────
    "AbstractEventStore": ("hexcore.domain.eventsourcing.store", "AbstractEventStore"),
    "AbstractSnapshotStore": (
        "hexcore.domain.eventsourcing.snapshots",
        "AbstractSnapshotStore",
    ),
    "Snapshot": ("hexcore.domain.eventsourcing.snapshots", "Snapshot"),
    "AbstractProjection": (
        "hexcore.domain.eventsourcing.projections",
        "AbstractProjection",
    ),
    "AbstractCheckpointStore": (
        "hexcore.domain.eventsourcing.projections",
        "AbstractCheckpointStore",
    ),
    # ── Excepciones ───────────────────────────────────────────────────────────
    "EventSourcingError": (
        "hexcore.domain.eventsourcing.exceptions",
        "EventSourcingError",
    ),
    "ConcurrencyError": (
        "hexcore.domain.eventsourcing.exceptions",
        "ConcurrencyError",
    ),
    "AggregateNotFoundError": (
        "hexcore.domain.eventsourcing.exceptions",
        "AggregateNotFoundError",
    ),
    "UnhandledEventError": (
        "hexcore.domain.eventsourcing.exceptions",
        "UnhandledEventError",
    ),
    # ── Aplicación ────────────────────────────────────────────────────────────
    "EventSourcedRepository": (
        "hexcore.application.eventsourcing.repository",
        "EventSourcedRepository",
    ),
    "Projector": ("hexcore.application.eventsourcing.projector", "Projector"),
    "EventStoreRelay": ("hexcore.application.eventsourcing.relay", "EventStoreRelay"),
    # ── Configuración ─────────────────────────────────────────────────────────
    "EventStoreConfig": (
        "hexcore.application.eventsourcing.config",
        "EventStoreConfig",
    ),
    "SnapshotConfig": ("hexcore.application.eventsourcing.config", "SnapshotConfig"),
    "ProjectionsConfig": (
        "hexcore.application.eventsourcing.config",
        "ProjectionsConfig",
    ),
    "EventStoreFactory": (
        "hexcore.application.eventsourcing.factory",
        "EventStoreFactory",
    ),
    # ── Adaptador en memoria (sin extras) ─────────────────────────────────────
    "InMemoryEventStore": (
        "hexcore.infrastructure.eventsourcing.memory",
        "InMemoryEventStore",
    ),
    "InMemorySnapshotStore": (
        "hexcore.infrastructure.eventsourcing.memory",
        "InMemorySnapshotStore",
    ),
    "InMemoryCheckpointStore": (
        "hexcore.infrastructure.eventsourcing.memory",
        "InMemoryCheckpointStore",
    ),
    # ── Adaptador SQLAlchemy — requiere [sql] ─────────────────────────────────
    "SqlAlchemyEventStore": (
        "hexcore.infrastructure.eventsourcing.sqlalchemy_store",
        "SqlAlchemyEventStore",
    ),
    "SqlAlchemySnapshotStore": (
        "hexcore.infrastructure.eventsourcing.sqlalchemy_store",
        "SqlAlchemySnapshotStore",
    ),
    "SqlAlchemyCheckpointStore": (
        "hexcore.infrastructure.eventsourcing.sqlalchemy_store",
        "SqlAlchemyCheckpointStore",
    ),
    "EventStoreModel": (
        "hexcore.infrastructure.eventsourcing.sqlalchemy_models",
        "EventStoreModel",
    ),
    "SnapshotModel": (
        "hexcore.infrastructure.eventsourcing.sqlalchemy_models",
        "SnapshotModel",
    ),
    "ProjectionCheckpointModel": (
        "hexcore.infrastructure.eventsourcing.sqlalchemy_models",
        "ProjectionCheckpointModel",
    ),
    "EventStoreMixin": (
        "hexcore.infrastructure.eventsourcing.sqlalchemy_models_mixins",
        "EventStoreMixin",
    ),
    "SnapshotMixin": (
        "hexcore.infrastructure.eventsourcing.sqlalchemy_models_mixins",
        "SnapshotMixin",
    ),
    "ProjectionCheckpointMixin": (
        "hexcore.infrastructure.eventsourcing.sqlalchemy_models_mixins",
        "ProjectionCheckpointMixin",
    ),
    "create_eventstore_tables": (
        "hexcore.infrastructure.eventsourcing.sqlalchemy_models",
        "create_eventstore_tables",
    ),
    "drop_eventstore_tables": (
        "hexcore.infrastructure.eventsourcing.sqlalchemy_models",
        "drop_eventstore_tables",
    ),
    # ── Adaptador Beanie — requiere [mongo] ───────────────────────────────────
    "BeanieEventStore": (
        "hexcore.infrastructure.eventsourcing.beanie_store",
        "BeanieEventStore",
    ),
    "BeanieSnapshotStore": (
        "hexcore.infrastructure.eventsourcing.beanie_store",
        "BeanieSnapshotStore",
    ),
    "BeanieCheckpointStore": (
        "hexcore.infrastructure.eventsourcing.beanie_store",
        "BeanieCheckpointStore",
    ),
    "StoredEventDocument": (
        "hexcore.infrastructure.eventsourcing.beanie_documents",
        "StoredEventDocument",
    ),
    "SnapshotDocument": (
        "hexcore.infrastructure.eventsourcing.beanie_documents",
        "SnapshotDocument",
    ),
    "ProjectionCheckpointDocument": (
        "hexcore.infrastructure.eventsourcing.beanie_documents",
        "ProjectionCheckpointDocument",
    ),
    "init_eventstore_documents": (
        "hexcore.infrastructure.eventsourcing.beanie_documents",
        "init_eventstore_documents",
    ),
    # ── Adaptador Redis — requiere [redis] ────────────────────────────────────
    "RedisEventStore": (
        "hexcore.infrastructure.eventsourcing.redis_store",
        "RedisEventStore",
    ),
    "RedisCheckpointStore": (
        "hexcore.infrastructure.eventsourcing.redis_store",
        "RedisCheckpointStore",
    ),
    # ── Contenedor y providers ────────────────────────────────────────────────
    "EventStoreContainer": (
        "hexcore.infrastructure.api.eventsourcing",
        "EventStoreContainer",
    ),
    "configure_event_store": (
        "hexcore.infrastructure.api.eventsourcing",
        "configure_event_store",
    ),
    "get_event_store_container": (
        "hexcore.infrastructure.api.eventsourcing",
        "get_event_store_container",
    ),
    "reset_event_store": (
        "hexcore.infrastructure.api.eventsourcing",
        "reset_event_store",
    ),
    "provide_event_store": (
        "hexcore.infrastructure.api.eventsourcing",
        "provide_event_store",
    ),
    "provide_snapshot_store": (
        "hexcore.infrastructure.api.eventsourcing",
        "provide_snapshot_store",
    ),
    "provide_checkpoint_store": (
        "hexcore.infrastructure.api.eventsourcing",
        "provide_checkpoint_store",
    ),
    "provide_projector": (
        "hexcore.infrastructure.api.eventsourcing",
        "provide_projector",
    ),
}

# `sorted(...)` no es una expresión que Pyright pueda evaluar para armar el `__all__`, así
# que la regla se silencia acá. La alternativa —escribir la lista a mano— se desincroniza
# del dict a la primera, y el `.pyi` generado sí lleva el `__all__` literal que el checker
# necesita.
__all__ = sorted(_EXPORTS)  # pyright: ignore[reportUnsupportedDunderAll]


def __getattr__(name: str) -> t.Any:
    try:
        module_path, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(
            f"module 'hexcore.eventsourcing' has no attribute {name!r}"
        ) from None

    import importlib

    value = getattr(importlib.import_module(module_path), attribute)
    # Se cachea en el módulo para que el segundo acceso no vuelva a importar.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return __all__
