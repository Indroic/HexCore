# ⚠️  ARCHIVO GENERADO — NO EDITAR A MANO.
#
# Generado por `scripts/gen_stubs.py` desde el `_EXPORTS` de `hexcore/cqrs.py`.
# Si editás esto a mano, el job `stubs-drift` de CI te lo va a revertir.
#
# Para regenerar:
#
#     uv run python scripts/gen_stubs.py --write
#
# Existe porque la fachada resuelve sus exports con `__getattr__` y declara
# `__all__ = sorted(_EXPORTS)`: las dos son expresiones de runtime, así que sin este stub
# los 64 símbolos de `hexcore.cqrs` tipan `Any`. El runtime no cambia — Python usa
# el `.py` y el checker usa el `.pyi`, así que la carga perezosa se mantiene.


from hexcore.application.cqrs.adapters import UseCaseCommandHandler as UseCaseCommandHandler
from hexcore.application.cqrs.config import BusConfig as BusConfig
from hexcore.application.cqrs.config import CQRSConfig as CQRSConfig
from hexcore.application.cqrs.factory import CQRSFactory as CQRSFactory
from hexcore.application.cqrs.in_memory_buses import InMemoryCommandBus as InMemoryCommandBus
from hexcore.application.cqrs.in_memory_buses import InMemoryEventBus as InMemoryEventBus
from hexcore.application.cqrs.in_memory_buses import InMemoryQueryBus as InMemoryQueryBus
from hexcore.application.cqrs.pipeline import MiddlewarePipeline as MiddlewarePipeline
from hexcore.application.cqrs.registry import HandlerFactory as HandlerFactory
from hexcore.application.cqrs.registry import HandlerRegistry as HandlerRegistry
from hexcore.application.cqrs.scheduler import DynamicScheduler as DynamicScheduler
from hexcore.domain.cqrs.buses import AbstractCommandBus as AbstractCommandBus
from hexcore.domain.cqrs.buses import AbstractEventBus as AbstractEventBus
from hexcore.domain.cqrs.buses import AbstractQueryBus as AbstractQueryBus
from hexcore.domain.cqrs.commands import Command as Command
from hexcore.domain.cqrs.context import is_worker_execution as is_worker_execution
from hexcore.domain.cqrs.context import worker_execution as worker_execution
from hexcore.domain.cqrs.cron import CronJobDefinition as CronJobDefinition
from hexcore.domain.cqrs.cron import ICronJobRepository as ICronJobRepository
from hexcore.domain.cqrs.cron import ILockProvider as ILockProvider
from hexcore.domain.cqrs.decorators import background_command as background_command
from hexcore.domain.cqrs.decorators import background_handler as background_handler
from hexcore.domain.cqrs.decorators import background_task as background_task
from hexcore.domain.cqrs.envelope import AbstractEnvelopeRestorer as AbstractEnvelopeRestorer
from hexcore.domain.cqrs.envelope import ENVELOPE_METADATA_KEY as ENVELOPE_METADATA_KEY
from hexcore.domain.cqrs.envelope import EnvelopeMetadataProvider as EnvelopeMetadataProvider
from hexcore.domain.cqrs.envelope import clear_envelope_registry as clear_envelope_registry
from hexcore.domain.cqrs.envelope import collect_envelope_metadata as collect_envelope_metadata
from hexcore.domain.cqrs.envelope import message_correlation_id as message_correlation_id
from hexcore.domain.cqrs.envelope import register_envelope_metadata_provider as register_envelope_metadata_provider
from hexcore.domain.cqrs.envelope import register_envelope_restorer as register_envelope_restorer
from hexcore.domain.cqrs.envelope import registered_envelope_keys as registered_envelope_keys
from hexcore.domain.cqrs.envelope import restored_envelope_scope as restored_envelope_scope
from hexcore.domain.cqrs.envelope import unregister_envelope_key as unregister_envelope_key
from hexcore.domain.cqrs.exceptions import CQRSError as CQRSError
from hexcore.domain.cqrs.exceptions import DeserializationError as DeserializationError
from hexcore.domain.cqrs.exceptions import DuplicateHandlerError as DuplicateHandlerError
from hexcore.domain.cqrs.exceptions import HandlerNotFoundError as HandlerNotFoundError
from hexcore.domain.cqrs.handlers import AbstractCommandHandler as AbstractCommandHandler
from hexcore.domain.cqrs.handlers import AbstractQueryHandler as AbstractQueryHandler
from hexcore.domain.cqrs.middleware import AbstractMiddleware as AbstractMiddleware
from hexcore.domain.cqrs.middleware import NextHandler as NextHandler
from hexcore.domain.cqrs.queries import Query as Query
from hexcore.domain.cqrs.serializer import AbstractSerializer as AbstractSerializer
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer as ITaskEnqueuer
from hexcore.domain.events import DomainEvent as DomainEvent
from hexcore.infrastructure.cqrs.cron_sql import CronJobModel as CronJobModel
from hexcore.infrastructure.cqrs.cron_sql import CronJobModelMixin as CronJobModelMixin
from hexcore.infrastructure.cqrs.cron_sql import SqlAlchemyCronJobRepository as SqlAlchemyCronJobRepository
from hexcore.infrastructure.cqrs.cron_sql import create_cron_tables as create_cron_tables
from hexcore.infrastructure.cqrs.cron_sql import cron_job as cron_job
from hexcore.infrastructure.cqrs.cron_sql import seed_cron_jobs as seed_cron_jobs
from hexcore.infrastructure.cqrs.middlewares import LoggingMiddleware as LoggingMiddleware
from hexcore.infrastructure.cqrs.middlewares import RetryMiddleware as RetryMiddleware
from hexcore.infrastructure.cqrs.middlewares import TransactionMiddleware as TransactionMiddleware
from hexcore.infrastructure.cqrs.middlewares import ValidationMiddleware as ValidationMiddleware
from hexcore.infrastructure.cqrs.postgres_lock import PostgresLockProvider as PostgresLockProvider
from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer as PydanticSerializer
from hexcore.infrastructure.cqrs.redis_lock import RedisLockProvider as RedisLockProvider
from hexcore.infrastructure.workers.consumer import CQRSConsumer as CQRSConsumer
from hexcore.infrastructure.workers.runner import WorkerDied as WorkerDied
from hexcore.infrastructure.workers.runner import run_cqrs_worker as run_cqrs_worker
from hexcore.infrastructure.workers.runner import run_procrastinate_worker as run_procrastinate_worker
from hexcore.infrastructure.workers.runner import worker_loop as worker_loop

__all__ = [
    "AbstractCommandBus",
    "AbstractCommandHandler",
    "AbstractEnvelopeRestorer",
    "AbstractEventBus",
    "AbstractMiddleware",
    "AbstractQueryBus",
    "AbstractQueryHandler",
    "AbstractSerializer",
    "BusConfig",
    "CQRSConfig",
    "CQRSConsumer",
    "CQRSError",
    "CQRSFactory",
    "Command",
    "CronJobDefinition",
    "CronJobModel",
    "CronJobModelMixin",
    "DeserializationError",
    "DomainEvent",
    "DuplicateHandlerError",
    "DynamicScheduler",
    "ENVELOPE_METADATA_KEY",
    "EnvelopeMetadataProvider",
    "HandlerFactory",
    "HandlerNotFoundError",
    "HandlerRegistry",
    "ICronJobRepository",
    "ILockProvider",
    "ITaskEnqueuer",
    "InMemoryCommandBus",
    "InMemoryEventBus",
    "InMemoryQueryBus",
    "LoggingMiddleware",
    "MiddlewarePipeline",
    "NextHandler",
    "PostgresLockProvider",
    "PydanticSerializer",
    "Query",
    "RedisLockProvider",
    "RetryMiddleware",
    "SqlAlchemyCronJobRepository",
    "TransactionMiddleware",
    "UseCaseCommandHandler",
    "ValidationMiddleware",
    "WorkerDied",
    "background_command",
    "background_handler",
    "background_task",
    "clear_envelope_registry",
    "collect_envelope_metadata",
    "create_cron_tables",
    "cron_job",
    "is_worker_execution",
    "message_correlation_id",
    "register_envelope_metadata_provider",
    "register_envelope_restorer",
    "registered_envelope_keys",
    "restored_envelope_scope",
    "run_cqrs_worker",
    "run_procrastinate_worker",
    "seed_cron_jobs",
    "unregister_envelope_key",
    "worker_execution",
    "worker_loop",
]
