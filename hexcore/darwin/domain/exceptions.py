"""
Excepciones del subsistema de identidad.

Dos criterios que valen para toda la jerarquía:

**No filtrar por qué falló una autenticación.** `InvalidCredentialsError` se usa igual para
"el mail no existe" que para "la contraseña está mal". Distinguirlas le regala al atacante
un oráculo de enumeración de usuarios: con mensajes distintos puede averiguar qué mails
están registrados sin adivinar una sola contraseña. El mensaje es deliberadamente el mismo.

**El mapa de status no vive acá y no toca `DEFAULT_EXCEPTION_STATUS_MAP`.** Se expone como
`IDENTITY_EXCEPTION_STATUS_MAP` y lo mergea `create_app`. Si se agregara al mapa por
defecto, la capa `api` tendría que importar este módulo en tiempo de import y se rompería el
contrato de dependencias opcionales: `hexcore.infrastructure.api` importa `fastapi`, y este
módulo tiene que poder importarse sin ningún extra.
"""
from __future__ import annotations

import typing as t

__all__ = [
    "IdentityError",
    # ── Autenticación (401) ──
    "AuthenticationError",
    "UnauthenticatedError",
    "InvalidCredentialsError",
    "TokenError",
    "TokenMalformedError",
    "TokenExpiredError",
    "TokenRevokedError",
    "TokenAudienceMismatchError",
    # ── Autorización (403) ──
    "AuthorizationError",
    "InsufficientScopeError",
    "EmailNotVerifiedError",
    "ImpersonationNotPermittedError",
    "CsrfValidationError",
    # ── Estado (409 / 423) ──
    "EmailAlreadyRegisteredError",
    "AccountLockedError",
    # ── Integridad (500) ──
    "WorkerContextIntegrityError",
    "IDENTITY_EXCEPTION_STATUS_MAP",
]


class IdentityError(Exception):
    """
    Raíz de todas las excepciones de identidad.

    **A propósito no está en `IDENTITY_EXCEPTION_STATUS_MAP`.** Los handlers de HexCore se
    registran ordenados por profundidad de MRO (`_specificity`), así que mapear la base a
    un 4xx haría que una excepción nueva sin mapear se tragara con ese código en vez de
    salir como 500. Dejándola afuera, olvidarse de mapear algo se nota en los tests.
    """


# ── Autenticación: no se sabe quién sos, o no se puede probar ─────────────────
class AuthenticationError(IdentityError):
    """No se pudo establecer la identidad del solicitante."""


class UnauthenticatedError(AuthenticationError):
    """No vino ninguna credencial. Distinto de que la credencial sea inválida."""

    def __init__(self, message: str = "Se requiere autenticación.") -> None:
        super().__init__(message)


class InvalidCredentialsError(AuthenticationError):
    """
    Las credenciales no sirven.

    Un solo mensaje para "no existe el usuario" y "la contraseña está mal". No es pereza:
    si difirieran, el atacante enumera usuarios registrados sin adivinar una contraseña.
    Por el mismo motivo el flujo de sign-in tiene que hashear una contraseña señuelo
    cuando no encuentra la fila, para que los tiempos tampoco delaten.
    """

    def __init__(self, message: str = "Las credenciales son inválidas.") -> None:
        super().__init__(message)


class TokenError(AuthenticationError):
    """Problemas con un token presentado."""


class TokenMalformedError(TokenError):
    """El token no se puede decodificar, o su firma no verifica."""


class TokenExpiredError(TokenError):
    """El token venció. Cliente bien hecho: refrescá y reintentá."""


class TokenRevokedError(TokenError):
    """
    El token está criptográficamente bien pero la sesión ya no vale.

    Es la excepción que justifica el híbrido JWT + base: sin revocación consultable, un
    token firmado y todavía vigente sería válido aunque el usuario haya cerrado sesión o
    le hayan cambiado la contraseña.
    """


class TokenAudienceMismatchError(TokenError):
    """
    El token llegó por un transporte que no es el suyo.

    Un token emitido para cookie, presentado como `Authorization: Bearer`, esquivaría
    `SameSite` y el chequeo de CSRF por completo. El `aud` ata el token a su transporte y
    esta excepción es lo que pasa cuando no coincide.
    """


# ── Autorización: sabemos quién sos, y no te alcanza ──────────────────────────
class AuthorizationError(IdentityError):
    """La identidad está establecida pero no habilita la operación."""


class InsufficientScopeError(AuthorizationError):
    """Falta un permiso o un rol."""

    def __init__(self, required: t.Iterable[str] = (), message: str | None = None) -> None:
        self.required = frozenset(required)
        if message is None:
            faltantes = ", ".join(sorted(self.required)) or "no declarados"
            message = f"Permisos insuficientes. Requeridos: {faltantes}."
        super().__init__(message)


class EmailNotVerifiedError(AuthorizationError):
    """La cuenta existe y la contraseña es correcta, pero el mail no está verificado."""


class ImpersonationNotPermittedError(AuthorizationError):
    """
    El actor no puede impersonar, o no a este sujeto.

    También se usa cuando una sesión impersonada intenta algo que sólo el dueño real puede
    hacer: cambiar la contraseña, refrescar la sesión, o impersonar a un tercero
    (impersonación en cadena, que rompería la cadena de custodia de la auditoría).
    """


class CsrfValidationError(AuthorizationError):
    """
    Falló el chequeo anti-CSRF del transporte por cookie.

    Sólo aplica al camino de cookie: un cliente Bearer manda el token a propósito en cada
    petición, así que no hay nada que falsificar desde otro origen.
    """


# ── Estado del recurso ────────────────────────────────────────────────────────
class EmailAlreadyRegisteredError(IdentityError):
    """
    Ese mail ya tiene cuenta.

    Ojo: en el flujo de **registro** esto también es un oráculo de enumeración. La ruta
    pública de sign-up debería responder igual exista o no la cuenta y mandar un mail
    distinto según el caso. Esta excepción es para los flujos administrativos, donde el
    solicitante ya está autenticado y autorizado.
    """


class AccountLockedError(IdentityError):
    """
    La cuenta está bloqueada, por intentos fallidos o por decisión administrativa.

    Se mapea a 423 (Locked) y no a 403: es un estado temporal del recurso, no una decisión
    sobre los permisos de quien pregunta.
    """


# ── Integridad interna ───────────────────────────────────────────────────────
class WorkerContextIntegrityError(IdentityError):
    """
    El sobre de autenticación que cruzó la cola no verifica.

    Nunca es culpa del usuario: significa que la firma no valida, que el sobre venció, o
    que el grant venía atado a otro mensaje (un grant de "borrar cuenta" re-adjuntado a un
    "transferir fondos"). Se mapea a 500 y se loguea como crítico, porque o hay un bug de
    cableado o alguien está escribiendo en el broker.
    """


# ── Mapa de status ───────────────────────────────────────────────────────────
#: Se **mergea** en `create_app`, no se agrega a `DEFAULT_EXCEPTION_STATUS_MAP`.
#:
#: Nótese que `IdentityError` no está: ver su docstring. Y que las de autenticación son
#: todas 401 mientras las de autorización son 403 — la diferencia es "no sé quién sos" vs
#: "sé quién sos y no te alcanza", que es exactamente la semántica de esos dos códigos.
IDENTITY_EXCEPTION_STATUS_MAP: dict[type[Exception], int] = {
    UnauthenticatedError: 401,
    InvalidCredentialsError: 401,
    TokenMalformedError: 401,
    TokenExpiredError: 401,
    TokenRevokedError: 401,
    TokenAudienceMismatchError: 401,
    InsufficientScopeError: 403,
    EmailNotVerifiedError: 403,
    ImpersonationNotPermittedError: 403,
    CsrfValidationError: 403,
    EmailAlreadyRegisteredError: 409,
    AccountLockedError: 423,
    WorkerContextIntegrityError: 500,
}
