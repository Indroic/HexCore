# ⚠️  ARCHIVO GENERADO — NO EDITAR A MANO.
#
# Generado por `scripts/gen_stubs.py` desde el `_EXPORTS` de `hexcore/eventsourcing.py`.
# Si editás esto a mano, el job `stubs-drift` de CI te lo va a revertir.
#
# Para regenerar:
#
#     uv run python scripts/gen_stubs.py --write
#
# Existe porque la fachada resuelve sus exports con `__getattr__` y declara
# `__all__ = sorted(_EXPORTS)`: las dos son expresiones de runtime, así que sin este stub
# los 53 símbolos de `hexcore.eventsourcing` tipan `Any`. El runtime no cambia — Python usa
# el `.py` y el checker usa el `.pyi`, así que la carga perezosa se mantiene.


from hexcore.application.eventsourcing.config import EventStoreConfig as EventStoreConfig
from hexcore.application.eventsourcing.config import ProjectionsConfig as ProjectionsConfig
from hexcore.application.eventsourcing.config import SnapshotConfig as SnapshotConfig
from hexcore.application.eventsourcing.factory import EventStoreFactory as EventStoreFactory
from hexcore.application.eventsourcing.projector import Projector as Projector
from hexcore.application.eventsourcing.relay import EventStoreRelay as EventStoreRelay
from hexcore.application.eventsourcing.repository import EventSourcedRepository as EventSourcedRepository
from hexcore.domain.base import EventRecorder as EventRecorder
from hexcore.domain.eventsourcing.aggregate import AggregateRoot as AggregateRoot
from hexcore.domain.eventsourcing.aggregate import when as when
from hexcore.domain.eventsourcing.exceptions import AggregateNotFoundError as AggregateNotFoundError
from hexcore.domain.eventsourcing.exceptions import ConcurrencyError as ConcurrencyError
from hexcore.domain.eventsourcing.exceptions import EventSourcingError as EventSourcingError
from hexcore.domain.eventsourcing.exceptions import UnhandledEventError as UnhandledEventError
from hexcore.domain.eventsourcing.projections import AbstractCheckpointStore as AbstractCheckpointStore
from hexcore.domain.eventsourcing.projections import AbstractProjection as AbstractProjection
from hexcore.domain.eventsourcing.snapshots import AbstractSnapshotStore as AbstractSnapshotStore
from hexcore.domain.eventsourcing.snapshots import Snapshot as Snapshot
from hexcore.domain.eventsourcing.store import AbstractEventStore as AbstractEventStore
from hexcore.domain.eventsourcing.stored import EXPECTED_VERSION_ANY as EXPECTED_VERSION_ANY
from hexcore.domain.eventsourcing.stored import EXPECTED_VERSION_NO_STREAM as EXPECTED_VERSION_NO_STREAM
from hexcore.domain.eventsourcing.stored import StoredEvent as StoredEvent
from hexcore.infrastructure.api.eventsourcing import EventStoreContainer as EventStoreContainer
from hexcore.infrastructure.api.eventsourcing import configure_event_store as configure_event_store
from hexcore.infrastructure.api.eventsourcing import get_event_store_container as get_event_store_container
from hexcore.infrastructure.api.eventsourcing import provide_checkpoint_store as provide_checkpoint_store
from hexcore.infrastructure.api.eventsourcing import provide_event_store as provide_event_store
from hexcore.infrastructure.api.eventsourcing import provide_projector as provide_projector
from hexcore.infrastructure.api.eventsourcing import provide_snapshot_store as provide_snapshot_store
from hexcore.infrastructure.api.eventsourcing import reset_event_store as reset_event_store
from hexcore.infrastructure.eventsourcing.beanie_documents import ProjectionCheckpointDocument as ProjectionCheckpointDocument
from hexcore.infrastructure.eventsourcing.beanie_documents import SnapshotDocument as SnapshotDocument
from hexcore.infrastructure.eventsourcing.beanie_documents import StoredEventDocument as StoredEventDocument
from hexcore.infrastructure.eventsourcing.beanie_documents import init_eventstore_documents as init_eventstore_documents
from hexcore.infrastructure.eventsourcing.beanie_store import BeanieCheckpointStore as BeanieCheckpointStore
from hexcore.infrastructure.eventsourcing.beanie_store import BeanieEventStore as BeanieEventStore
from hexcore.infrastructure.eventsourcing.beanie_store import BeanieSnapshotStore as BeanieSnapshotStore
from hexcore.infrastructure.eventsourcing.memory import InMemoryCheckpointStore as InMemoryCheckpointStore
from hexcore.infrastructure.eventsourcing.memory import InMemoryEventStore as InMemoryEventStore
from hexcore.infrastructure.eventsourcing.memory import InMemorySnapshotStore as InMemorySnapshotStore
from hexcore.infrastructure.eventsourcing.redis_store import RedisCheckpointStore as RedisCheckpointStore
from hexcore.infrastructure.eventsourcing.redis_store import RedisEventStore as RedisEventStore
from hexcore.infrastructure.eventsourcing.sqlalchemy_models import EventStoreModel as EventStoreModel
from hexcore.infrastructure.eventsourcing.sqlalchemy_models import ProjectionCheckpointModel as ProjectionCheckpointModel
from hexcore.infrastructure.eventsourcing.sqlalchemy_models import SnapshotModel as SnapshotModel
from hexcore.infrastructure.eventsourcing.sqlalchemy_models import create_eventstore_tables as create_eventstore_tables
from hexcore.infrastructure.eventsourcing.sqlalchemy_models import drop_eventstore_tables as drop_eventstore_tables
from hexcore.infrastructure.eventsourcing.sqlalchemy_models_mixins import EventStoreMixin as EventStoreMixin
from hexcore.infrastructure.eventsourcing.sqlalchemy_models_mixins import ProjectionCheckpointMixin as ProjectionCheckpointMixin
from hexcore.infrastructure.eventsourcing.sqlalchemy_models_mixins import SnapshotMixin as SnapshotMixin
from hexcore.infrastructure.eventsourcing.sqlalchemy_store import SqlAlchemyCheckpointStore as SqlAlchemyCheckpointStore
from hexcore.infrastructure.eventsourcing.sqlalchemy_store import SqlAlchemyEventStore as SqlAlchemyEventStore
from hexcore.infrastructure.eventsourcing.sqlalchemy_store import SqlAlchemySnapshotStore as SqlAlchemySnapshotStore

__all__ = [
    "AbstractCheckpointStore",
    "AbstractEventStore",
    "AbstractProjection",
    "AbstractSnapshotStore",
    "AggregateNotFoundError",
    "AggregateRoot",
    "BeanieCheckpointStore",
    "BeanieEventStore",
    "BeanieSnapshotStore",
    "ConcurrencyError",
    "EXPECTED_VERSION_ANY",
    "EXPECTED_VERSION_NO_STREAM",
    "EventRecorder",
    "EventSourcedRepository",
    "EventSourcingError",
    "EventStoreConfig",
    "EventStoreContainer",
    "EventStoreFactory",
    "EventStoreMixin",
    "EventStoreModel",
    "EventStoreRelay",
    "InMemoryCheckpointStore",
    "InMemoryEventStore",
    "InMemorySnapshotStore",
    "ProjectionCheckpointDocument",
    "ProjectionCheckpointMixin",
    "ProjectionCheckpointModel",
    "ProjectionsConfig",
    "Projector",
    "RedisCheckpointStore",
    "RedisEventStore",
    "Snapshot",
    "SnapshotConfig",
    "SnapshotDocument",
    "SnapshotMixin",
    "SnapshotModel",
    "SqlAlchemyCheckpointStore",
    "SqlAlchemyEventStore",
    "SqlAlchemySnapshotStore",
    "StoredEvent",
    "StoredEventDocument",
    "UnhandledEventError",
    "configure_event_store",
    "create_eventstore_tables",
    "drop_eventstore_tables",
    "get_event_store_container",
    "init_eventstore_documents",
    "provide_checkpoint_store",
    "provide_event_store",
    "provide_projector",
    "provide_snapshot_store",
    "reset_event_store",
    "when",
]
