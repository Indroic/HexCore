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

# ProcrastinateCommandBus no se exporta aquí intencionalmente.
# Importarlo directamente: from hexcore.infrastructure.cqrs.procrastinate import ProcrastinateCommandBus
# Esto evita ImportError si procrastinate no está instalado.
