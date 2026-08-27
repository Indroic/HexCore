"""
Los flujos de `impersonate`: empezar, terminar, y describir.

**No hay magia negra, y eso es todo el punto.** Una impersonación acá no es un flag en la sesión
del operador ni un `user_id` sustituido en un middleware: es **una sesión nueva** con dos
principales distintos, `actor_user_id` y `subject_user_id`, persistidos los dos en la fila. El
`AuthContext` que sale de ella no se puede construir sin un `Impersonation` con motivo y
vencimiento, así que una impersonación no auditable no existe ni por error.

Las cuatro propiedades que se sostienen sin excepción:

1. **La sesión del operador sigue viva.** Empezar una impersonación no la toca, así que terminarla
   es volver a usar la propia — no hay que reconstruir nada, y si el operador cierra la pestaña la
   impersonación muere sola con su techo.
2. **Techo de 60 minutos, no renovable.** Lo fija `SessionService.create` y lo hace real el
   rechazo del refresh: una sesión impersonada no se rota. Ver `IMPERSONATION_CAP`.
3. **Cada acción queda auditada con los dos principales.** Lo hace el `AuthContext`, que viaja en
   el token y —vía el sobre firmado de la Fase 6— también por la cola: un comando encolado
   durante una impersonación se procesa con el actor correcto en el worker.
4. **`has_scope` y `has_role` consultan al actor, nunca al subject.** Impersonar no presta
   permisos: el operador ve lo que el otro ve, y puede hacer lo que **él** puede hacer.
"""
from __future__ import annotations

import typing as t
from uuid import UUID

from hexcore.darwin.domain.exceptions import UnauthenticatedError
from hexcore.darwin.plugins.impersonate.domain import (
    ImpersonationNotActiveError,
    ScopeImpersonationPolicy,
)

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.context import AuthContext, Transport
    from hexcore.darwin.domain.entities import IdentitySession, User
    from hexcore.darwin.domain.ports import (
        AbstractAuditSink,
        AbstractClock,
        AbstractSessionRepository,
        AbstractUserRepository,
    )
    from hexcore.darwin.domain.value_objects import TokenPair
    from hexcore.darwin.plugins.impersonate.domain import AbstractImpersonationPolicy

__all__ = ["Impersonated", "ImpersonationInfo", "ImpersonationService"]


class Impersonated(t.NamedTuple):
    """
    El resultado de empezar una impersonación.

    Trae la sesión y el par nuevos. La sesión del operador **no** está acá porque no cambió: sigue
    valiendo, y el cliente la conserva para volver.
    """

    subject: "User"
    session: "IdentitySession"
    tokens: "TokenPair"


class ImpersonationInfo(t.NamedTuple):
    """Lo que la interfaz necesita para mostrar la barra de "estás viendo como…"."""

    active: bool
    actor_id: UUID | str | None = None
    subject_id: UUID | str | None = None
    reason: str | None = None
    expires_at: t.Any = None


class ImpersonationService:
    """
    Empezar y terminar impersonaciones.

    Uso::

        servicio = get_impersonation_service()
        resultado = await servicio.start(
            context=current_auth(), subject_id=uid, reason="ticket #4821"
        )
    """

    def __init__(
        self,
        *,
        users: "AbstractUserRepository",
        sessions_repository: "AbstractSessionRepository",
        sessions: t.Any,
        clock: "AbstractClock",
        policy: "AbstractImpersonationPolicy | None" = None,
        audit: "AbstractAuditSink | None" = None,
    ) -> None:
        self._users = users
        self._sessions_repository = sessions_repository
        self._sessions = sessions
        self._clock = clock
        self._policy = policy or ScopeImpersonationPolicy()
        self._audit = audit

    # ── Empezar ───────────────────────────────────────────────────────────────
    async def start(
        self,
        *,
        context: "AuthContext[t.Any] | None",
        subject_id: UUID,
        reason: str,
        transport: "Transport" = "cookie",
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> Impersonated:
        """
        Crea una sesión impersonada. **La del operador sigue viva.**

        Args:
            context: El contexto del operador. `None` es un error y no un caso anónimo: una
                impersonación sin actor conocido es justamente lo que no puede existir.
            subject_id: A quién.
            reason: Por qué. **Obligatorio y no vacío**, y lo exige el modelo: una auditoría que
                dice quién y a quién pero no por qué no sirve para responder la pregunta que se le
                hace seis meses después.
            transport: Cookie o Bearer, igual que cualquier sesión.

        Raises:
            UnauthenticatedError: no hay contexto.
            ValueError: el motivo está vacío.
            ImpersonationDeniedError y subclases: lo decide la política.
            InvalidCredentialsError: el sujeto no existe. Mismo error que un login fallido: un 404
                distinto le confirmaría a un operador con permiso parcial qué ids existen.
        """
        from hexcore.darwin.domain.exceptions import InvalidCredentialsError

        if context is None:
            raise UnauthenticatedError(
                "Para impersonar hace falta una sesión: la impersonación se define por tener un "
                "actor identificado."
            )

        if not reason.strip():
            raise ValueError(
                "El motivo de la impersonación es obligatorio. Sin él, la auditoría registra "
                "quién y a quién pero no por qué, que es lo único que se pregunta después."
            )

        sujeto = await self._users.get_by_id(subject_id)
        if sujeto is None:
            raise InvalidCredentialsError("No se encontró a esa persona.")

        # La política decide, y decide **antes** de que exista cualquier sesión: si autorizara
        # después de crearla, un rechazo dejaría una sesión impersonada huérfana que hay que
        # revocar — y el camino de limpieza es el que falla.
        self._policy.authorize(context=context, subject=sujeto)

        actor = await self._users.get_by_id(_como_uuid(context.actor_id))
        if actor is None:
            raise UnauthenticatedError("El actor de la sesión ya no existe.")

        sesion, par = await self._sessions.create(
            actor=actor,
            subject=sujeto,
            transport=transport,
            ip_address=ip_address,
            user_agent=user_agent,
            impersonation_reason=reason.strip(),
            impersonation_granted_by=actor.id,
            # Los scopes del **actor**, no los del sujeto. Impersonar no presta permisos: el
            # operador ve lo que el otro ve y puede hacer lo que él mismo puede hacer.
            scopes=context.actor.scopes,
        )

        await self._auditar(
            action="impersonation.start",
            actor_id=actor.id,
            subject_id=sujeto.id,
            metadata={
                "reason": reason.strip(),
                "session_id": str(sesion.id),
                "operator_session_id": str(_session_id_de(context)),
                "expires_at": (
                    sesion.impersonation_expires_at.isoformat()
                    if sesion.impersonation_expires_at
                    else None
                ),
            },
        )
        return Impersonated(subject=sujeto, session=sesion, tokens=par)

    # ── Terminar ──────────────────────────────────────────────────────────────
    async def stop(self, *, context: "AuthContext[t.Any] | None") -> None:
        """
        Termina la impersonación: revoca **la sesión impersonada** y nada más.

        No hay que devolverle nada al operador: su sesión original nunca se tocó. Es lo que hace
        que "volver" sea del lado del cliente —descartar el token de impersonación y seguir con
        el propio— en vez de un segundo intercambio que puede fallar a mitad de camino.

        Raises:
            UnauthenticatedError: no hay contexto.
            ImpersonationNotActiveError: la sesión no es una impersonación.
        """
        if context is None:
            raise UnauthenticatedError("No hay sesión.")

        if not context.is_impersonating:
            raise ImpersonationNotActiveError(
                "Esta sesión no es una impersonación, así que no hay nada que terminar."
            )

        sid = _session_id_de(context)
        if sid is None:  # pragma: no cover - un contexto impersonado siempre tiene sesión
            raise ImpersonationNotActiveError(
                "La sesión impersonada no tiene identificador, así que no se puede revocar."
            )
        await self._sessions.revoke(sid, reason="impersonation.stop")

        await self._auditar(
            action="impersonation.stop",
            actor_id=context.actor_id,
            subject_id=context.subject_id,
            metadata={"session_id": str(sid)},
        )

    # ── Describir ─────────────────────────────────────────────────────────────
    async def describe(
        self, context: "AuthContext[t.Any] | None"
    ) -> ImpersonationInfo:
        """
        El estado de impersonación del contexto.

        Es lo que la interfaz necesita para mostrar la barra de "estás viendo como…", y esa barra
        no es cosmética: sin ella, un operador olvida que está impersonando y toma decisiones
        creyendo que actúa como él mismo.

        **Lee la fila de `session` cuando hay impersonación**, y es la excepción deliberada al
        "cero DB en el camino caliente" — la misma que hace `/auth/me`. El motivo y el
        vencimiento real **no viajan en el token**: el motivo es texto de largo arbitrario y el
        vencimiento del techo es un claim más que se pagaría en cada petición para un caso raro.
        El contexto reconstruido de un token trae un motivo marcador y el `exp` del access token,
        que sirven para el invariante del modelo y no para mostrarle nada a nadie.

        Es una lectura por llamada a este endpoint, no por request autenticado: la barra se pinta
        una vez por carga de página.
        """
        if context is None or not context.is_impersonating:
            return ImpersonationInfo(active=False)

        permiso = context.impersonation
        reason = permiso.reason if permiso else None
        expires_at = permiso.expires_at if permiso else None

        sid = _session_id_de(context)
        if sid is not None:
            fila = await self._sessions_repository.get(sid)
            if fila is not None:
                # La fila es la fuente de verdad: el token sólo dice *que* hay impersonación.
                reason = fila.impersonation_reason or reason
                expires_at = fila.impersonation_expires_at or expires_at

        return ImpersonationInfo(
            active=True,
            actor_id=context.actor_id,
            subject_id=context.subject_id,
            reason=reason,
            expires_at=expires_at,
        )

    # ── Interno ───────────────────────────────────────────────────────────────
    async def _auditar(self, **kwargs: t.Any) -> None:
        """
        Escribe en la auditoría si hay sink.

        `impersonated=True` en las dos acciones: el registro de que la impersonación empezó
        también es un evento *de* una impersonación, y filtrar la auditoría por ese flag tiene
        que traer el inicio además de lo que pasó adentro.
        """
        if self._audit is not None:
            await self._audit.record(impersonated=True, **kwargs)


def _session_id_de(context: "AuthContext[t.Any]") -> UUID | None:
    """
    El `session_id` del actor, o `None`.

    Existe porque `AuthContext.actor` es `Principal | SystemPrincipal` y **sólo el primero tiene
    sesión**: un cron no la tiene, y por eso el campo no está en la unión. Leerlo con `hasattr`
    funcionaría en runtime y le esconde al checker que hay un caso sin manejar.
    """
    from hexcore.darwin.domain.context import Principal

    actor = context.actor
    return actor.session_id if isinstance(actor, Principal) else None


def _como_uuid(valor: UUID | str) -> UUID:
    """
    El `actor_id` a `UUID`.

    `AuthContext.actor_id` puede ser un string —un principal de sistema, como
    ``"cron:cerrar-registros"``— y esos no pueden impersonar: no tienen fila en `user`, así que
    `get_by_id` no los encontraría. Se convierte acá y el `ValueError` lo traduce el llamador.
    """
    if isinstance(valor, UUID):
        return valor
    try:
        return UUID(valor)
    except ValueError as exc:
        raise UnauthenticatedError(
            f"El actor {valor!r} no es un usuario: un principal de sistema no puede impersonar, "
            f"porque no hay a quién responsabilizar del acceso."
        ) from exc
