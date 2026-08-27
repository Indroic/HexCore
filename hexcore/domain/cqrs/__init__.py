"""
hexcore.domain.cqrs — Abstracciones puras del patrón CQRS.
Cero dependencias de infraestructura.

Los alias `I*` (`ICommandBus`, `ISerializer`, …) **se eliminaron en 7.0**: estaban deprecados
desde 5.0, o sea dos majors de aviso. Los nombres canónicos son los `Abstract*`. La tabla de
reemplazos está en el README, y `tests/test_deprecations.py` fija que los viejos ya no
resuelven.
"""
from __future__ import annotations

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
    # Excepciones
    "CQRSError",
    "HandlerNotFoundError",
    "DuplicateHandlerError",
    "DeserializationError",
]
