"""
hexcore.domain.cqrs — Abstracciones puras del patrón CQRS.
Cero dependencias de infraestructura.
"""

from .commands import Command
from .queries import Query
from .handlers import AbstractCommandHandler, AbstractQueryHandler
from .buses import AbstractCommandBus, AbstractQueryBus, AbstractEventBus
from .middleware import AbstractMiddleware, NextHandler
from .serializer import AbstractSerializer
from .exceptions import (
    CQRSError,
    HandlerNotFoundError,
    DuplicateHandlerError,
    DeserializationError,
)

# Aliases de retrocompatibilidad (I* → Abstract*)
from .handlers import ICommandHandler, IQueryHandler
from .buses import ICommandBus, IQueryBus, IEventBus
from .middleware import IMiddleware
from .serializer import ISerializer
from .cron import CronJobDefinition, ICronJobRepository, ILockProvider
from .context import IN_WORKER, is_worker_execution, local_execution, worker_execution
from .resolution import build_fqn, resolve_dotted

__all__ = [
    # Nombres canónicos (Abstract*)
    "Command",
    "Query",
    "AbstractCommandHandler",
    "AbstractQueryHandler",
    "AbstractCommandBus",
    "AbstractQueryBus",
    "AbstractEventBus",
    "AbstractMiddleware",
    "NextHandler",
    "AbstractSerializer",
    # Cron
    "CronJobDefinition",
    "ICronJobRepository",
    "ILockProvider",
    # Contexto de ejecución y resolución de FQN
    "IN_WORKER",
    "is_worker_execution",
    "local_execution",
    "worker_execution",
    "build_fqn",
    "resolve_dotted",
    # Aliases (I*)
    "ICommandHandler",
    "IQueryHandler",
    "ICommandBus",
    "IQueryBus",
    "IEventBus",
    "IMiddleware",
    "ISerializer",
    # Excepciones
    "CQRSError",
    "HandlerNotFoundError",
    "DuplicateHandlerError",
    "DeserializationError",
]
