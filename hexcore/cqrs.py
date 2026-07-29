"""
Fachada del subsistema CQRS: un import obvio por tarea.

Montar CQRS obligaba a importar de `hexcore.domain.cqrs.commands`,
`hexcore.domain.cqrs.buses`, `hexcore.application.cqrs.registry`,
`hexcore.application.cqrs.in_memory_buses`, `hexcore.infrastructure.cqrs.pydantic_serializer`,
`hexcore.infrastructure.workers.consumer`… Esto reexporta lo público::

    import hexcore.cqrs as cqrs

    registry = cqrs.HandlerRegistry()
    registry.register_command_handler(CrearTicket, CrearTicketHandler())
    bus = cqrs.InMemoryCommandBus(registry=registry)

Las rutas largas siguen funcionando: esto **no mueve nada de sitio**.

Los nombres canónicos son los `Abstract*`. Los alias `I*` existen por
retrocompatibilidad y no se exponen aquí, para que haya un solo nombre por concepto en la
documentación y en los ejemplos.

La resolución es perezosa (`__getattr__` de módulo) para que importar esta fachada no
arrastre las dependencias opcionales: `hexcore.cqrs.SqlAlchemyCronJobRepository` sólo
requiere el extra `[sql]` en el momento en que lo pedís.
"""
from __future__ import annotations

import typing as t

# name -> (módulo, atributo)
_EXPORTS: dict[str, tuple[str, str]] = {
    # ── Mensajes ──────────────────────────────────────────────────────────────
    "Command": ("hexcore.domain.cqrs.commands", "Command"),
    "Query": ("hexcore.domain.cqrs.queries", "Query"),
    "DomainEvent": ("hexcore.domain.events", "DomainEvent"),
    # ── Contratos (nombres canónicos) ─────────────────────────────────────────
    "AbstractCommandHandler": ("hexcore.domain.cqrs.handlers", "AbstractCommandHandler"),
    "AbstractQueryHandler": ("hexcore.domain.cqrs.handlers", "AbstractQueryHandler"),
    "AbstractCommandBus": ("hexcore.domain.cqrs.buses", "AbstractCommandBus"),
    "AbstractQueryBus": ("hexcore.domain.cqrs.buses", "AbstractQueryBus"),
    "AbstractEventBus": ("hexcore.domain.cqrs.buses", "AbstractEventBus"),
    "AbstractMiddleware": ("hexcore.domain.cqrs.middleware", "AbstractMiddleware"),
    "AbstractSerializer": ("hexcore.domain.cqrs.serializer", "AbstractSerializer"),
    "NextHandler": ("hexcore.domain.cqrs.middleware", "NextHandler"),
    "ITaskEnqueuer": ("hexcore.domain.cqrs.task_queues", "ITaskEnqueuer"),
    # ── Decoradores (Smart Routing) ───────────────────────────────────────────
    "background_command": ("hexcore.domain.cqrs.decorators", "background_command"),
    "background_handler": ("hexcore.domain.cqrs.decorators", "background_handler"),
    "background_task": ("hexcore.domain.cqrs.decorators", "background_task"),
    # ── Registry, pipeline y buses ────────────────────────────────────────────
    "HandlerRegistry": ("hexcore.application.cqrs.registry", "HandlerRegistry"),
    "HandlerFactory": ("hexcore.application.cqrs.registry", "HandlerFactory"),
    "MiddlewarePipeline": ("hexcore.application.cqrs.pipeline", "MiddlewarePipeline"),
    "InMemoryCommandBus": (
        "hexcore.application.cqrs.in_memory_buses",
        "InMemoryCommandBus",
    ),
    "InMemoryQueryBus": ("hexcore.application.cqrs.in_memory_buses", "InMemoryQueryBus"),
    "InMemoryEventBus": ("hexcore.application.cqrs.in_memory_buses", "InMemoryEventBus"),
    # ── Configuración y factory ───────────────────────────────────────────────
    "CQRSConfig": ("hexcore.application.cqrs.config", "CQRSConfig"),
    "BusConfig": ("hexcore.application.cqrs.config", "BusConfig"),
    "CQRSFactory": ("hexcore.application.cqrs.factory", "CQRSFactory"),
    "UseCaseCommandHandler": (
        "hexcore.application.cqrs.adapters",
        "UseCaseCommandHandler",
    ),
    # ── Serializer y middlewares ──────────────────────────────────────────────
    "PydanticSerializer": (
        "hexcore.infrastructure.cqrs.pydantic_serializer",
        "PydanticSerializer",
    ),
    "LoggingMiddleware": ("hexcore.infrastructure.cqrs.middlewares", "LoggingMiddleware"),
    "RetryMiddleware": ("hexcore.infrastructure.cqrs.middlewares", "RetryMiddleware"),
    "ValidationMiddleware": (
        "hexcore.infrastructure.cqrs.middlewares",
        "ValidationMiddleware",
    ),
    "TransactionMiddleware": (
        "hexcore.infrastructure.cqrs.middlewares",
        "TransactionMiddleware",
    ),
    # ── Worker ────────────────────────────────────────────────────────────────
    "CQRSConsumer": ("hexcore.infrastructure.workers.consumer", "CQRSConsumer"),
    "run_cqrs_worker": ("hexcore.infrastructure.workers.runner", "run_cqrs_worker"),
    "run_procrastinate_worker": (
        "hexcore.infrastructure.workers.runner",
        "run_procrastinate_worker",
    ),
    "worker_loop": ("hexcore.infrastructure.workers.runner", "worker_loop"),
    "WorkerDied": ("hexcore.infrastructure.workers.runner", "WorkerDied"),
    "is_worker_execution": ("hexcore.domain.cqrs.context", "is_worker_execution"),
    "worker_execution": ("hexcore.domain.cqrs.context", "worker_execution"),
    # ── Cron ──────────────────────────────────────────────────────────────────
    "CronJobDefinition": ("hexcore.domain.cqrs.cron", "CronJobDefinition"),
    "ICronJobRepository": ("hexcore.domain.cqrs.cron", "ICronJobRepository"),
    "ILockProvider": ("hexcore.domain.cqrs.cron", "ILockProvider"),
    "DynamicScheduler": ("hexcore.application.cqrs.scheduler", "DynamicScheduler"),
    # Requieren extras: se resuelven sólo al pedirlos.
    "CronJobModel": ("hexcore.infrastructure.cqrs.cron_sql", "CronJobModel"),
    "CronJobModelMixin": ("hexcore.infrastructure.cqrs.cron_sql", "CronJobModelMixin"),
    "SqlAlchemyCronJobRepository": (
        "hexcore.infrastructure.cqrs.cron_sql",
        "SqlAlchemyCronJobRepository",
    ),
    "seed_cron_jobs": ("hexcore.infrastructure.cqrs.cron_sql", "seed_cron_jobs"),
    "create_cron_tables": ("hexcore.infrastructure.cqrs.cron_sql", "create_cron_tables"),
    "cron_job": ("hexcore.infrastructure.cqrs.cron_sql", "cron_job"),
    "RedisLockProvider": ("hexcore.infrastructure.cqrs.redis_lock", "RedisLockProvider"),
    "PostgresLockProvider": (
        "hexcore.infrastructure.cqrs.postgres_lock",
        "PostgresLockProvider",
    ),
    # ── Excepciones ───────────────────────────────────────────────────────────
    "CQRSError": ("hexcore.domain.cqrs.exceptions", "CQRSError"),
    "HandlerNotFoundError": (
        "hexcore.domain.cqrs.exceptions",
        "HandlerNotFoundError",
    ),
    "DuplicateHandlerError": (
        "hexcore.domain.cqrs.exceptions",
        "DuplicateHandlerError",
    ),
    "DeserializationError": ("hexcore.domain.cqrs.exceptions", "DeserializationError"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> t.Any:
    try:
        module_path, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module 'hexcore.cqrs' has no attribute {name!r}") from None

    import importlib

    value = getattr(importlib.import_module(module_path), attribute)
    # Se cachea en el módulo para que el segundo acceso no vuelva a importar.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return __all__
