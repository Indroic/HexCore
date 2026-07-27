"""
Middlewares pre-construidos para el pipeline CQRS.
Cada uno es independiente, configurable y opcionalmente activable.
"""
from __future__ import annotations

import asyncio
import logging
import time
import typing as t

from hexcore.domain.cqrs.middleware import IMiddleware, NextHandler


class LoggingMiddleware(IMiddleware):
    """
    Middleware de logging que registra dispatch, duración y errores.
    """

    def __init__(
        self,
        logger: logging.Logger | None = None,
        *,
        log_level: int = logging.INFO,
    ) -> None:
        self._logger = logger or logging.getLogger("hexcore.cqrs")
        self._log_level = log_level

    async def handle(self, message: t.Any, next_handler: NextHandler) -> t.Any:
        msg_type = type(message).__qualname__
        self._logger.log(self._log_level, "[CQRS] Dispatching: %s", msg_type)
        start = time.perf_counter()
        try:
            result = await next_handler(message)
            elapsed = (time.perf_counter() - start) * 1000
            self._logger.log(
                self._log_level,
                "[CQRS] Completed: %s (%.2fms)",
                msg_type,
                elapsed,
            )
            return result
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            self._logger.error(
                "[CQRS] Failed: %s (%.2fms) — %s: %s",
                msg_type,
                elapsed,
                type(exc).__name__,
                exc,
            )
            raise


class RetryMiddleware(IMiddleware):
    """
    Middleware de reintentos con backoff exponencial.
    Solo reintenta excepciones en ``retryable_exceptions``.
    """

    def __init__(
        self,
        *,
        max_retries: int = 3,
        base_delay: float = 0.1,
        retryable_exceptions: tuple[type[Exception], ...] = (Exception,),
    ) -> None:
        self._max_retries = max_retries
        self._base_delay = base_delay
        self._retryable_exceptions = retryable_exceptions

    async def handle(self, message: t.Any, next_handler: NextHandler) -> t.Any:
        last_exception: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                return await next_handler(message)
            except self._retryable_exceptions as exc:
                last_exception = exc
                if attempt < self._max_retries:
                    delay = self._base_delay * (2**attempt)
                    await asyncio.sleep(delay)
        raise last_exception  # type: ignore[misc]


class ValidationMiddleware(IMiddleware):
    """
    Middleware que valida el mensaje usando Pydantic ``model_validate``
    antes de pasarlo al siguiente handler. Útil para re-validar
    commands que vienen de fuentes no confiables.
    """

    async def handle(self, message: t.Any, next_handler: NextHandler) -> t.Any:
        if hasattr(message, "model_validate"):
            # Re-validar el modelo (detecta datos corruptos post-deserialización)
            validated = type(message).model_validate(message.model_dump())
            return await next_handler(validated)
        return await next_handler(message)


class TransactionMiddleware(IMiddleware):
    """
    Middleware que envuelve la ejecución del handler en un contexto transaccional
    usando el Unit of Work de Hexcore.

    Requiere que el handler tenga acceso al UoW (inyectado vía constructor).
    Este middleware se encarga del commit/rollback.
    """

    def __init__(self, uow_factory: t.Callable[[], t.Any] | None = None) -> None:
        """
        Args:
            uow_factory: Callable que retorna un IUnitOfWork context manager.
                         Si es None, usa un factory por defecto (intenta SQL, luego NoSQL).
        """
        self._uow_factory = uow_factory or self._default_uow_factory

    @staticmethod
    def _default_uow_factory() -> t.Any:
        from hexcore.config import LazyConfig
        from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork, NoSqlUnitOfWork
        
        config = LazyConfig.get_config()
        # Heurística simple: Si hay una URL de SQL (por defecto siempre la hay), asume SQL.
        # En una app real de Hexcore se puede inyectar esto mejor, pero provee un default funcional.
        if config.sql_database_url:
            from hexcore.infrastructure.repositories.orms.sqlalchemy.session import AsyncSessionLocal
            return SqlAlchemyUnitOfWork(session=AsyncSessionLocal())
        return NoSqlUnitOfWork()

    async def handle(self, message: t.Any, next_handler: NextHandler) -> t.Any:
        uow = self._uow_factory()
        async with uow:
            result = await next_handler(message)
            await uow.commit()
            return result
