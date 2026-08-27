"""
Contexto de autenticación: **quién ejecuta** vs **a quién afecta**.

La distinción es la razón de existir de este módulo. Con un solo `user_id`, la impersonación
es indistinguible del uso normal: la fila queda escrita "por" la víctima y el operador de
soporte que la escribió desaparece del registro. Acá hay dos principales:

- ``actor``: la persona física que ejecuta. Nunca se deduce, nunca se hereda.
- ``subject``: la cuenta afectada. Por defecto **es** el actor.

El invariante lo garantiza un validador, no la disciplina: si ``subject`` difiere de
``actor``, ``impersonation`` es obligatorio. **Un contexto impersonado no auditable no se
puede construir.** Eso es el "sin magia negra" del diseño: no hay ningún camino donde
alguien actúe como otro y el sistema no lo sepa.

Por qué un ContextVar y no un parámetro: `AbstractMiddleware.handle(message, next_handler)`
no tiene parámetro de contexto, y `AbstractCommandHandler.handle(command)` recibe un solo
argumento. Cambiar esas firmas rompería el pipeline, los cuatro middlewares que se shippean,
los tres buses in-memory y los tres adaptadores de transporte. El ContextVar es el mecanismo
que `hexcore.domain.cqrs.context` ya establece para `IN_WORKER`, y `RequestIDMiddleware` ya
usa con doble publicación ContextVar + `request.state`.

Este módulo es **stdlib + pydantic y nada más**: lo importan el middleware de CQRS y los
handlers, así que tiene que poder importarse sin `[sql]`, sin `[api]` y sin `[darwin]`.
"""
from __future__ import annotations

import typing as t
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from hexcore.darwin.domain.exceptions import (
    ImpersonationNotPermittedError,
    InsufficientScopeError,
    UnauthenticatedError,
)

__all__ = [
    "AUTH_CONTEXT",
    "Transport",
    "Principal",
    "SystemPrincipal",
    "Impersonation",
    "AuthContext",
    "current_auth",
    "require_auth",
    "auth_scope",
    "system_context",
]


#: De dónde vino la credencial. Va en el `aud` del token, para que una cookie no se pueda
#: replayear como Bearer esquivando CSRF.
#:
#: - ``cookie`` / ``bearer``: los dos transportes HTTP.
#: - ``internal``: originado en el proceso (un cron, un seed, la CLI). No hay request.
#: - ``worker``: restaurado desde el sobre firmado que cruzó una cola.
Transport = t.Literal["cookie", "bearer", "internal", "worker"]

TUser = t.TypeVar("TUser")


# ── Principales ──────────────────────────────────────────────────────────────
class Principal(BaseModel):
    """
    Un sujeto identificado. Inmutable.

    `session_id` es opcional porque un principal de sistema no tiene sesión, pero para un
    usuario que llegó por HTTP **siempre** está: es lo que ata el token a una fila
    revocable, y sin él la revocación es imposible por construcción.
    """

    model_config = ConfigDict(frozen=True)

    user_id: UUID
    session_id: UUID | None = None
    email: str | None = None
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def has_role(self, role: str) -> bool:
        return role in self.roles


class SystemPrincipal(BaseModel):
    """
    El actor de un proceso automático: un cron, un seed, la CLI, un worker.

    **No es un superusuario.** Lleva un set de grants explícito y enumerado, declarado al
    cablear, y nada más. Un cron que cierra registros recibe ``{"register.close"}`` y no
    puede hacer otra cosa.

    La alternativa —un flag ``is_superuser=True``— es exactamente lo que este diseño evita:
    un bypass que se concede una vez "para que el cron funcione" y después nadie audita. Si
    un proceso necesita un permiso más, se agrega a su lista y queda en el diff.
    """

    model_config = ConfigDict(frozen=True)

    name: str
    scopes: frozenset[str] = frozenset()

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def has_role(self, role: str) -> bool:
        # Un principal de sistema no tiene roles: tiene grants. Modelarlo con roles
        # invitaría a darle "admin" y volver al superusuario por la puerta de atrás.
        return False


class Impersonation(BaseModel):
    """
    El permiso explícito que hace legítimo que `actor` difiera de `subject`.

    `reason` es obligatorio y no vacío a propósito: una auditoría que dice *quién* y *a
    quién* pero no *por qué* no sirve para responder la pregunta que se le hace seis meses
    después. `expires_at` también, porque una impersonación sin techo es una cuenta
    compartida con pasos extra.
    """

    model_config = ConfigDict(frozen=True)

    granted_by: UUID
    reason: str = Field(min_length=1)
    granted_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _la_ventana_es_valida(self) -> "Impersonation":
        if self.expires_at <= self.granted_at:
            raise ValueError(
                "Impersonation.expires_at tiene que ser posterior a granted_at: una "
                "impersonación que vence antes de empezar no habilita nada y deja un "
                "registro de auditoría engañoso."
            )
        return self

    def is_expired_at(self, moment: datetime) -> bool:
        return moment >= self.expires_at


# ── El contexto ──────────────────────────────────────────────────────────────
class AuthContext(BaseModel, t.Generic[TUser]):
    """
    Quién ejecuta (`actor`) y a quién afecta (`subject`).

    El parámetro genérico es lo que hace que la personalización se **vea** en el IDE: con
    `AuthContext[MiUsuario]`, `require_auth().user` tipa `MiUsuario | None` en vez de `Any`.
    Sin eso la extensión funciona pero es invisible, que es la mitad del valor.

    Uso::

        # sesión normal: subject es el actor
        ctx = AuthContext(actor=principal, subject=principal, transport="cookie")

        # impersonación: el permiso es obligatorio
        ctx = AuthContext(
            actor=soporte,
            subject=cliente,
            transport="cookie",
            impersonation=Impersonation(
                granted_by=supervisor.user_id,
                reason="ticket #4821: el cliente no puede cerrar su cuenta",
                granted_at=ahora,
                expires_at=ahora + timedelta(minutes=60),
            ),
        )
    """

    model_config = ConfigDict(frozen=True)

    actor: Principal | SystemPrincipal
    subject: Principal | SystemPrincipal
    transport: Transport
    impersonation: Impersonation | None = None
    #: El modelo extendido de la app, resuelto por `IdentityConfig.context_resolver`.
    user: TUser | None = None

    # ── Invariante ────────────────────────────────────────────────────────────
    @model_validator(mode="after")
    def _la_impersonacion_es_auditable(self) -> "AuthContext[TUser]":
        """
        Hace **imposible construir** una impersonación no auditable.

        Los dos sentidos importan:

        - `subject != actor` sin permiso → no hay registro de quién autorizó ni por qué.
        - permiso con `subject == actor` → ensuciaría la auditoría con impersonaciones que
          nunca ocurrieron, y una auditoría con ruido es una auditoría que nadie lee.
        """
        distintos = self.actor_id != self.subject_id

        if distintos and self.impersonation is None:
            raise ValueError(
                "subject difiere de actor sin un permiso de impersonación. Un contexto "
                "impersonado tiene que ser auditable por construcción: pasá "
                "`impersonation=Impersonation(granted_by=..., reason=..., granted_at=..., "
                "expires_at=...)`."
            )

        if not distintos and self.impersonation is not None:
            raise ValueError(
                "hay un permiso de impersonación pero actor y subject son el mismo. Eso "
                "dejaría en la auditoría una impersonación que nunca pasó: omití "
                "`impersonation` para una sesión normal."
            )

        return self

    # ── Identidades ───────────────────────────────────────────────────────────
    @property
    def actor_id(self) -> UUID | str:
        """El id del actor. Para un principal de sistema, su nombre."""
        return (
            self.actor.user_id
            if isinstance(self.actor, Principal)
            else self.actor.name
        )

    @property
    def subject_id(self) -> UUID | str:
        return (
            self.subject.user_id
            if isinstance(self.subject, Principal)
            else self.subject.name
        )

    @property
    def is_impersonating(self) -> bool:
        return self.impersonation is not None

    @property
    def is_system(self) -> bool:
        return isinstance(self.actor, SystemPrincipal)

    # ── Chequeos ──────────────────────────────────────────────────────────────
    def has_scope(self, scope: str) -> bool:
        """
        Si el **actor** tiene el permiso.

        Se consulta el actor y no el sujeto, y es la decisión de autorización central de
        todo el módulo: en una impersonación, lo que se puede hacer lo determina quien
        ejecuta, no la cuenta afectada. Al revés, impersonar a un admin sería una
        escalación de privilegios en un solo paso.
        """
        return self.actor.has_scope(scope)

    def has_role(self, role: str) -> bool:
        """Igual que `has_scope`: se consulta el actor."""
        return self.actor.has_role(role)

    def require_scopes(self, *scopes: str) -> None:
        """
        Lanza `InsufficientScopeError` si al actor le falta alguno.

        Raises:
            InsufficientScopeError: con el set completo de los que faltan, no el primero.
                Enterarse de a uno convierte una corrección en varias vueltas.
        """
        faltantes = {scope for scope in scopes if not self.actor.has_scope(scope)}
        if faltantes:
            raise InsufficientScopeError(faltantes)

    def assert_not_impersonating(self, operation: str) -> None:
        """
        Lanza si el contexto es impersonado.

        Para las operaciones que sólo el dueño real puede hacer: cambiar la contraseña,
        refrescar la sesión, dar de alta un segundo factor, o impersonar a un tercero.

        Raises:
            ImpersonationNotPermittedError
        """
        if self.impersonation is None:
            return
        raise ImpersonationNotPermittedError(
            f"'{operation}' no se puede ejecutar desde una sesión impersonada: "
            f"{self.actor_id} está actuando como {self.subject_id}."
        )


# ── Publicación ambiental ────────────────────────────────────────────────────
# Mismo patrón que `hexcore.domain.cqrs.context.IN_WORKER`: un ContextVar y un
# contextmanager con set/reset de token. No se inventa un mecanismo nuevo.
AUTH_CONTEXT: ContextVar["AuthContext[t.Any] | None"] = ContextVar(
    "hexcore_darwin_auth", default=None
)


def current_auth() -> "AuthContext[t.Any] | None":
    """
    El contexto en curso, o `None` fuera de uno.

    **Nunca lanza**, así que se puede llamar desde cualquier lado —incluido un handler de
    logging o un middleware que corre antes de la autenticación— sin comprobar nada.
    """
    return AUTH_CONTEXT.get()


def require_auth() -> "AuthContext[t.Any]":
    """
    El contexto en curso, o `UnauthenticatedError`.

    La versión para handlers: si tu handler necesita un actor, pedilo con esto y dejá que
    el mapeo de excepciones lo convierta en un 401.

    Raises:
        UnauthenticatedError: si no hay contexto.

    Uso::

        class TransferirFondos(AbstractCommandHandler[Transferir, None]):
            async def handle(self, command: Transferir) -> None:
                auth = require_auth()
                auth.require_scopes("funds.transfer")
                ...
    """
    context = AUTH_CONTEXT.get()
    if context is None:
        raise UnauthenticatedError(
            "No hay AuthContext en este contexto de ejecución. Si esto corre fuera de un "
            "request —un cron, un seed, la CLI—, envolvé el despacho en "
            "`with system_context('nombre', scopes={...}):`."
        )
    return context


@contextmanager
def auth_scope(context: "AuthContext[t.Any]") -> t.Iterator[None]:
    """
    Publica `context` como el contexto ambiental dentro del bloque.

    El `reset` va en un `finally`, así que una excepción no deja el contexto colgado para la
    corutina siguiente que reuse el mismo task.

    Anida: al salir se restaura el de afuera, no `None`. Es lo que permite que un handler
    corriendo en un worker despache otro comando y el actor restaurado se propague sin que
    la cadena de custodia se corte.

    Uso::

        with auth_scope(contexto):
            await command_bus.dispatch(comando)
    """
    token = AUTH_CONTEXT.set(context)
    try:
        yield
    finally:
        AUTH_CONTEXT.reset(token)


@contextmanager
def system_context(
    name: str,
    *,
    scopes: t.Iterable[str] = (),
    transport: Transport = "internal",
) -> t.Iterator["AuthContext[t.Any]"]:
    """
    Atajo para el actor de un proceso automático: cron, seed, CLI, migración.

    Existe para que "esto lo corre el sistema" sea **explícito y enumerado** en vez de un
    `None` que la autorización tenga que interpretar. La regla del módulo es que no hay
    ningún camino donde el chequeo devuelva "permitido" porque no pudo identificar al
    actor: la ausencia de actor es anónimo, y anónimo no pasa nada.

    Args:
        name: Quién es. Aparece en la auditoría, así que que sea reconocible
            (``"cron:cerrar-registros"``, no ``"system"``).
        scopes: Los permisos de este proceso. Enumerados, no un comodín.

    Uso::

        with system_context("cron:cerrar-registros", scopes={"register.close"}):
            await command_bus.dispatch(CerrarRegistros())
    """
    principal = SystemPrincipal(name=name, scopes=frozenset(scopes))
    context: AuthContext[t.Any] = AuthContext(
        actor=principal, subject=principal, transport=transport
    )
    with auth_scope(context):
        yield context
