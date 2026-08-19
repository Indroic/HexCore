"""
Comandos y queries de identidad, más sus handlers.

Los comandos son `Command` de HexCore, así que son `frozen` y serializables — o sea que pueden
ir por la cola si algún día hace falta. Los handlers son `AbstractCommandHandler`, así que se
registran en el `HandlerRegistry` como cualquier otro y pasan por el mismo pipeline de
middlewares.

**Ningún comando de auth lleva `@background_command`.** Un sign-in que se encola devuelve al
cliente antes de saber si las credenciales eran válidas, con lo cual no puede responder ni 200
ni 401. Los flujos de identidad son sincrónicos por naturaleza; lo que sí va a background es
lo que **sale** de ellos (mandar el mail de verificación), y eso lo despacha el handler del
consumidor suscribiéndose a los eventos.

Nota sobre los campos: las contraseñas viajan como `str` y no como `SecretStr`. Es deliberado
y tiene un costo: un `model_dump()` de estos comandos las expone. La alternativa —`SecretStr`—
rompe la serialización (`model_dump(mode="json")` emite `"**********"`), y un comando que no
round-trippea no se puede encolar ni loguear como payload. Se elige que sean serializables y se
cubre el riesgo donde está: estos comandos **no** deben loguearse. El middleware de logging del
framework registra el nombre del comando, no su contenido.
"""
from __future__ import annotations

import typing as t
from uuid import UUID

from hexcore.darwin.domain.context import Transport
from hexcore.darwin.domain.value_objects import TokenPair, VerificationPurpose
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.handlers import AbstractCommandHandler
from hexcore.domain.cqrs.queries import Query

if t.TYPE_CHECKING:
    from hexcore.darwin.application.services import IdentityService, SessionService
    from hexcore.darwin.domain.context import AuthContext
    from hexcore.darwin.domain.entities import IdentitySession, User

__all__ = [
    # Comandos
    "SignUp",
    "VerifyEmail",
    "SignIn",
    "RefreshSession",
    "SignOut",
    "SignOutEverywhere",
    "ChangePassword",
    "IssueVerificationCode",
    # Queries
    "AuthenticateToken",
    "ListActiveSessions",
    # Resultados
    "SignUpResult",
    "SignInResult",
    "RefreshResult",
    # Handlers
    "SignUpHandler",
    "VerifyEmailHandler",
    "SignInHandler",
    "RefreshSessionHandler",
    "SignOutHandler",
    "SignOutEverywhereHandler",
    "ChangePasswordHandler",
    "IssueVerificationCodeHandler",
    "AuthenticateTokenHandler",
    "ListActiveSessionsHandler",
    "register_identity_handlers",
]


# ── Comandos ──────────────────────────────────────────────────────────────────
class SignUp(Command):
    """Crea una cuenta con credencial local."""

    email: str
    password: str
    name: str | None = None


class VerifyEmail(Command):
    """Canjea un código de verificación de email."""

    email: str
    code: str


class SignIn(Command):
    """Autentica con credencial local y crea la sesión."""

    email: str
    password: str
    transport: Transport = "cookie"
    ip_address: str | None = None
    user_agent: str | None = None
    scopes: frozenset[str] = frozenset()


class RefreshSession(Command):
    """Rota el refresh token."""

    refresh_token: str
    transport: Transport = "cookie"


class SignOut(Command):
    """Revoca una sesión."""

    session_id: UUID
    reason: str = "sign-out"


class SignOutEverywhere(Command):
    """
    Revoca **todas** las sesiones del usuario.

    `subject_user_id` explícito y no tomado del contexto ambiental: un admin puede cerrar las
    sesiones de otro, y hacer que el comando lo tome implícitamente del actor haría imposible
    expresarlo. Quién puede hacerlo lo decide la autorización, no la forma del comando.
    """

    subject_user_id: UUID
    reason: str = "signed-out-everywhere"


class ChangePassword(Command):
    """Cambia la contraseña. Revoca todas las sesiones como efecto."""

    user_id: UUID
    current_password: str
    new_password: str


class IssueVerificationCode(Command):
    """Emite un código nuevo, invalidando los pendientes del mismo propósito."""

    email: str
    purpose: VerificationPurpose = "email_verification"


# ── Queries ───────────────────────────────────────────────────────────────────
class AuthenticateToken(Query["AuthContext[t.Any]"]):
    """
    Verifica un access token y devuelve el `AuthContext`.

    Es una query y no un comando porque no cambia estado — y porque `AbstractQueryBus.ask` es
    síncrono en proceso por diseño, que es exactamente lo que el camino caliente necesita.
    """

    access_token: str
    transport: Transport = "cookie"


class ListActiveSessions(Query["list[IdentitySession]"]):
    """Las sesiones vivas de un usuario. Para una pantalla de seguridad."""

    subject_user_id: UUID


# ── Resultados ────────────────────────────────────────────────────────────────
class SignUpResult(t.NamedTuple):
    """
    El resultado de un sign-up.

    `verification_code` viene **en claro** porque hay que mandarlo por mail: la fila guarda su
    hash, así que es la única vez que existe. No lo loguees.
    """

    user: "User"
    verification_code: str


class SignInResult(t.NamedTuple):
    user: "User"
    session: "IdentitySession"
    tokens: TokenPair


class RefreshResult(t.NamedTuple):
    session: "IdentitySession"
    tokens: TokenPair


# ── Handlers ──────────────────────────────────────────────────────────────────
class _IdentityHandler:
    """
    Base de los handlers que usan `IdentityService`.

    El servicio se resuelve del contenedor al **construir** el handler, no en cada `handle()`:
    el registry cachea la instancia, así que resolver por llamada sería trabajo repetido. Se
    puede inyectar para test.
    """

    def __init__(self, service: "IdentityService | None" = None) -> None:
        self._service = service

    @property
    def service(self) -> "IdentityService":
        if self._service is None:
            from hexcore.darwin.application.container import get_identity_container

            self._service = get_identity_container().identity_service()
        return self._service


class _SessionHandler:
    """Base de los handlers que usan `SessionService`."""

    def __init__(self, service: "SessionService | None" = None) -> None:
        self._service = service

    @property
    def service(self) -> "SessionService":
        if self._service is None:
            from hexcore.darwin.application.container import get_identity_container

            self._service = get_identity_container().session_service()
        return self._service


class SignUpHandler(_IdentityHandler, AbstractCommandHandler[SignUp, SignUpResult]):
    async def handle(self, command: SignUp) -> SignUpResult:
        usuario, codigo = await self.service.sign_up(
            email=command.email, password=command.password, name=command.name
        )
        return SignUpResult(user=usuario, verification_code=codigo)


class VerifyEmailHandler(_IdentityHandler, AbstractCommandHandler[VerifyEmail, "User"]):
    async def handle(self, command: VerifyEmail) -> "User":
        return await self.service.verify_email(email=command.email, code=command.code)


class SignInHandler(_IdentityHandler, AbstractCommandHandler[SignIn, SignInResult]):
    async def handle(self, command: SignIn) -> SignInResult:
        usuario, sesion, par = await self.service.sign_in(
            email=command.email,
            password=command.password,
            transport=command.transport,
            ip_address=command.ip_address,
            user_agent=command.user_agent,
            scopes=command.scopes,
        )
        return SignInResult(user=usuario, session=sesion, tokens=par)


class RefreshSessionHandler(
    _SessionHandler, AbstractCommandHandler[RefreshSession, RefreshResult]
):
    async def handle(self, command: RefreshSession) -> RefreshResult:
        sesion, par = await self.service.refresh(
            command.refresh_token, transport=command.transport
        )
        return RefreshResult(session=sesion, tokens=par)


class SignOutHandler(_SessionHandler, AbstractCommandHandler[SignOut, None]):
    async def handle(self, command: SignOut) -> None:
        await self.service.revoke(command.session_id, reason=command.reason)


class SignOutEverywhereHandler(
    _SessionHandler, AbstractCommandHandler[SignOutEverywhere, int]
):
    async def handle(self, command: SignOutEverywhere) -> int:
        return await self.service.revoke_all_for(
            command.subject_user_id, reason=command.reason
        )


class ChangePasswordHandler(
    _IdentityHandler, AbstractCommandHandler[ChangePassword, None]
):
    async def handle(self, command: ChangePassword) -> None:
        await self.service.change_password(
            user_id=command.user_id,
            current_password=command.current_password,
            new_password=command.new_password,
        )


class IssueVerificationCodeHandler(
    _IdentityHandler, AbstractCommandHandler[IssueVerificationCode, str]
):
    async def handle(self, command: IssueVerificationCode) -> str:
        return await self.service.issue_verification(
            email=command.email, purpose=command.purpose
        )


class AuthenticateTokenHandler(_SessionHandler):
    """
    Handler de la query de autenticación.

    No hereda de `AbstractQueryHandler[Q, R]` con parámetros por una limitación de la variancia
    del genérico y `AuthContext[t.Any]`; cumple el protocolo estructural (`handle`), que es lo
    que el registry consulta.
    """

    async def handle(self, query: AuthenticateToken) -> "AuthContext[t.Any]":
        return await self.service.authenticate(
            query.access_token, transport=query.transport
        )


class ListActiveSessionsHandler(_SessionHandler):
    async def handle(self, query: ListActiveSessions) -> "list[IdentitySession]":
        # Va contra el repositorio y no contra el servicio: es una lectura sin lógica, y meterle
        # un método al servicio sólo para delegar no agrega nada.
        from hexcore.darwin.application.container import get_identity_container

        repo = get_identity_container().sessions_repository()
        return await repo.list_active_for_user(query.subject_user_id)


# ── Registro ──────────────────────────────────────────────────────────────────
def register_identity_handlers(registry: t.Any) -> t.Any:
    """
    Registra los handlers de identidad en un `HandlerRegistry`. Fluido: devuelve el registry.

    Se registran como **factories** y no como instancias: el registry las invoca en el primer
    `resolve`, así que el contenedor tiene que estar configurado recién entonces y no al
    registrar. Eso permite llamar a esto en import time, que es donde uno quiere tener el
    cableado.

    Uso::

        from hexcore.cqrs import HandlerRegistry
        from hexcore.darwin import configure_identity, register_identity_handlers

        registry = HandlerRegistry()
        register_identity_handlers(registry)
        configure_identity(IdentityConfig())
    """
    comandos: list[tuple[type, t.Callable[[], t.Any]]] = [
        (SignUp, SignUpHandler),
        (VerifyEmail, VerifyEmailHandler),
        (SignIn, SignInHandler),
        (RefreshSession, RefreshSessionHandler),
        (SignOut, SignOutHandler),
        (SignOutEverywhere, SignOutEverywhereHandler),
        (ChangePassword, ChangePasswordHandler),
        (IssueVerificationCode, IssueVerificationCodeHandler),
    ]
    for tipo, handler in comandos:
        registry.register_command_handler(tipo, registry.factory(handler))

    consultas: list[tuple[type, t.Callable[[], t.Any]]] = [
        (AuthenticateToken, AuthenticateTokenHandler),
        (ListActiveSessions, ListActiveSessionsHandler),
    ]
    for tipo, handler in consultas:
        registry.register_query_handler(tipo, registry.factory(handler))

    return registry
