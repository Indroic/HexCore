# pyright: reportUnusedFunction=false
#
# En un módulo de router **ninguna** función se llama por nombre: a todas las registra su
# decorador. La regla se apaga acá, a nivel de archivo y con motivo, en vez de repetir doce
# `# pyright: ignore` inline — que serían ruido y esconderían un caso real de función muerta.
"""
El router de identidad: sign-up, verificación, sign-in, refresh, sign-out.

**Un solo endpoint por operación, sirviendo los dos transportes.** El endpoint no ramifica:
resuelve el transporte una vez y le delega la emisión. El cliente web recibe `Set-Cookie` y
**ningún token en el cuerpo**; el cliente nativo recibe los tokens en el cuerpo y **ningún**
`Set-Cookie`. Duplicar las rutas —`/auth/cookie/sign-in` y `/auth/bearer/sign-in`— duplicaría
también los chequeos de seguridad, y la copia que se olvida de uno es la que se explota.

Las rutas van con el `rate_limit` corregido en la Fase 0: `client_ip_key` ya no confía en
`X-Forwarded-For`, y el conteo es atómico. Sin las dos cosas, el límite del login era un
no-op.

Requiere el extra `[api]`.
"""
from __future__ import annotations

import typing as t

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from hexcore.darwin.domain.context import AuthContext
from hexcore.darwin.infrastructure.api.dependencies import (
    provide_auth,
    require_not_impersonated,
)

if t.TYPE_CHECKING:
    from hexcore.darwin.application.config import IdentityConfig
    from hexcore.darwin.domain.value_objects import TokenPair
    from hexcore.darwin.infrastructure.transports import AbstractTransport

__all__ = [
    "SignUpRequest",
    "VerifyEmailRequest",
    "SignInRequest",
    "SessionResponse",
    "MeResponse",
    "SignedOutResponse",
    "RevokedResponse",
    "SessionInfo",
    "build_identity_router",
    "emit_tokens",
    "session_response_body",
    "resolve_transport",
]


# ── DTOs ──────────────────────────────────────────────────────────────────────
class SignUpRequest(BaseModel):
    """
    El cuerpo de `POST /auth/sign-up`.

    `email` es opcional desde 10.0: qué se exige lo decide `IdentityConfig.require_email`, y
    duplicar el requisito acá daría dos lugares donde puede discrepar. Un cuerpo sin ninguno de
    los dos identificadores lo rechaza el servicio con un 400.
    """

    email: str | None = None
    password: str
    name: str | None = None
    username: str | None = None


class VerifyEmailRequest(BaseModel):
    email: str
    code: str


class SignInRequest(BaseModel):
    """
    El cuerpo de `POST /auth/sign-in`.

    Acepta el identificador con **tres nombres**: `identifier`, que es el canónico desde 10.0,
    más `email` y `username`. Los dos viejos no son cortesía: un cliente de 9.x manda
    `{"email": ..., "password": ...}`, y sin ellos ese cuerpo dejaría de validar y todo front
    existente se rompería en el deploy.

    Son **tres campos opcionales más un validador**, y no un `validation_alias` con
    `AliasChoices`, que sería la forma corta. El motivo es concreto: FastAPI reconstruye el
    campo al armar el parámetro de cuerpo, y en esa reconstrucción pydantic descarta el alias y
    avisa con `UnsupportedFieldAttributeWarning` — en cada arranque de cada app que monte el
    router. La validación igual funcionaba, pero un warning que no se puede arreglar desde la
    app del consumidor es ruido permanente. De paso, esta forma deja decir **qué** falta en vez
    de un "field required" sobre un campo que el cliente no sabe que existe.
    """

    model_config = ConfigDict(populate_by_name=True)

    identifier: str | None = None
    email: str | None = None
    username: str | None = None
    password: str

    @model_validator(mode="after")
    def _hay_exactamente_un_identificador(self) -> "SignInRequest":
        presentes = [
            v for v in (self.identifier, self.email, self.username) if v is not None
        ]
        if not presentes:
            raise ValueError(
                "Falta el identificador: mandá 'identifier' (o 'email' / 'username', que son "
                "sus nombres previos) junto con 'password'."
            )
        if len(presentes) > 1:
            raise ValueError(
                "Vinieron varios identificadores a la vez. 'identifier', 'email' y 'username' "
                "son tres nombres del mismo campo: mandá uno solo."
            )
        return self

    @property
    def credential(self) -> str:
        """El identificador, venga con el nombre que venga. El validador garantiza que hay uno."""
        return t.cast(str, self.identifier or self.email or self.username)


class SessionResponse(BaseModel):
    """
    La respuesta de un sign-in o un refresh.

    Los tokens son **opcionales** porque el camino de cookie no los devuelve: van en
    `Set-Cookie` y devolverlos además daría una copia que puede terminar en `localStorage`,
    que es justamente lo que `HttpOnly` evita.
    """

    session_id: str
    expires_in: int
    token_type: str = "Bearer"
    access_token: str | None = None
    refresh_token: str | None = None


class MeResponse(BaseModel):
    """
    Quién sos, y **a nombre de quién** estás actuando.

    `actor_id` y `subject_id` separados incluso acá: un operador que impersona tiene que ver
    en su propia UI que está dentro de la cuenta de otro. Devolver un solo id haría que la
    interfaz no pueda mostrarlo.
    """

    actor_id: str
    subject_id: str
    impersonating: bool = False
    email: str | None = None
    username: str | None = None

    #: El estado de la cuenta según la app. Sale del token, no de la base — y por eso lo
    #: devuelve incluso este endpoint, que sí consulta: es el mismo valor que la app va a leer
    #: de `auth.actor.status` en cualquier otra ruta, sin consultar.
    status: str | None = None

    roles: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)


class SignedOutResponse(BaseModel):
    """La respuesta de `POST /auth/sign-out`."""

    signed_out: bool


class RevokedResponse(BaseModel):
    """La respuesta de `POST /auth/sign-out-everywhere`: cuántas sesiones se revocaron."""

    revoked: int


class SessionInfo(BaseModel):
    """Una fila de `GET /auth/sessions`, para una pantalla de seguridad."""

    session_id: str
    transport: str
    created_at: str
    expires_at: str
    ip_address: str | None
    user_agent: str | None
    impersonated: bool
    current: bool


# ── Helpers de transporte ─────────────────────────────────────────────────────
def resolve_transport(request: Request) -> "AbstractTransport":
    """Dependencia: el transporte de este request, resuelto una sola vez."""
    from hexcore.darwin.application.container import get_identity_container
    from hexcore.darwin.infrastructure.transports import TransportResolver

    contenedor = get_identity_container()
    return TransportResolver(
        cookies=contenedor.config.cookies, tokens=contenedor.config.tokens
    ).resolve(request)


def emit_tokens(
    response: Response,
    tokens: "TokenPair",
    transport: "AbstractTransport",
    config: "IdentityConfig",
) -> None:
    """
    Escribe el par en la respuesta según el transporte, más el valor anti-CSRF si hace falta.

    El valor anti-CSRF sólo se emite en el camino de cookie, y su cookie **no** es `HttpOnly`
    a propósito: el cliente tiene que poder leerla para devolverla en el header
    `X-CSRF-Token`. Que sea legible es lo que obliga a que su valor sea derivado del `sid` y
    no aleatorio — ver `derive_csrf_token`.
    """
    transport.emit(response, tokens)

    if transport.name != "cookie":
        return

    from hexcore.darwin.infrastructure.hashing import derive_csrf_token

    clave = config.secret_key
    if clave is None:  # pragma: no cover - `IdentityConfig` ya lo garantiza
        return

    # `max_age` **de la sesión, no del access token**.
    #
    # Acá estaba con `tokens.expires_in`, que es la vida del access token: 120 s por defecto.
    # Pasado ese rato el navegador descartaba la cookie de CSRF, y el `CsrfMiddleware` —que
    # exige que el header `X-CSRF-Token` coincida con ella— rechazaba con 403 el siguiente
    # `POST`. Incluido `POST /auth/refresh`, o sea que una pestaña inactiva más de dos minutos
    # no podía renovar la sesión y el usuario quedaba afuera sin entender por qué.
    #
    # El valor es un HMAC del `sid`, así que es válido mientras viva la sesión: alinearlo con
    # el refresh es lo coherente, y es el mismo número que la cookie de refresh.
    response.set_cookie(
        config.cookies.name_for("csrf"),
        derive_csrf_token(str(tokens.session_id), clave.get_secret_value()),
        max_age=int(config.tokens.refresh_ttl.total_seconds()),
        httponly=False,
        secure=config.cookies.secure,
        samesite=config.cookies.same_site,
        path=config.cookies.path,
    )


def session_response_body(
    tokens: "TokenPair", transport: "AbstractTransport"
) -> SessionResponse:
    """
    El cuerpo de la respuesta: con tokens sólo si el transporte es Bearer.

    Es público —y no `_cuerpo`— porque los routers de los plugins lo reusan: `magic_link`
    devuelve la misma forma que `/auth/sign-in`. Que cada plugin armara su propio cuerpo
    dejaría que uno filtre los tokens en el cuerpo estando en modo cookie, que es justamente
    la decisión que esta función centraliza.
    """
    if transport.name == "cookie":
        return SessionResponse(
            session_id=str(tokens.session_id), expires_in=tokens.expires_in
        )
    return SessionResponse(
        session_id=str(tokens.session_id),
        expires_in=tokens.expires_in,
        access_token=tokens.access_token,
        refresh_token=tokens.refresh_token,
    )


# ── El router ─────────────────────────────────────────────────────────────────
def build_identity_router(
    *,
    prefix: str = "/auth",
    tags: t.Sequence[str] = ("auth",),
    sign_in_rate_limit: tuple[int, int] | None = (5, 300),
    include_sign_up: bool = True,
) -> APIRouter:
    """
    Construye el router de identidad.

    Args:
        prefix: Prefijo de las rutas.
        tags: Tags de OpenAPI.
        sign_in_rate_limit: `(intentos, ventana_en_segundos)` para sign-in y verificación, o
            `None` para no limitar. El default —5 cada 5 minutos— se aplica **por IP**, y usa
            `on_backend_error="deny"`: si el backend del límite se cae, la ruta de login
            rechaza. Es al revés que el default de `rate_limit`, y es deliberado — un Redis
            caído no debería convertirse en credential stuffing ilimitado.
        include_sign_up: Si se monta `POST /sign-up`. Se puede apagar en una app donde las
            cuentas las crea un administrador.

    Returns:
        Un `APIRouter` para pasarle a `create_app(routers=[...])`.

    ⚠️ **`POST /sign-up` es un oráculo de enumeración si lo dejás público tal cual**: responde
    409 cuando el mail ya existe. Para una ruta pública, la respuesta tiene que ser la misma
    exista o no la cuenta, y la diferencia va en el mail que se manda. Este endpoint sirve el
    caso administrativo; el público conviene escribirlo en la app.

    Uso::

        from hexcore.darwin import build_identity_router

        app = create_app(
            features=AppFeatures(auth_context=True, csrf=True),
            routers=[build_identity_router()],
        )
    """
    router = APIRouter(prefix=prefix, tags=list(tags))
    limite = _rate_limit(sign_in_rate_limit)

    if include_sign_up:

        @router.post("/sign-up", status_code=201, dependencies=limite)
        async def sign_up(payload: SignUpRequest) -> dict[str, str]:
            """
            Crea una cuenta y **devuelve el código de verificación**.

            El código vuelve en el cuerpo porque el framework no manda mails: quién lo manda
            y con qué plantilla es de la aplicación. Suscribite a `UserRegisteredEvent` o usá
            este valor para mandarlo vos.

            **No lo loguees ni lo devuelvas en una ruta pública**: es la credencial que
            verifica el mail.
            """
            from hexcore.darwin.application.container import get_identity_container

            servicio = get_identity_container().identity_service()
            usuario, codigo = await servicio.sign_up(
                email=payload.email,
                password=payload.password,
                name=payload.name,
                username=payload.username,
            )
            cuerpo = {"user_id": str(usuario.id)}
            if codigo is not None:
                cuerpo["verification_code"] = codigo
            return cuerpo

    @router.post("/verify-email", dependencies=limite)
    async def verify_email(payload: VerifyEmailRequest) -> dict[str, bool]:
        from hexcore.darwin.application.container import get_identity_container

        servicio = get_identity_container().identity_service()
        usuario = await servicio.verify_email(email=payload.email, code=payload.code)
        return {"email_verified": usuario.email_verified}

    @router.post("/sign-in", dependencies=limite, response_model=SessionResponse)
    async def sign_in(
        payload: SignInRequest,
        request: Request,
        transport: "AbstractTransport" = Depends(resolve_transport),
    ) -> Response:
        """
        Autentica y abre la sesión. **El mismo endpoint para los dos transportes.**

        Devuelve `JSONResponse` a mano en vez de un modelo, porque hay que escribir cookies
        en la respuesta y para eso hace falta el objeto.
        """
        from hexcore.darwin.application.container import get_identity_container

        contenedor = get_identity_container()
        _, _, tokens = await contenedor.identity_service().sign_in(
            identifier=payload.credential,
            password=payload.password,
            transport=transport.name,
            ip_address=_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )

        respuesta = JSONResponse(
            session_response_body(tokens, transport).model_dump(exclude_none=True)
        )
        emit_tokens(respuesta, tokens, transport, contenedor.config)
        return respuesta

    @router.post("/refresh", response_model=SessionResponse)
    async def refresh(
        request: Request,
        transport: "AbstractTransport" = Depends(resolve_transport),
    ) -> Response:
        """
        Rota la sesión.

        **Sin rate limit**, y no es un olvido: un cliente legítimo refresca cada dos minutos
        —el TTL del access token— así que un límite por IP cortaría a los usuarios detrás de
        un NAT compartido. La protección de este endpoint es la detección de reuso: un refresh
        robado revoca la familia entera en el primer intento.
        """
        from hexcore.darwin.application.container import get_identity_container
        from hexcore.darwin.domain.exceptions import UnauthenticatedError

        token = transport.extract_refresh(request)
        if token is None:
            raise UnauthenticatedError(
                "No vino el token de refresco. Por cookie va en la cookie de refresh; por "
                "Bearer, en el header 'X-Refresh-Token'."
            )

        contenedor = get_identity_container()
        _, tokens = await contenedor.session_service().refresh(
            token, transport=transport.name
        )

        respuesta = JSONResponse(
            session_response_body(tokens, transport).model_dump(exclude_none=True)
        )
        emit_tokens(respuesta, tokens, transport, contenedor.config)
        return respuesta

    @router.post("/sign-out", response_model=SignedOutResponse)
    async def sign_out(
        auth: "AuthContext[t.Any]" = Depends(provide_auth),
        transport: "AbstractTransport" = Depends(resolve_transport),
    ) -> Response:
        """
        Cierra la sesión en curso: revoca la fila **y** borra las cookies.

        Las dos cosas: borrar la cookie sin revocar deja el token vivo para quien lo haya
        copiado, y revocar sin borrar la cookie le deja al navegador una credencial muerta
        que va a mandar en cada petición.
        """
        from hexcore.darwin.application.container import get_identity_container

        from hexcore.darwin.domain.context import Principal

        # `isinstance` y no `hasattr`: un `SystemPrincipal` no tiene sesión que revocar, y
        # el chequeo estructural dejaría el tipo en `Unknown` para el type checker.
        actor = auth.actor
        if isinstance(actor, Principal) and actor.session_id is not None:
            await get_identity_container().session_service().revoke(
                actor.session_id, reason="sign-out"
            )

        respuesta = JSONResponse({"signed_out": True})
        transport.clear(respuesta)
        return respuesta

    @router.post(
        "/sign-out-everywhere",
        dependencies=[Depends(require_not_impersonated("sign_out_everywhere"))],
        response_model=RevokedResponse,
    )
    async def sign_out_everywhere(
        auth: "AuthContext[t.Any]" = Depends(provide_auth),
        transport: "AbstractTransport" = Depends(resolve_transport),
    ) -> Response:
        """
        Revoca **todas** las sesiones del sujeto.

        No se permite impersonado: es el flujo que la víctima ejecuta para echar a un
        atacante, y dejar que un operador lo dispare desde una sesión impersonada le daría la
        capacidad de expulsar al dueño de su propia cuenta.
        """
        from hexcore.darwin.application.container import get_identity_container

        subject = auth.subject_id
        revocadas = await get_identity_container().session_service().revoke_all_for(
            t.cast(t.Any, subject), reason="signed-out-everywhere"
        )

        respuesta = JSONResponse({"revoked": revocadas})
        transport.clear(respuesta)
        return respuesta

    @router.get("/me", response_model=MeResponse)
    async def me(auth: "AuthContext[t.Any]" = Depends(provide_auth)) -> MeResponse:
        """
        Quién sos. **Lee la fila del usuario**, y es la excepción deliberada al "cero DB en
        el camino caliente".

        El mail no viaja en el token a propósito: es PII, y un access token que el cliente
        guarda —en `localStorage`, en el keychain, en un log de proxy— no es el lugar para
        ponerla. La regla de no tocar la base vale para **cada petición autenticada**, no
        para el endpoint cuyo propósito es justamente devolver los datos del usuario.
        """
        from hexcore.darwin.application.container import get_identity_container

        email = None
        username = None
        actor_id = auth.actor_id
        if not isinstance(actor_id, str):
            usuario = await get_identity_container().users().get_by_id(actor_id)
            if usuario is not None:
                email = usuario.email
                username = usuario.username

        return MeResponse(
            actor_id=str(actor_id),
            subject_id=str(auth.subject_id),
            impersonating=auth.is_impersonating,
            email=email,
            username=username,
            status=getattr(auth.actor, "status", None),
            roles=sorted(getattr(auth.actor, "roles", frozenset())),
            scopes=sorted(auth.actor.scopes),
        )

    @router.get("/sessions", response_model=list[SessionInfo])
    async def sessions(
        auth: "AuthContext[t.Any]" = Depends(provide_auth),
    ) -> list[SessionInfo]:
        """
        Las sesiones vivas del sujeto, para una pantalla de seguridad.

        **No devuelve el `token_hash`.** Es un hash, no el token, pero publicar el hash de
        una credencial en una API no aporta nada y sí le da a un atacante con acceso de
        lectura el índice por el que buscar.
        """
        from hexcore.darwin.application.container import get_identity_container

        repo = get_identity_container().sessions_repository()
        vivas = await repo.list_active_for_user(t.cast(t.Any, auth.subject_id))
        return [
            SessionInfo(
                session_id=str(s.id),
                transport=s.transport,
                created_at=s.created_at.isoformat(),
                expires_at=s.expires_at.isoformat(),
                ip_address=s.ip_address,
                user_agent=s.user_agent,
                impersonated=s.is_impersonated,
                current=s.id == getattr(auth.actor, "session_id", None),
            )
            for s in vivas
        ]

    return router


def _rate_limit(spec: tuple[int, int] | None) -> list[t.Any]:
    """
    Arma la dependencia de rate limit, o una lista vacía.

    `on_backend_error="deny"` a propósito, al revés que el default de `rate_limit`: en una
    ruta de login, un backend caído que deja pasar es credential stuffing ilimitado.
    """
    if spec is None:
        return []

    from hexcore.infrastructure.api.rate_limit import client_ip_key, rate_limit

    intentos, ventana = spec
    return [
        Depends(
            rate_limit(
                intentos,
                ventana,
                key=client_ip_key,
                on_backend_error="deny",
                namespace="hexcore:darwin:auth",
            )
        )
    ]


def _ip(request: Request) -> str | None:
    """
    La IP del par, para el registro de la sesión.

    Deliberadamente **no** mira `X-Forwarded-For`: el header lo escribe el cliente, y guardar
    un valor que él eligió en la fila de sesión llenaría de basura la pantalla de "tus
    dispositivos". Una app detrás de un proxy que quiera la IP real la resuelve con
    `forwarded_ip_key` y la pasa por su cuenta.
    """
    cliente = request.client
    return cliente.host if cliente else None
