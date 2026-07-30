"""
hexcore.domain.cqrs — Abstracciones puras del patrón CQRS.
Cero dependencias de infraestructura.

Los alias `I*` siguen disponibles pero están deprecados desde 5.0 y se eliminan en 6.0.
Se resuelven por `__getattr__`, no con un `from … import`: importarlos aquí de forma eager
haría que el aviso se emitiera al importar el paquete, con lo que nadie podría saber
**quién** usa el alias.
"""
from __future__ import annotations

import typing as t

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


# ── Alias de retrocompatibilidad (deprecados desde 5.0, se eliminan en 6.0) ────
# Se resuelven perezosamente: cada acceso emite el DeprecationWarning y delega en el
# módulo que lo declara, que a su vez avisa. Se filtra el aviso duplicado emitiendo sólo
# aquí y devolviendo el nombre canónico directamente.
from hexcore._deprecation import warn_deprecated  # noqa: E402

_DEPRECATED_ALIASES: dict[str, tuple[str, str]] = {
    "ICommandHandler": ("hexcore.domain.cqrs.handlers", "AbstractCommandHandler"),
    "IQueryHandler": ("hexcore.domain.cqrs.handlers", "AbstractQueryHandler"),
    "ICommandBus": ("hexcore.domain.cqrs.buses", "AbstractCommandBus"),
    "IQueryBus": ("hexcore.domain.cqrs.buses", "AbstractQueryBus"),
    "IEventBus": ("hexcore.domain.cqrs.buses", "AbstractEventBus"),
    "IMiddleware": ("hexcore.domain.cqrs.middleware", "AbstractMiddleware"),
    "ISerializer": ("hexcore.domain.cqrs.serializer", "AbstractSerializer"),
}


def __getattr__(name: str) -> t.Any:
    entry = _DEPRECATED_ALIASES.get(name)
    if entry is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module_path, canonical = entry
    warn_deprecated(name, canonical)

    import importlib

    return getattr(importlib.import_module(module_path), canonical)


if t.TYPE_CHECKING:
    from .buses import ICommandBus, IEventBus, IQueryBus
    from .handlers import ICommandHandler, IQueryHandler
    from .middleware import IMiddleware
    from .serializer import ISerializer
