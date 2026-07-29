"""
Middlewares HTTP de serie.

`RequestIDMiddleware` es el ejemplo canónico de código 100 % genérico que toda API
reescribe. Lo que aporta esta versión y no la típica de 28 líneas: el request-id vive
en un `ContextVar` y hay un `logging.Filter` que lo inyecta en cada línea de log. Sin
eso, tener el header no sirve para correlacionar nada.
"""
from __future__ import annotations

import logging
import time
import typing as t
import uuid
from contextvars import ContextVar

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

__all__ = [
    "REQUEST_ID",
    "RequestIDMiddleware",
    "TimingMiddleware",
    "RequestIDLogFilter",
    "get_request_id",
    "install_request_id_logging",
]


REQUEST_ID: ContextVar[str] = ContextVar("hexcore_request_id", default="-")

DEFAULT_REQUEST_ID_HEADER = "X-Request-ID"
DEFAULT_TIMING_HEADER = "X-Response-Time-ms"


def get_request_id() -> str:
    """
    El request-id del request en curso, o ``"-"`` fuera de un request.

    Nunca lanza: se puede llamar desde cualquier sitio (incluido un handler de logging)
    sin comprobar si hay request.
    """
    return REQUEST_ID.get()


class RequestIDMiddleware(BaseHTTPMiddleware):
    """
    Propaga un identificador por request.

    Reusa el header entrante si viene (el balanceador o el gateway suele ponerlo, y
    romper esa cadena es perder la traza); si no, genera un UUID4. Lo publica en el
    `ContextVar` `REQUEST_ID` y lo devuelve en la respuesta.
    """

    def __init__(
        self,
        app: t.Any,
        *,
        header_name: str = DEFAULT_REQUEST_ID_HEADER,
        generator: t.Callable[[], str] | None = None,
    ) -> None:
        super().__init__(app)
        self.header_name = header_name
        self._generator = generator or (lambda: str(uuid.uuid4()))

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        request_id = request.headers.get(self.header_name) or self._generator()
        token = REQUEST_ID.set(request_id)
        # Disponible también en `request.state` para quien no quiera el ContextVar.
        request.state.request_id = request_id
        try:
            response = await call_next(request)
        finally:
            REQUEST_ID.reset(token)
        response.headers[self.header_name] = request_id
        return response


class TimingMiddleware(BaseHTTPMiddleware):
    """Añade el tiempo de proceso del request en milisegundos como header."""

    def __init__(
        self,
        app: t.Any,
        *,
        header_name: str = DEFAULT_TIMING_HEADER,
    ) -> None:
        super().__init__(app)
        self.header_name = header_name

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers[self.header_name] = f"{elapsed_ms:.2f}"
        return response


class RequestIDLogFilter(logging.Filter):
    """
    Inyecta el request-id en cada `LogRecord` como ``record.request_id``.

    Es la mitad del valor de `RequestIDMiddleware`: con esto, un formatter como
    ``"%(asctime)s [%(request_id)s] %(message)s"`` correlaciona todas las líneas de un
    request sin que nadie tenga que pasar el id a mano.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id()
        return True


def install_request_id_logging(
    logger: logging.Logger | None = None,
    *,
    fmt: str | None = None,
) -> None:
    """
    Instala el filtro de request-id en los handlers de un logger.

    Args:
        logger: El logger a instrumentar. Por defecto, el root logger.
        fmt: Si se da, se aplica como formato a los handlers del logger. Usá
            ``%(request_id)s`` en él. Si es None, no se toca el formato: el filtro
            deja el atributo disponible para el formato que ya tengas.
    """
    target = logger or logging.getLogger()
    log_filter = RequestIDLogFilter()

    # El filtro va en los handlers, no en el logger: un filtro de logger no se aplica
    # a los registros que suben por propagación desde loggers hijos.
    handlers = target.handlers or logging.getLogger().handlers
    for handler in handlers:
        if not any(isinstance(f, RequestIDLogFilter) for f in handler.filters):
            handler.addFilter(log_filter)
        if fmt is not None:
            handler.setFormatter(logging.Formatter(fmt))
