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
    Middleware de reintentos con backoff exponencial, **in-process**.
    Solo reintenta excepciones en ``retryable_exceptions``.

    **Cuidado al combinarlo con Smart Routing.** Los reintentos se multiplican: si la cola
    reintenta el job 3 veces y este middleware reintenta 3 veces dentro de cada intento,
    el handler corre hasta 12 veces, no 6. Con un handler no idempotente eso son 12
    cobros, no 12 logs.

    Elegí uno de los dos:

    - **El de la cola** (recomendado para `@background_command`): la cola persiste el
      intento, sobrevive a un reinicio del worker y su backoff se ve en el panel.
      Instanciá el bus sin este middleware.
    - **Éste** para comandos síncronos, donde no hay cola que reintente y el usuario está
      esperando la respuesta.

    Si lo declarás y el mensaje además va a background, el middleware avisa una vez por
    tipo de comando con un `warning`.
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
        self._warned: set[str] = set()
        self._logger = logging.getLogger("hexcore.cqrs.retry")

    async def handle(self, message: t.Any, next_handler: NextHandler) -> t.Any:
        self._warn_if_also_retried_by_the_queue(message)

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

    def _warn_if_also_retried_by_the_queue(self, message: t.Any) -> None:
        """
        Avisa una vez por tipo si el mensaje también lo reintenta la cola.

        Una vez por tipo y no por mensaje: en un worker con carga, un warning por job
        llena el log y deja de leerse.
        """
        message_type = type(message)
        if not getattr(message_type, "__cqrs_background__", False):
            return

        name = message_type.__qualname__
        if name in self._warned:
            return
        self._warned.add(name)
        self._logger.warning(
            "'%s' está decorado con @background_command y además pasa por "
            "RetryMiddleware: los reintentos se multiplican (%d in-process x los de la "
            "cola). Si el handler no es idempotente, quitá uno de los dos.",
            name,
            self._max_retries,
        )


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
    usando el Unit of Work de Hexcore, y comitea al terminar.

    **Es para handlers que NO gestionan su propia transacción.** Si tu handler ya
    hace ``async with self.uow:`` y ``await self.uow.commit()`` —el patrón que
    enseña `DOCS.md` para los use cases— no uses este middleware: comitearías dos
    veces.

    ``uow_factory`` es obligatorio. Antes existía un default que armaba la sesión
    con el session factory *interno* de HexCore en vez del engine de la aplicación,
    lo que producía transacciones contra un engine distinto al del resto del
    request. Adivinar aquí es peor que no funcionar.

    Uso::

        TransactionMiddleware(uow_factory=lambda: SqlAlchemyUnitOfWork(session=my_factory()))
    """

    def __init__(self, uow_factory: t.Callable[[], t.Any] | None = None) -> None:
        """
        Args:
            uow_factory: Callable que retorna un IUnitOfWork usable como context
                manager. Obligatorio.

        Raises:
            ValueError: Si no se provee `uow_factory`.
        """
        if uow_factory is None:
            raise ValueError(
                "TransactionMiddleware requiere un 'uow_factory' explícito. "
                "Pasá un callable que construya el UoW con el engine de tu "
                "aplicación, p. ej. "
                "TransactionMiddleware(uow_factory=lambda: SqlAlchemyUnitOfWork(session=session_factory())). "
                "Recordá que este middleware comitea: no lo uses con handlers que "
                "ya gestionan su propia transacción."
            )
        self._uow_factory = uow_factory

    async def handle(self, message: t.Any, next_handler: NextHandler) -> t.Any:
        uow = self._uow_factory()
        async with uow:
            result = await next_handler(message)
            await uow.commit()
            return result
