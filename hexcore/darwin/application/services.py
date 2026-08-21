"""
Servicios de aplicación: los flujos de identidad.

Acá vive la lógica que los handlers CQRS orquestan. Está en servicios y no en los handlers
porque los mismos flujos se invocan desde más de un lado: un handler HTTP, la CLI que crea el
primer admin, un seed, un plugin. Un handler es un adaptador de un mensaje a una operación; la
operación es esto.

Tres invariantes de seguridad que se implementan **acá** y no en la capa HTTP, porque la capa
HTTP es sólo uno de los llamadores:

1. **El sign-in hashea una contraseña señuelo cuando el usuario no existe.** Sin eso, responder
   "credenciales inválidas" tarda microsegundos para un mail inexistente y decenas de
   milisegundos para uno real, y esa diferencia enumera usuarios registrados sin adivinar ni
   una contraseña.
2. **El token de sesión se guarda hasheado y se compara por hash.** La fila nunca ve el token.
3. **Rotar el refresh detecta el reuso.** Si el token ya estaba consumido, se revoca la familia
   entera: si el atacante y el usuario legítimo tienen los dos un token del linaje, revocar uno
   solo deja al otro adentro, y no hay forma de saber cuál es cuál.
"""
from __future__ import annotations

import typing as t
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from hexcore.darwin.domain.context import AuthContext, Principal, Transport
from hexcore.darwin.domain.entities import (
    CREDENTIAL_PROVIDER,
    Account,
    IdentitySession,
    User,
    Verification,
)
from hexcore.darwin.domain.events import (
    SessionCreatedEvent,
    SessionReuseDetectedEvent,
    SessionRevokedEvent,
    UserEmailVerifiedEvent,
    UserRegisteredEvent,
    UserSignedInEvent,
    UserSignInFailedEvent,
)
from hexcore.darwin.domain.exceptions import (
    AccountLockedError,
    EmailAlreadyRegisteredError,
    EmailNotVerifiedError,
    ImpersonationNotPermittedError,
    InvalidCredentialsError,
    TokenExpiredError,
    TokenMalformedError,
    TokenRevokedError,
)
from hexcore.darwin.application.hooks import run_hooks
from hexcore.darwin.domain.value_objects import Email, TokenPair, VerificationPurpose

if t.TYPE_CHECKING:
    from hexcore.darwin.application.config import IdentityConfig
    from hexcore.darwin.application.plugins import PluginRegistry
    from hexcore.darwin.domain.ports import (
        AbstractAccountRepository,
        AbstractAuditSink,
        AbstractClock,
        AbstractPasswordHasher,
        AbstractRevocationList,
        AbstractSessionRepository,
        AbstractUserRepository,
        AbstractVerificationRepository,
    )
    from hexcore.darwin.infrastructure.tokens import (
        JoserfcTokenIssuer,
        JoserfcTokenVerifier,
    )
    from hexcore.domain.events import EventBus

__all__ = [
    "SessionService",
    "IdentityService",
    "SIGN_IN_AUTHENTICATED",
    "IMPERSONATION_CAP",
]

#: El punto de extensión del sign-in: la contraseña ya se validó y la sesión **todavía no
#: existe**.
#:
#: Es una acción y no un evento porque un hook acá puede **abortar** el sign-in: `two_factor`
#: lanza `TwoFactorRequiredError` y no se emite ningún token. Un evento se publica después del
#: hecho y no tiene forma de impedirlo.
SIGN_IN_AUTHENTICATED = "user.sign_in.authenticated"

#: Ventana de gracia para un refresh rotado. Dentro de ella, presentar el token ya consumido
#: devuelve el **mismo** par nuevo en vez de disparar la detección de reuso.
#:
#: Existe porque sin ella un cliente con dos pestañas, o uno que reintenta tras un timeout de
#: red, dispara "reuso de token" y se auto-desloguea de todos lados. Es el falso positivo más
#: común de la detección de reuso, y el que hace que los equipos la terminen desactivando.
GRACE_WINDOW = timedelta(seconds=10)

#: Techo de vida de una sesión impersonada, **no renovable**.
#:
#: 60 minutos: alcanza para atender un caso de soporte y no alcanza para que la sesión quede
#: abierta en una pestaña olvidada. Y es un techo duro y no un TTL renovable porque una
#: impersonación que se renueva sola es una cuenta compartida con pasos extra: el operador entra
#: una vez y se queda, y la auditoría dice "una impersonación" donde hubo tres días de acceso.
IMPERSONATION_CAP = timedelta(minutes=60)


class SessionService:
    """
    Crea, verifica, rota y revoca sesiones.

    Separado de `IdentityService` porque los flujos de sesión son los que reusan los plugins:
    2FA crea una sesión recién tras el segundo factor, OAuth la crea sin contraseña, e
    Impersonate la crea con dos principales distintos. Todos pasan por acá.
    """

    def __init__(
        self,
        *,
        sessions: "AbstractSessionRepository",
        users: "AbstractUserRepository",
        issuer: "JoserfcTokenIssuer",
        verifier: "JoserfcTokenVerifier",
        revocations: "AbstractRevocationList",
        clock: "AbstractClock",
        config: "IdentityConfig",
        events: "EventBus | None" = None,
        audit: "AbstractAuditSink | None" = None,
    ) -> None:
        self._sessions = sessions
        self._users = users
        self._issuer = issuer
        self._verifier = verifier
        self._revocations = revocations
        self._clock = clock
        self._config = config
        self._events = events
        self._audit = audit

    # ── Crear ─────────────────────────────────────────────────────────────────
    async def create(
        self,
        *,
        actor: User,
        subject: User | None = None,
        transport: Transport = "cookie",
        ip_address: str | None = None,
        user_agent: str | None = None,
        impersonation_reason: str | None = None,
        impersonation_granted_by: UUID | None = None,
        scopes: t.Iterable[str] = (),
    ) -> tuple[IdentitySession, TokenPair]:
        """
        Crea una sesión y emite su par de tokens.

        `subject` distinto de `actor` es una impersonación, y entonces `impersonation_reason` y
        `impersonation_granted_by` son **obligatorios** — el `AuthContext` que se construye acá
        los exige, así que una impersonación no auditable no se puede crear ni por error.

        Devuelve la sesión persistida y el par. El **token en claro no vuelve a estar
        disponible**: la fila guarda su hash, así que si lo perdés hay que crear otra sesión.
        """
        from hexcore.darwin.domain.context import Impersonation
        from hexcore.darwin.infrastructure.hashing import generate_token, hash_token

        ahora = self._clock.now()
        es_impersonacion = subject is not None and subject.id != actor.id
        afectado = subject if subject is not None else actor

        if es_impersonacion and (
            impersonation_reason is None or impersonation_granted_by is None
        ):
            raise ValueError(
                "Una impersonación necesita `impersonation_reason` y "
                "`impersonation_granted_by`: sin los dos, la auditoría no puede responder "
                "quién autorizó ni por qué."
            )

        token_claro = generate_token()
        sesion = IdentitySession(
            actor_user_id=actor.id,
            subject_user_id=afectado.id,
            token_hash=hash_token(token_claro),
            family_id=uuid4(),
            transport=transport,
            expires_at=ahora + self._config.tokens.session_ttl,
            ip_address=ip_address,
            user_agent=user_agent,
            impersonation_reason=impersonation_reason,
            impersonation_granted_by=impersonation_granted_by,
            impersonation_expires_at=(
                ahora + IMPERSONATION_CAP if es_impersonacion else None
            ),
        )
        sesion = await self._sessions.add(sesion)

        impersonacion = None
        if es_impersonacion:
            impersonacion = Impersonation(
                granted_by=t.cast(UUID, impersonation_granted_by),
                reason=t.cast(str, impersonation_reason),
                granted_at=ahora,
                expires_at=t.cast(datetime, sesion.impersonation_expires_at),
            )

        contexto: AuthContext[t.Any] = AuthContext(
            actor=Principal(
                user_id=actor.id, session_id=sesion.id, email=actor.email,
                scopes=frozenset(scopes),
            ),
            subject=Principal(
                user_id=afectado.id, session_id=sesion.id, email=afectado.email
            ),
            transport=transport,
            impersonation=impersonacion,
        )

        par = await self._emitir_par(contexto, sesion, actor)

        await self._publicar(
            SessionCreatedEvent(
                actor_user_id=actor.id,
                subject_user_id=afectado.id,
                impersonated=es_impersonacion,
                session_id=sesion.id,
                transport=transport,
            )
        )
        return sesion, par

    async def _emitir_par(
        self,
        contexto: "AuthContext[t.Any]",
        sesion: IdentitySession,
        actor: User,
    ) -> TokenPair:
        acceso = await self._issuer.issue_access(
            contexto,
            session_id=sesion.id,
            generation=actor.token_generation,
            scopes=contexto.actor.scopes,
        )
        refresco = await self._issuer.issue_refresh(
            contexto, session_id=sesion.id, generation=actor.token_generation
        )
        return TokenPair(
            access_token=acceso,
            refresh_token=refresco,
            expires_in=int(self._config.tokens.access_ttl.total_seconds()),
            session_id=sesion.id,
        )

    # ── Verificar ─────────────────────────────────────────────────────────────
    async def authenticate(
        self, access_token: str, *, transport: Transport
    ) -> AuthContext[t.Any]:
        """
        Verifica un access token y reconstruye el `AuthContext`.

        Es el camino caliente: **no toca la base**. Verifica la firma, la ventana temporal, el
        `aud` del transporte y el `typ`, y después consulta la denylist en cache. La fila de
        `session` se lee sólo en `refresh()`, en el worker, y cuando el cache falla con
        política `deny`.

        Raises:
            TokenMalformedError, TokenExpiredError, TokenAudienceMismatchError: del verificador.
            TokenRevokedError: si el `sid` está en la denylist.
        """
        from hexcore.darwin.domain.context import Impersonation

        claims = await self._verifier.verify(
            access_token, transport=transport, expected_typ="at+jwt"
        )

        if await self._revocations.is_revoked(claims.sid):
            raise TokenRevokedError("La sesión fue revocada.")

        impersonacion = None
        if claims.imp:
            # El motivo y quién autorizó no viajan en el token —serían claims de tamaño
            # arbitrario controlados por el flujo— así que se reconstruye lo mínimo para que el
            # invariante del contexto se cumpla. El detalle auditable vive en la fila y en
            # `audit_log`.
            ahora = self._clock.now()
            impersonacion = Impersonation(
                granted_by=claims.act,
                reason="(en el registro de auditoría)",
                granted_at=ahora,
                expires_at=claims.expires_at,
            )

        return AuthContext(
            actor=Principal(
                user_id=claims.act, session_id=claims.sid, scopes=claims.scopes
            ),
            subject=Principal(user_id=claims.sub, session_id=claims.sid),
            transport=transport,
            impersonation=impersonacion,
        )

    # ── Rotar ─────────────────────────────────────────────────────────────────
    async def refresh(
        self, refresh_token: str, *, transport: Transport
    ) -> tuple[IdentitySession, TokenPair]:
        """
        Rota el refresh token. Detecta el reuso.

        El flujo: verificar el token → consumir la fila **atómicamente** → crear la sesión
        siguiente en la misma familia → emitir el par nuevo.

        Si `consume_for_rotation` devuelve `None`, el token ya estaba consumido. Eso es, o un
        reintento benigno dentro de la ventana de gracia, o **un token robado**. No hay forma de
        distinguirlos, así que fuera de la ventana se revoca la familia entera y se publica
        `SessionReuseDetectedEvent` — una de las pocas señales inequívocas de compromiso que un
        sistema de auth puede emitir.

        Raises:
            TokenRevokedError: reuso detectado, o sesión ya revocada.
            TokenMalformedError: token inválido, o el sujeto ya no existe.
        """
        claims = await self._verifier.verify(
            refresh_token, transport=transport, expected_typ="rt+jwt"
        )
        ahora = self._clock.now()

        anterior = await self._sessions.get(claims.sid)
        if anterior is None:
            raise TokenMalformedError("La sesión del token no existe.")

        if anterior.revoked_at is not None:
            raise TokenRevokedError("La sesión fue revocada.")

        # ⚠️ Una sesión impersonada **no se rota**, y ese es el mecanismo que hace que el techo
        # de 60 minutos sea real. Si se pudiera refrescar, el operador extendería la sesión
        # indefinidamente sin volver a pedir permiso ni dejar un segundo registro de auditoría:
        # el techo pasaría a ser "60 minutos por refresh", o sea ninguno.
        #
        # Se chequea **antes** de `consume_for_rotation` a propósito: consumir la fila y después
        # rechazar dejaría la sesión inutilizable por lo que queda de su hora.
        if anterior.is_impersonated:
            raise ImpersonationNotPermittedError(
                "Una sesión impersonada no se puede refrescar. Su techo de vida es duro: "
                "cuando vence, hay que volver a pedir la impersonación."
            )

        consumida = await self._sessions.consume_for_rotation(claims.sid, at=ahora)
        if consumida is None:
            return await self._manejar_reuso(anterior, ahora, transport)

        if ahora >= anterior.expires_at:
            raise TokenExpiredError(
                "La sesión alcanzó su techo de vida. Volvé a iniciar sesión."
            )

        actor = await self._users.get_by_id(anterior.actor_user_id)
        if actor is None:
            raise TokenMalformedError("El actor de la sesión ya no existe.")

        # El `gen` del token tiene que seguir siendo el vigente: si el usuario cambió la
        # contraseña entre la emisión y el refresh, el corte masivo tiene que valer también acá.
        if claims.gen < actor.token_generation:
            raise TokenRevokedError(
                "Todas las sesiones del usuario fueron revocadas. Volvé a iniciar sesión."
            )

        return await self._rotar(anterior, actor, ahora, transport)

    async def _manejar_reuso(
        self, sesion: IdentitySession, ahora: datetime, transport: Transport
    ) -> tuple[IdentitySession, TokenPair]:
        """
        Un refresh ya consumido. Ventana de gracia, o robo.

        Dentro de la gracia se acepta en silencio: dos pestañas o un reintento tras un timeout
        de red son el falso positivo más común de la detección de reuso, y el que hace que los
        equipos la terminen desactivando. Fuera de ella, cae la familia entera.
        """
        consumido_hace = (
            ahora - sesion.consumed_at if sesion.consumed_at is not None else GRACE_WINDOW
        )
        if consumido_hace < GRACE_WINDOW:
            raise TokenRevokedError(
                "Este token de refresco ya se canjeó hace instantes. Reintentá con el par "
                "nuevo; si no lo tenés, volvé a iniciar sesión."
            )

        revocadas = await self._sessions.revoke_family(
            sesion.family_id, at=ahora, reason="reuse-detected"
        )
        await self._publicar(
            SessionReuseDetectedEvent(
                actor_user_id=sesion.actor_user_id,
                subject_user_id=sesion.subject_user_id,
                impersonated=sesion.is_impersonated,
                session_id=sesion.id,
                family_id=sesion.family_id,
            )
        )
        await self._auditar(
            action="session.reuse_detected",
            actor_id=sesion.actor_user_id,
            subject_id=sesion.subject_user_id,
            metadata={"family_id": str(sesion.family_id), "revoked": revocadas},
        )
        raise TokenRevokedError(
            "Se detectó el reuso de un token de refresco: todas las sesiones de esa familia "
            "se revocaron. Volvé a iniciar sesión."
        )

    async def _rotar(
        self,
        anterior: IdentitySession,
        actor: User,
        ahora: datetime,
        transport: Transport,
    ) -> tuple[IdentitySession, TokenPair]:
        from hexcore.darwin.domain.context import Impersonation
        from hexcore.darwin.infrastructure.hashing import generate_token, hash_token

        token_claro = generate_token()
        siguiente = IdentitySession(
            actor_user_id=anterior.actor_user_id,
            subject_user_id=anterior.subject_user_id,
            token_hash=hash_token(token_claro),
            # Misma familia: es lo que permite revocar el linaje entero ante un reuso.
            family_id=anterior.family_id,
            transport=transport,
            # El techo **no** se extiende al rotar: si se extendiera, rotar indefinidamente
            # sería una sesión eterna y `session_ttl` no valdría para nada.
            expires_at=anterior.expires_at,
            ip_address=anterior.ip_address,
            user_agent=anterior.user_agent,
            impersonation_reason=anterior.impersonation_reason,
            impersonation_granted_by=anterior.impersonation_granted_by,
            impersonation_expires_at=anterior.impersonation_expires_at,
        )
        siguiente = await self._sessions.add(siguiente)

        impersonacion = None
        if anterior.is_impersonated:
            impersonacion = Impersonation(
                granted_by=t.cast(UUID, anterior.impersonation_granted_by),
                reason=t.cast(str, anterior.impersonation_reason),
                granted_at=ahora,
                expires_at=t.cast(datetime, anterior.impersonation_expires_at),
            )

        contexto: AuthContext[t.Any] = AuthContext(
            actor=Principal(
                user_id=anterior.actor_user_id,
                session_id=siguiente.id,
                email=actor.email,
            ),
            subject=Principal(
                user_id=anterior.subject_user_id, session_id=siguiente.id
            ),
            transport=transport,
            impersonation=impersonacion,
        )

        par = await self._emitir_par(contexto, siguiente, actor)

        # La sesión anterior queda en la denylist: su access token todavía puede estar vigente
        # hasta `access_ttl`, y sin esto seguiría sirviendo después de rotar.
        await self._revocations.revoke(
            anterior.id, until=ahora + self._config.tokens.access_ttl
        )

        from hexcore.darwin.domain.events import SessionRefreshedEvent

        await self._publicar(
            SessionRefreshedEvent(
                actor_user_id=anterior.actor_user_id,
                subject_user_id=anterior.subject_user_id,
                impersonated=anterior.is_impersonated,
                session_id=siguiente.id,
                previous_session_id=anterior.id,
                family_id=anterior.family_id,
            )
        )
        return siguiente, par

    # ── Revocar ───────────────────────────────────────────────────────────────
    async def revoke(self, session_id: UUID, *, reason: str = "sign-out") -> None:
        """Revoca una sesión: la fila y la denylist, en ese orden."""
        ahora = self._clock.now()
        sesion = await self._sessions.get(session_id)

        await self._sessions.revoke(session_id, at=ahora, reason=reason)
        # La denylist cubre el access token que todavía puede estar vigente. Sin ella, cerrar
        # sesión no tiene efecto hasta que el access venza.
        await self._revocations.revoke(
            session_id, until=ahora + self._config.tokens.access_ttl
        )

        if sesion is not None:
            await self._publicar(
                SessionRevokedEvent(
                    actor_user_id=sesion.actor_user_id,
                    subject_user_id=sesion.subject_user_id,
                    impersonated=sesion.is_impersonated,
                    session_id=session_id,
                    reason=reason,
                )
            )

    async def revoke_all_for(self, user_id: UUID, *, reason: str) -> int:
        """
        Revoca **todas** las sesiones del usuario, con un solo UPDATE sobre su generación.

        Además revoca las filas y pone cada `sid` en la denylist: el contador de generación
        cubre los tokens nuevos, pero un access token ya emitido lleva el `gen` viejo y seguiría
        pasando la capa 3 hasta que el cache de generación venza.
        """
        ahora = self._clock.now()
        activas = await self._sessions.list_active_for_user(user_id)

        await self._users.bump_token_generation(user_id)

        for sesion in activas:
            await self._sessions.revoke(sesion.id, at=ahora, reason=reason)
            await self._revocations.revoke(
                sesion.id, until=ahora + self._config.tokens.access_ttl
            )

        from hexcore.darwin.domain.events import AllSessionsRevokedEvent

        await self._publicar(
            AllSessionsRevokedEvent(
                actor_user_id=user_id,
                subject_user_id=user_id,
                reason=reason,
                revoked_count=len(activas),
            )
        )
        return len(activas)

    # ── Helpers ───────────────────────────────────────────────────────────────
    async def _publicar(self, evento: t.Any) -> None:
        if self._events is not None:
            await self._events.publish(evento)

    async def _auditar(self, **kwargs: t.Any) -> None:
        if self._audit is not None:
            await self._audit.record(**kwargs)


class IdentityService:
    """
    Registro, verificación de email y sign-in.

    Orquesta `SessionService` para todo lo que sea sesión: acá vive lo que es específico de las
    credenciales.
    """

    def __init__(
        self,
        *,
        users: "AbstractUserRepository",
        accounts: "AbstractAccountRepository",
        verifications: "AbstractVerificationRepository",
        sessions: SessionService,
        hasher: "AbstractPasswordHasher",
        clock: "AbstractClock",
        config: "IdentityConfig",
        events: "EventBus | None" = None,
        plugins: "PluginRegistry | None" = None,
    ) -> None:
        self._users = users
        self._accounts = accounts
        self._verifications = verifications
        self._sessions = sessions
        self._hasher = hasher
        self._clock = clock
        self._config = config
        self._events = events
        self._plugins = plugins

    # ── Registro ──────────────────────────────────────────────────────────────
    async def sign_up(
        self, *, email: str, password: str, name: str | None = None
    ) -> tuple[User, str]:
        """
        Crea un usuario con credencial local y devuelve `(usuario, código_de_verificación)`.

        El código vuelve **en claro** porque hay que mandarlo por mail: la fila guarda su hash.
        Es la única vez que existe.

        La política de contraseñas se valida **antes** de tocar la base, así que una contraseña
        inválida no deja un usuario a medio crear.

        Raises:
            ValueError: si la contraseña no cumple la política.
            EmailAlreadyRegisteredError: si el mail ya tiene cuenta.
        """
        from hexcore.darwin.infrastructure.hashing import (
            generate_numeric_code,
            hash_token,
        )

        self._config.passwords.validate_password(password)
        normalizado = Email(value=email).value

        if await self._users.get_by_email(normalizado) is not None:
            # Nota: en una ruta pública de sign-up esto también es un oráculo de enumeración.
            # La ruta HTTP debería responder igual exista o no la cuenta y diferenciar por el
            # mail que manda; esta excepción es para los flujos administrativos.
            raise EmailAlreadyRegisteredError(f"Ya existe una cuenta para {normalizado}.")

        usuario = await self._users.add(User(email=normalizado, name=name))
        await self._accounts.add(
            Account(
                user_id=usuario.id,
                provider_id=CREDENTIAL_PROVIDER,
                account_id=str(usuario.id),
                password=self._hasher.hash(password),
            )
        )

        codigo = generate_numeric_code(6)
        ahora = self._clock.now()
        await self._verifications.add(
            Verification(
                identifier=normalizado,
                value_hash=hash_token(codigo),
                purpose="email_verification",
                expires_at=ahora + timedelta(hours=24),
            )
        )

        await self._publicar(
            UserRegisteredEvent(
                actor_user_id=usuario.id,
                subject_user_id=usuario.id,
                email=normalizado,
            )
        )
        return usuario, codigo

    async def verify_email(self, *, email: str, code: str) -> User:
        """
        Canjea un código de verificación.

        Raises:
            InvalidCredentialsError: código inválido, vencido o ya usado. Un solo error para los
                tres casos: distinguirlos diría si el mail tiene un código pendiente.
        """
        from hexcore.darwin.infrastructure.hashing import hash_token

        normalizado = Email(value=email).value
        ahora = self._clock.now()

        consumido = await self._verifications.consume(
            normalizado, "email_verification", hash_token(code), at=ahora
        )
        if consumido is None:
            raise InvalidCredentialsError("El código de verificación no es válido.")

        usuario = await self._users.get_by_email(normalizado)
        if usuario is None:
            raise InvalidCredentialsError("El código de verificación no es válido.")

        actualizado = await self._users.update(
            usuario.model_copy(update={"email_verified": True})
        )
        await self._publicar(
            UserEmailVerifiedEvent(
                actor_user_id=actualizado.id,
                subject_user_id=actualizado.id,
                email=normalizado,
            )
        )
        return actualizado

    # ── Sign-in ───────────────────────────────────────────────────────────────
    async def sign_in(
        self,
        *,
        email: str,
        password: str,
        transport: Transport = "cookie",
        ip_address: str | None = None,
        user_agent: str | None = None,
        scopes: t.Iterable[str] = (),
    ) -> tuple[User, IdentitySession, TokenPair]:
        """
        Autentica con credencial local y crea la sesión.

        **El orden de los chequeos es deliberado y es la parte que importa.** Primero se resuelve
        la credencial y se verifica la contraseña —hasheando un señuelo si no hay fila— y sólo
        **después** se chequea si la cuenta está bloqueada o el mail sin verificar. Al revés,
        responder "email no verificado" antes de validar la contraseña le confirma al atacante
        que el mail existe y que la contraseña que probó era correcta.

        Raises:
            InvalidCredentialsError: mail inexistente o contraseña incorrecta. **El mismo error
                para los dos**, y con el mismo tiempo de respuesta.
            AccountLockedError, EmailNotVerifiedError: sólo tras validar la contraseña.
        """
        normalizado = Email(value=email).value
        usuario = await self._users.get_by_email(normalizado)

        credencial = (
            await self._accounts.get_credential(usuario.id) if usuario else None
        )

        if usuario is None or credencial is None or credencial.password is None:
            # Igualá el tiempo antes de fallar. Sin esto, un mail inexistente responde en
            # microsegundos y uno real en decenas de milisegundos.
            self._hasher.hash_dummy()
            await self._publicar(
                UserSignInFailedEvent(
                    subject_user_id=usuario.id if usuario else None,
                    email=None,
                    reason="unknown-account",
                )
            )
            raise InvalidCredentialsError()

        if not self._hasher.verify(password, credencial.password):
            await self._publicar(
                UserSignInFailedEvent(
                    actor_user_id=usuario.id,
                    subject_user_id=usuario.id,
                    reason="bad-password",
                )
            )
            raise InvalidCredentialsError()

        # Recién acá, con la contraseña ya validada, se puede decir algo específico sin filtrar.
        ahora = self._clock.now()
        if usuario.is_locked_at(ahora):
            raise AccountLockedError(
                "La cuenta está bloqueada temporalmente. Intentá más tarde."
            )
        if self._config.require_verified_email and not usuario.email_verified:
            raise EmailNotVerifiedError(
                "Verificá tu dirección de correo antes de iniciar sesión."
            )

        # Migración transparente de algoritmo: si el hash es viejo, se regenera aprovechando
        # que acá tenemos la contraseña en claro. Es el único momento en que se puede.
        if self._hasher.needs_rehash(credencial.password):
            await self._accounts.update(
                credencial.model_copy(update={"password": self._hasher.hash(password)})
            )

        # ── El punto de extensión del sign-in ─────────────────────────────────
        # Acá la contraseña ya se validó y la sesión **todavía no existe**, que es el único
        # lugar donde un segundo factor puede exigirse: antes no se sabe quién es el usuario,
        # y después ya hay un par de tokens emitido que habría que revocar.
        #
        # Corre acá y no en el `HookMiddleware` porque el router llama a este servicio
        # **directo**, sin pasar por el bus: un plugin enganchado sólo al bus no vería nunca
        # un sign-in por HTTP.
        await run_hooks(self._plugins, SIGN_IN_AUTHENTICATED, "before", usuario)

        sesion, par = await self._sessions.create(
            actor=usuario,
            transport=transport,
            ip_address=ip_address,
            user_agent=user_agent,
            scopes=scopes,
        )

        await self._publicar(
            UserSignedInEvent(
                actor_user_id=usuario.id,
                subject_user_id=usuario.id,
                transport=transport,
            )
        )
        return usuario, sesion, par

    # ── Contraseña ────────────────────────────────────────────────────────────
    async def change_password(
        self, *, user_id: UUID, current_password: str, new_password: str
    ) -> None:
        """
        Cambia la contraseña y **revoca todas las sesiones**.

        Revocar no es opcional: si un atacante tenía una sesión abierta, cambiar la contraseña
        sin cortarla no lo echa. Es el flujo que la víctima ejecuta justamente para echarlo.

        Raises:
            InvalidCredentialsError: si la contraseña actual no coincide.
            ValueError: si la nueva no cumple la política.
        """
        self._config.passwords.validate_password(new_password)

        credencial = await self._accounts.get_credential(user_id)
        if credencial is None or credencial.password is None:
            raise InvalidCredentialsError()
        if not self._hasher.verify(current_password, credencial.password):
            raise InvalidCredentialsError()

        await self._accounts.update(
            credencial.model_copy(update={"password": self._hasher.hash(new_password)})
        )
        await self._sessions.revoke_all_for(user_id, reason="password-changed")

        from hexcore.darwin.domain.events import UserPasswordChangedEvent

        await self._publicar(
            UserPasswordChangedEvent(actor_user_id=user_id, subject_user_id=user_id)
        )

    async def issue_verification(
        self, *, email: str, purpose: VerificationPurpose
    ) -> str:
        """
        Emite un código nuevo, invalidando los pendientes del mismo propósito.

        Invalidar los anteriores no es limpieza: sin eso, cincuenta clicks en "reenviar" dejan
        cincuenta códigos válidos y el espacio a adivinar se multiplica por cincuenta.
        """
        from hexcore.darwin.infrastructure.hashing import (
            generate_numeric_code,
            hash_token,
        )

        normalizado = Email(value=email).value
        ahora = self._clock.now()

        await self._verifications.invalidate_for(normalizado, purpose, at=ahora)

        codigo = generate_numeric_code(6)
        await self._verifications.add(
            Verification(
                identifier=normalizado,
                value_hash=hash_token(codigo),
                purpose=purpose,
                expires_at=ahora + timedelta(hours=1),
            )
        )
        return codigo

    async def _publicar(self, evento: t.Any) -> None:
        if self._events is not None:
            await self._events.publish(evento)
