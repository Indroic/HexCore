"""
Los dos middlewares HTTP de Darwin: contexto de autenticación y anti-CSRF.

`AuthContextMiddleware` publica el `AuthContext` en el `ContextVar` **y** en
`request.state`, exactamente como `RequestIDMiddleware` hace con el request-id. La doble
publicación no es redundancia: el `ContextVar` es lo que ven los command handlers y los
middlewares de CQRS, que no tienen el `Request` a mano; `request.state` es lo que ven las
dependencias de FastAPI y los endpoints, que sí lo tienen y para los que ir por el
`ContextVar` sería indirección gratuita.

**No autentica ni rechaza.** Si la credencial es inválida o no vino, publica `None` y deja
pasar: la decisión de exigir autenticación es de cada ruta, vía `require_authenticated`. Un
middleware que rechaza obligaría a mantener una lista de rutas públicas en dos lugares —el
middleware y el router— y esa lista se desincroniza siempre.

Requiere el extra `[api]`.
"""
from __future__ import annotations

import logging
import typing as t

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from hexcore.darwin.domain.context import AUTH_CONTEXT
from hexcore.darwin.domain.exceptions import CsrfValidationError, IdentityError

if t.TYPE_CHECKING:
    from hexcore.darwin.application.config import IdentityConfig
    from hexcore.darwin.domain.context import AuthContext

logger = logging.getLogger("hexcore.darwin.api")

__all__ = [
    "CSRF_HEADER",
    "SAFE_METHODS",
    "AuthContextMiddleware",
    "CsrfMiddleware",
    "auth_from_request",
]

#: Header por el que el cliente devuelve el valor anti-CSRF.
CSRF_HEADER = "X-CSRF-Token"

#: Métodos que no cambian estado, y por lo tanto no necesitan chequeo anti-CSRF.
#:
#: `HEAD` y `OPTIONS` van adentro porque un preflight de CORS es `OPTIONS` y exigirle el
#: header lo rompería antes de que el request real llegue a existir.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})


def auth_from_request(request: Request) -> "AuthContext[t.Any] | None":
    """
    El `AuthContext` que el middleware publicó, o `None`.

    Para dependencias y endpoints: es más directo que `current_auth()` cuando ya tenés el
    `Request`, y no depende de que el `ContextVar` siga vigente.
    """
    return getattr(request.state, "darwin_auth", None)


class AuthContextMiddleware(BaseHTTPMiddleware):
    """
    Resuelve la credencial del request y publica el contexto.

    Se registra **antes** de `RequestIDMiddleware` en `create_app`, y el orden importa:
    Starlette ejecuta los middlewares en orden inverso al de registro, así que registrar
    después significa correr *más afuera*. Con auth registrado antes, corre **adentro** de
    request-id — y entonces `get_request_id()` ya devuelve el id real cuando este middleware
    loguea o cuando el sink de auditoría lo lee.

    El `reset` del `ContextVar` va en un `finally`: sin eso, un endpoint que lanza deja el
    contexto colgado para la corutina siguiente que reuse el mismo task, o sea filtrado de
    identidad entre requests.
    """

    def __init__(self, app: t.Any) -> None:
        super().__init__(app)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        contexto = await self._resolver(request)

        request.state.darwin_auth = contexto
        token = AUTH_CONTEXT.set(contexto)
        try:
            return await call_next(request)
        finally:
            AUTH_CONTEXT.reset(token)

    async def _resolver(self, request: Request) -> "AuthContext[t.Any] | None":
        """
        Verifica la credencial, o devuelve `None`.

        **Traga las excepciones de identidad a propósito.** Un token vencido, revocado o mal
        formado deja el request anónimo, y quien exija autenticación va a responder 401 con
        el `WWW-Authenticate` que corresponde. Rechazar acá le daría 401 a las rutas públicas
        de un cliente cuyo token acaba de vencer — incluida la ruta de refresh, que es
        justamente la que tiene que poder atenderlo.
        """
        from hexcore.darwin.application.container import get_identity_container

        try:
            contenedor = get_identity_container()
        except RuntimeError:
            # Darwin no está cableado. Es el caso de una app que prendió `auth_context` y se
            # olvidó de `configure_identity()`: se avisa una vez y se sigue anónimo, porque
            # tumbar cada request no ayuda a encontrarlo más rápido que un log claro.
            _avisar_sin_cablear()
            return None

        from hexcore.darwin.infrastructure.transports import TransportResolver

        resolver = TransportResolver(cookies=contenedor.config.cookies)
        transporte = resolver.resolve(request)
        token = transporte.extract_access(request)
        if token is None:
            return None

        try:
            return await contenedor.session_service().authenticate(
                token, transport=transporte.name
            )
        except IdentityError as exc:
            logger.debug(
                "Credencial rechazada en %s %s: %s",
                request.method,
                request.url.path,
                type(exc).__name__,
            )
            return None


_ya_avisado_sin_cablear = False


def _avisar_sin_cablear() -> None:
    """Avisa una vez por proceso, no una vez por request: el ruido no ayuda a diagnosticar."""
    global _ya_avisado_sin_cablear
    if _ya_avisado_sin_cablear:
        return
    _ya_avisado_sin_cablear = True
    logger.error(
        "AppFeatures.auth_context está prendido pero Darwin no está configurado, así que "
        "todos los requests van a quedar anónimos. Llamá a `configure_identity(...)` al "
        "arrancar la aplicación."
    )


class CsrfMiddleware(BaseHTTPMiddleware):
    """
    Chequeo anti-CSRF para el transporte por cookie.

    **`SameSite` solo no alcanza**, y es la razón de que este middleware exista: `Lax` no
    cubre un atacante alojado en un subdominio del mismo sitio (`evil.example.com` contra
    `app.example.com` son same-site para la cookie), y hay navegadores y clientes viejos
    donde el atributo se ignora. `SameSite` es la primera capa; ésta es la segunda.

    Dos chequeos, y hay que pasar **los dos**:

    1. **Origen.** El `Origin` (o `Referer`, si no vino `Origin`) tiene que estar en
       `trusted_origins`. Es lo que ataja el caso normal, porque el navegador pone `Origin`
       en toda petición que cambia estado y una página atacante no puede falsificarlo.
    2. **Double-submit.** El header `X-CSRF-Token` tiene que coincidir con la cookie de CSRF.
       Ataja el caso donde `Origin` no viene (clientes viejos, algunos proxies) sin dejar el
       hueco abierto.

    El valor de la cookie de CSRF **no es aleatorio**: es un HMAC del `sid`. Si fuera
    aleatorio, un subdominio comprometido podría escribir la cookie de CSRF (que no puede
    llevar el prefijo `__Host-`, porque el cliente tiene que poder leerla) y mandar el mismo
    valor en el header — pasando el double-submit con un valor que eligió él. Derivándolo del
    `sid` con una clave del servidor, un valor que el atacante inventa no verifica.

    Sólo aplica al camino de cookie: un cliente Bearer adjunta el token a propósito en cada
    petición, así que un origen ajeno no puede provocar una petición autenticada.
    """

    def __init__(
        self,
        app: t.Any,
        *,
        exempt_paths: t.Sequence[str] = (),
    ) -> None:
        super().__init__(app)
        self._exempt = tuple(exempt_paths)

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        if self._exento(request):
            return await call_next(request)

        from hexcore.darwin.application.container import get_identity_container

        try:
            contenedor = get_identity_container()
        except RuntimeError:
            _avisar_sin_cablear()
            return await call_next(request)

        config = contenedor.config
        nombre_cookie = config.cookies.name_for("access")

        # Sin cookie de sesión no hay nada que un origen ajeno pueda aprovechar: el request
        # es anónimo o viene por Bearer, y en los dos casos el CSRF no aplica.
        if nombre_cookie not in request.cookies:
            return await call_next(request)

        try:
            self._verificar(request, config)
        except CsrfValidationError as exc:
            # **Se devuelve la respuesta, no se propaga la excepción.** Un middleware de
            # Starlette corre por **fuera** de `ExceptionMiddleware`, así que lo que lanza no
            # pasa por los handlers que `register_exception_handlers` instaló: saldría como un
            # 500 con el traceback en texto plano en vez del 403 con el cuerpo del framework.
            # Se replica la forma de `_build_handler` a mano para que el cliente vea lo mismo
            # que en cualquier otro error de dominio.
            logger.info(
                "CSRF rechazado en %s %s: %s", request.method, request.url.path, exc
            )
            return JSONResponse(
                status_code=403,
                content={"detail": str(exc), "error": type(exc).__name__},
            )

        return await call_next(request)

    def _exento(self, request: Request) -> bool:
        if request.method.upper() in SAFE_METHODS:
            return True
        ruta = request.url.path
        return any(ruta == p or ruta.startswith(p.rstrip("/") + "/") for p in self._exempt)

    def _verificar(self, request: Request, config: "IdentityConfig") -> None:
        origen = self._origen(request)
        if origen is not None:
            if origen not in config.trusted_origins:
                raise CsrfValidationError(
                    f"El origen '{origen}' no está en `trusted_origins`, así que esta "
                    f"petición con cookie de sesión se rechaza."
                )
        elif not config.trusted_origins:
            # Ni `Origin` ni orígenes declarados: no hay forma de decidir, y en la duda se
            # rechaza. Fallar cerrando es el criterio de todo este módulo.
            raise CsrfValidationError(
                "La petición no trae `Origin` ni `Referer` y `trusted_origins` está vacío, "
                "así que no se puede verificar de dónde viene. Declará los orígenes de tu "
                "frontend en `IdentityConfig(trusted_origins=[...])`."
            )

        enviado = request.headers.get(CSRF_HEADER)
        esperado = request.cookies.get(config.cookies.name_for("csrf"))
        if not enviado or not esperado:
            raise CsrfValidationError(
                f"Falta el chequeo anti-CSRF: la petición tiene que mandar el header "
                f"'{CSRF_HEADER}' con el mismo valor que la cookie de CSRF."
            )

        from hexcore.darwin.infrastructure.hashing import compare_hashes

        if not compare_hashes(enviado, esperado):
            raise CsrfValidationError(
                "El valor anti-CSRF del header no coincide con el de la cookie."
            )

    @staticmethod
    def _origen(request: Request) -> str | None:
        """
        El origen de la petición: `Origin`, y si no vino, el origen del `Referer`.

        `Referer` como respaldo y no como equivalente: se puede recortar por política del
        navegador, así que sirve para decidir cuando existe pero su ausencia no prueba nada.
        """
        origen = request.headers.get("Origin")
        if origen:
            return origen

        referer = request.headers.get("Referer")
        if not referer:
            return None
        from urllib.parse import urlsplit

        partes = urlsplit(referer)
        if not partes.scheme or not partes.netloc:
            return None
        return f"{partes.scheme}://{partes.netloc}"
