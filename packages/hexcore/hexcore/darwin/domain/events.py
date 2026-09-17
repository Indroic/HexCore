"""
Eventos de dominio de Darwin.

Dos restricciones del bus de eventos de HexCore condicionaron la forma de este módulo. **Las
dos se arreglaron en 9.0**, y quedan escritas porque explican por qué el módulo es como es:

**1. El despacho era por clase exacta.** `InMemoryEventBus.publish` hacía
``self._handlers.get(type(event), [])``: no recorría el MRO, así que suscribirse a una clase
base **no recibía nada**, sin error. Por eso acá no hay un `AuthEvent` base con hijos: hay
hojas concretas, y cada una se suscribe por su nombre. Desde 9.0 los buses despachan por
jerarquía (`hexcore.domain.cqrs.dispatch.handlers_for`), así que una base sería viable —
agregarla ahora sería un cambio de API de Darwin, no una corrección, y por eso no se hizo en
el mismo movimiento.

**2. `event_name` usaba `.replace("Event", "")`, no `removesuffix`.** ``EventLogCreatedEvent``
salía como ``"LOGCREATED"``: "Event" en el medio del nombre se perdía. Corregido en 9.0. Los
nombres de acá llevan "Event" **sólo como sufijo**, así que ninguno cambió de valor — que es
justamente por qué la convención existía.

Todos los eventos llevan **actor y sujeto**, no un solo `user_id`. Es lo que hace que la
auditoría siga siendo cierta bajo impersonación: sin el actor, la acción queda atribuida a la
víctima.
"""
from __future__ import annotations

import typing as t
from uuid import UUID

from hexcore.domain.events import DomainEvent

__all__ = [
    "UserRegisteredEvent",
    "UserEmailVerifiedEvent",
    "UserPasswordChangedEvent",
    "UserSignedInEvent",
    "UserSignInFailedEvent",
    "SessionCreatedEvent",
    "SessionRefreshedEvent",
    "SessionRevokedEvent",
    "SessionReuseDetectedEvent",
    "AllSessionsRevokedEvent",
    "ImpersonationStartedEvent",
    "ImpersonationEndedEvent",
    "AccountLinkedEvent",
    "AccountUnlinkedEvent",
]


class _IdentityEventFields(DomainEvent):
    """
    Campos comunes. **No es una clase para suscribirse.**

    Existe sólo para no repetir los campos en catorce eventos. Nadie debería registrar un
    handler contra esto: el bus despacha por clase exacta, así que no recibiría nada. Es
    privada (guion bajo) justamente para que no aparezca en el `__all__` ni en la fachada.
    """

    #: Quién ejecutó. En un evento de sistema, `None` y `actor_name` lo describe.
    actor_user_id: UUID | None = None
    actor_name: str | None = None
    #: La cuenta afectada.
    subject_user_id: UUID | None = None
    #: Si la acción se hizo impersonando.
    impersonated: bool = False
    #: Correlación con el `REQUEST_ID` de la capa HTTP, para cruzar logs con auditoría.
    request_id: str | None = None


# ── Usuario ──────────────────────────────────────────────────────────────────
class UserRegisteredEvent(_IdentityEventFields):
    """
    Se creó una cuenta. `email` va acá para que un handler de bienvenida no consulte.

    **Opcional desde 10.0**, porque el alta puede ser sólo con nombre de usuario. Un handler
    que manda el mail de bienvenida tiene que chequearlo: con `None` no hay a dónde mandarlo, y
    esa es justamente la información que necesita para no intentarlo.
    """

    email: str | None = None
    #: El nombre de usuario, si la cuenta se creó con uno. Va por el mismo motivo que el mail:
    #: para que un handler no tenga que volver a consultar la fila que se acaba de crear.
    username: str | None = None


class UserEmailVerifiedEvent(_IdentityEventFields):
    email: str


class UserPasswordChangedEvent(_IdentityEventFields):
    """
    Cambió la contraseña.

    Quien escuche esto debería asumir que **todas** las sesiones del usuario se van a
    revocar: es la política, y el evento es la señal para notificar por mail.
    """


class UserSignedInEvent(_IdentityEventFields):
    transport: str


class UserSignInFailedEvent(_IdentityEventFields):
    """
    Un intento fallido.

    `email` puede venir en `None` a propósito: si el handler lo va a loguear, un mail que no
    existe en la base igual queda registrado, y eso convierte los logs en una lista de mails
    tanteados. Que lo decida la política de cada app.
    """

    email: str | None = None
    reason: str


# ── Sesión ───────────────────────────────────────────────────────────────────
class SessionCreatedEvent(_IdentityEventFields):
    session_id: UUID
    transport: str


class SessionRefreshedEvent(_IdentityEventFields):
    """
    Se rotó el refresh token.

    `previous_session_id` porque la rotación crea una fila nueva en la misma familia: sin el
    anterior no se puede reconstruir el linaje al investigar un reuso.
    """

    session_id: UUID
    previous_session_id: UUID
    family_id: UUID


class SessionRevokedEvent(_IdentityEventFields):
    session_id: UUID
    reason: str


class SessionReuseDetectedEvent(_IdentityEventFields):
    """
    Se presentó un refresh token ya consumido. **Señal de robo de token.**

    La respuesta es revocar la familia entera, no sólo el token: si el atacante y el usuario
    legítimo tienen los dos un token del linaje, revocar uno deja al otro adentro. Este
    evento existe para que se pueda alertar, porque es de las pocas señales inequívocas de
    compromiso que un sistema de auth puede emitir.
    """

    session_id: UUID
    family_id: UUID


class AllSessionsRevokedEvent(_IdentityEventFields):
    """Se incrementó `token_generation`: cae todo lo del usuario de una."""

    reason: str
    revoked_count: int = 0


# ── Impersonación ────────────────────────────────────────────────────────────
class ImpersonationStartedEvent(_IdentityEventFields):
    """
    Arrancó una impersonación.

    `reason` y `granted_by` son obligatorios, igual que en `Impersonation`: es el registro
    que responde "¿por qué este operador entró a la cuenta de este cliente?" seis meses
    después.
    """

    session_id: UUID
    granted_by: UUID
    reason: str
    expires_at: str


class ImpersonationEndedEvent(_IdentityEventFields):
    session_id: UUID
    #: `expired` si se venció el techo, `manual` si el operador la cerró.
    ended_by: t.Literal["manual", "expired"]


# ── Cuentas externas (OAuth) ─────────────────────────────────────────────────
class AccountLinkedEvent(_IdentityEventFields):
    provider_id: str
    account_id: str


class AccountUnlinkedEvent(_IdentityEventFields):
    provider_id: str
    account_id: str
