"""
hexcore.infrastructure.cqrs — Adaptadores de infraestructura para CQRS.
Serializers, Middlewares concretos y backends de bus asíncronos.
"""

from .pydantic_serializer import PydanticSerializer
from .middlewares import (
    LoggingMiddleware,
    RetryMiddleware,
    ValidationMiddleware,
    TransactionMiddleware,
)

__all__ = [
    "PydanticSerializer",
    "LoggingMiddleware",
    "RetryMiddleware",
    "ValidationMiddleware",
    "TransactionMiddleware",
]

# Los adaptadores con dependencias opcionales no se exportan aquí intencionalmente:
# importarlos aquí haría fallar el import de este paquete cuando la dependencia no está
# instalada. Importalos por su ruta:
#
#   from hexcore.infrastructure.cqrs.procrastinate import ProcrastinateCommandBus   # [procrastinate]
#   from hexcore.infrastructure.cqrs.cron_sql import SqlAlchemyCronJobRepository    # [sql]
#   from hexcore.infrastructure.cqrs.redis_lock import RedisLockProvider            # [redis]
#   from hexcore.infrastructure.cqrs.postgres_lock import PostgresLockProvider      # [sql]
