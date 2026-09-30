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
import warnings
from contextvars import ContextVar

from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

__all__ = [
    "REQUEST_ID",
    "PredicateCORSMiddleware",
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


cors_logger = logging.getLogger("hexcore.api.cors")


class PredicateCORSMiddleware(CORSMiddleware):
    """
    `CORSMiddleware` que además acepta los orígenes que un predicado apruebe.

    `allow_origins` es una lista fija, y un subdominio por tenant creado en caliente no se puede
    enumerar de antemano: sin esto hay que redesplegar por cada alta, o abrir `"*"` — que con
    credenciales no es válido. El predicado se consulta **sólo** cuando el origen no está ya en
    la lista, así que declarar los dos no duplica trabajo.

    Dos decisiones que no son obvias:

    * **Falla cerrado.** Un predicado que lanza se trata como «no confiable» y se loguea en
      `hexcore.api.cors`: un chequeo de confianza que explota nunca decide «confío» por default.
      El log importa porque el síntoma —un origen legítimo sin CORS— no apunta a ninguna causa.
    * **`Vary: Origin` siempre.** Con orígenes dinámicos la respuesta depende del `Origin`
      también cuando se rechaza; sin `Vary`, una caché compartida podría servirle a un origen
      aceptado la respuesta que guardó sin `Access-Control-Allow-Origin`. Starlette sólo lo
      agrega cuando el origen es aceptado.
    """

    def __init__(
        self,
        app: t.Any,
        *,
        origin_predicate: t.Callable[[str], bool],
        **kwargs: t.Any,
    ) -> None:
        super().__init__(app, **kwargs)
        self._origin_predicate = origin_predicate

    def is_allowed_origin(self, origin: str) -> bool:
        if super().is_allowed_origin(origin):
            return True
        try:
            return bool(self._origin_predicate(origin))
        except Exception:
            cors_logger.exception(
                "cors_origin_predicate lanzó evaluando el origen %r; se lo trata como no "
                "confiable.",
                origin,
            )
            return False

    async def simple_response(
        self, scope: Scope, receive: Receive, send: Send, request_headers: Headers
    ) -> None:
        async def con_vary(message: Message) -> None:
            if message["type"] == "http.response.start":
                message.setdefault("headers", [])
                headers = MutableHeaders(scope=message)
                if "origin" not in headers.get("vary", "").lower():
                    headers.add_vary_header("Origin")
            await send(message)

        await super().simple_response(scope, receive, con_vary, request_headers)


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

    **Configurá el logging primero.** Esta función instrumenta los handlers que
    **ya existen**; si no hay ninguno, no hay nada que instrumentar y no hace nada::

        logging.basicConfig(level=logging.INFO)          # primero
        install_request_id_logging(fmt="%(asctime)s [%(request_id)s] %(message)s")

    Al revés no falla, pero deja el logging sin configurar y el request-id sin
    aparecer. Por eso el caso "sin handlers" emite un `RuntimeWarning` en vez de
    devolver en silencio: el síntoma sería "no veo el request-id en los logs", que no
    apunta a ninguna causa.

    Args:
        logger: El logger a instrumentar. Por defecto, el root logger.
        fmt: Si se da, se aplica como formato a los handlers del logger. Usá
            ``%(request_id)s`` en él. Si es None, no se toca el formato: el filtro
            deja el atributo disponible para el formato que ya tengas.

    Warns:
        RuntimeWarning: Si el logger de destino no tiene ningún handler.
    """
    target = logger or logging.getLogger()
    log_filter = RequestIDLogFilter()

    # El filtro va en los handlers, no en el logger: un filtro de logger no se aplica
    # a los registros que suben por propagación desde loggers hijos.
    handlers = target.handlers or logging.getLogger().handlers
    if not handlers:
        # Un no-op silencioso en una utilidad de *observabilidad* es especialmente malo:
        # el usuario descubre que no funcionó cuando necesita correlacionar un incidente.
        warnings.warn(
            f"install_request_id_logging: el logger {target.name!r} no tiene handlers, "
            "así que no hay nada que instrumentar y esta llamada no hizo nada. "
            "Configurá el logging primero (p. ej. logging.basicConfig(...)) y llamá "
            "después.",
            RuntimeWarning,
            stacklevel=2,
        )
        return

    for handler in handlers:
        if not any(isinstance(f, RequestIDLogFilter) for f in handler.filters):
            handler.addFilter(log_filter)
        if fmt is not None:
            handler.setFormatter(logging.Formatter(fmt))
