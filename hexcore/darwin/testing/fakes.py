"""
Dobles en memoria de los puertos de Darwin.

**Para qué existen.** Los tests de Darwin del propio framework corren contra SQLite: es lo
correcto ahí, porque lo que se prueba incluye la atomicidad de las sentencias. Un consumidor no
está probando eso — está probando *su* caso de uso, que además pasa por auth. Para eso, levantar un
motor, crear seis tablas y borrarlas es tiempo y ceremonia por nada.

Con estos fakes, cablear Darwin completo es una llamada y no toca disco.

Las tres decisiones que importan:

1. **Guardan copias**, igual que `hexcore.testing.FakeRepository`: mutar la entidad después de
   guardarla no cambia lo guardado. Sin eso, un test pasa por una aliasing que en producción no
   existe.
2. **Las operaciones que la seguridad exige atómicas siguen siendo de un solo paso**: `consume`,
   `consume_for_rotation` y `bump_token_generation` chequean y escriben sin ceder el control. Un
   fake que las parte en dos haría que los tests de replay pasen y el código real falle.
3. **No hay ningún `assert` de conveniencia**: si un flujo real lanzaría, el fake lanza. Un doble
   más permisivo que la implementación deja pasar bugs que después aparecen en producción.
"""
from __future__ import annotations

import threading
import typing as t
from datetime import UTC, datetime
from uuid import UUID

from hexcore.darwin.domain.entities import CREDENTIAL_PROVIDER, Account, IdentitySession, User, Verification
from hexcore.darwin.domain.ports import (
    AbstractAccountRepository,
    AbstractAuditSink,
    AbstractPasswordHasher,
    AbstractRevocationList,
    AbstractSessionRepository,
    AbstractUserRepository,
    AbstractVerificationRepository,
)
from hexcore.darwin.domain.value_objects import VerificationPurpose

__all__ = [
    "FakeUserRepository",
    "FakeSessionRepository",
    "FakeAccountRepository",
    "FakeVerificationRepository",
    "FakeRevocationList",
    "RecordingAuditSink",
    "PlainTextHasher",
    "AuditRecord",
]

TEntidad = t.TypeVar("TEntidad", User, IdentitySession, Account, Verification)


def _copiar(entidad: TEntidad) -> TEntidad:
    """Copia profunda. Ver el punto 1 del docstring del módulo."""
    return entidad.model_copy(deep=True)


# ── Hasher ────────────────────────────────────────────────────────────────────
class PlainTextHasher(AbstractPasswordHasher):
    """
    Hasher que **no hashea**: prefija la contraseña con `plain$`.

    ⚠️ **Sólo para tests, y por una razón concreta**: Argon2id tarda ~100 ms a propósito, así que
    una suite con cincuenta sign-ins paga cinco segundos en KDF. Este doble los baja a cero.

    Nunca lo cablees fuera de un test. El prefijo existe para que un `grep 'plain$'` en un dump
    encuentre inmediatamente si alguien lo hizo.

    Uso::

        contenedor = configure_identity(config, hasher=PlainTextHasher())
    """

    #: Cuántas veces se llamó a `hash_dummy`. Es lo que permite aseverar que el camino de "mail
    #: inexistente" iguala el tiempo — el chequeo que evita la enumeración de usuarios.
    dummy_calls: int

    def __init__(self) -> None:
        self.dummy_calls = 0
        self.hashed: list[str] = []

    def hash(self, password: str) -> str:
        self.hashed.append(password)
        return f"plain${password}"

    def verify(self, password: str, hashed: str) -> bool:
        return hashed == f"plain${password}"

    def needs_rehash(self, hashed: str) -> bool:
        """
        Siempre `False`.

        Devolver `True` haría que cada sign-in reescriba la credencial, y un test que cuenta
        escrituras vería una de más sin motivo.
        """
        return False

    def hash_dummy(self) -> None:
        self.dummy_calls += 1


# ── Usuarios ──────────────────────────────────────────────────────────────────
class FakeUserRepository(AbstractUserRepository):
    """
    Usuarios en memoria, indexados por id y por mail.

    El índice por mail se mantiene en paralelo y no se recorre la lista: es la consulta del
    sign-in, y un test con doscientos usuarios sembrados no debería ser cuadrático.

    Uso::

        usuarios = FakeUserRepository([User(email="ana@ejemplo.com", email_verified=True)])
        contenedor = configure_identity(config, users=usuarios)
    """

    def __init__(self, users: t.Iterable[User] = ()) -> None:
        self._por_id: dict[UUID, User] = {}
        self._por_email: dict[str, UUID] = {}
        self._lock = threading.RLock()
        for usuario in users:
            self._guardar(usuario)

    def _guardar(self, usuario: User) -> User:
        copia = _copiar(usuario)
        with self._lock:
            anterior = self._por_id.get(copia.id)
            if anterior is not None and anterior.email != copia.email:
                # Un cambio de mail tiene que sacar la entrada vieja del índice: si no, el mail
                # anterior seguiría resolviendo al usuario y un test de "cambié mi mail" pasaría
                # con las dos direcciones funcionando.
                self._por_email.pop(anterior.email, None)
            self._por_id[copia.id] = copia
            self._por_email[copia.email] = copia.id
        return _copiar(copia)

    async def get_by_id(self, user_id: UUID) -> User | None:
        with self._lock:
            fila = self._por_id.get(user_id)
        return _copiar(fila) if fila is not None else None

    async def get_by_email(self, email: str) -> User | None:
        """`email` ya viene normalizado por `Email`; no se vuelve a normalizar acá."""
        with self._lock:
            user_id = self._por_email.get(email)
            fila = self._por_id.get(user_id) if user_id is not None else None
        return _copiar(fila) if fila is not None else None

    async def add(self, user: User) -> User:
        with self._lock:
            if user.email in self._por_email:
                from hexcore.darwin.domain.exceptions import (
                    EmailAlreadyRegisteredError,
                )

                # El repositorio real lo rechaza por el `UNIQUE`. Dejarlo pasar acá haría que un
                # test de "no se puede registrar dos veces" pase sin probar nada.
                raise EmailAlreadyRegisteredError(
                    "Ya existe una cuenta con ese correo."
                )
        return self._guardar(user)

    async def update(self, user: User) -> User:
        return self._guardar(user)

    async def bump_token_generation(self, user_id: UUID) -> int:
        """
        Sube la generación **en un solo paso**, sin ceder el control.

        Es la capa 3 de la revocación: leer, sumar y escribir con un `await` en el medio dejaría
        que dos revocaciones masivas concurrentes suban una sola generación, y la mitad de los
        tokens seguiría valiendo.
        """
        with self._lock:
            usuario = self._por_id.get(user_id)
            if usuario is None:
                return 0
            nueva = usuario.token_generation + 1
            self._por_id[user_id] = usuario.model_copy(
                update={"token_generation": nueva}
            )
            return nueva


# ── Sesiones ──────────────────────────────────────────────────────────────────
class FakeSessionRepository(AbstractSessionRepository):
    """
    Sesiones en memoria, con la atomicidad que la rotación de refresh necesita.

    Uso::

        contenedor = configure_identity(config, sessions=FakeSessionRepository())
    """

    def __init__(self, sessions: t.Iterable[IdentitySession] = ()) -> None:
        self._por_id: dict[UUID, IdentitySession] = {}
        self._por_hash: dict[str, UUID] = {}
        self._lock = threading.RLock()

        #: Por qué se revocó cada sesión. Ver `revoke`.
        self.reasons: dict[UUID, str] = {}

        for sesion in sessions:
            self._guardar(sesion)

    def _guardar(self, sesion: IdentitySession) -> IdentitySession:
        copia = _copiar(sesion)
        with self._lock:
            self._por_id[copia.id] = copia
            self._por_hash[copia.token_hash] = copia.id
        return _copiar(copia)

    async def get(self, session_id: UUID) -> IdentitySession | None:
        with self._lock:
            fila = self._por_id.get(session_id)
        return _copiar(fila) if fila is not None else None

    async def get_by_token_hash(self, token_hash: str) -> IdentitySession | None:
        with self._lock:
            sid = self._por_hash.get(token_hash)
            fila = self._por_id.get(sid) if sid is not None else None
        return _copiar(fila) if fila is not None else None

    async def add(self, identity_session: IdentitySession) -> IdentitySession:
        return self._guardar(identity_session)

    async def revoke(self, session_id: UUID, *, at: datetime, reason: str) -> None:
        """
        Marca la sesión revocada. El `reason` se registra acá y no en la fila.

        La entidad no tiene columna de motivo —vive en `audit_log` y en el evento
        `SessionRevokedEvent`— así que el fake lo guarda en `reasons` para que un test pueda
        aseverar *por qué* se revocó sin cablear un sink.
        """
        with self._lock:
            sesion = self._por_id.get(session_id)
            if sesion is not None:
                self._por_id[session_id] = sesion.model_copy(
                    update={"revoked_at": at}
                )
                self.reasons[session_id] = reason

    async def revoke_family(
        self, family_id: UUID, *, at: datetime, reason: str
    ) -> int:
        """Revoca la familia entera. Es lo que dispara la detección de reuso."""
        revocadas = 0
        with self._lock:
            for sid, sesion in list(self._por_id.items()):
                if sesion.family_id == family_id and sesion.revoked_at is None:
                    self._por_id[sid] = sesion.model_copy(update={"revoked_at": at})
                    revocadas += 1
        return revocadas

    async def consume_for_rotation(
        self, session_id: UUID, *, at: datetime
    ) -> IdentitySession | None:
        """
        Consume la sesión para rotar, **en un solo paso**.

        `None` si ya estaba consumida. Sin la atomicidad, dos rotaciones concurrentes con el
        mismo token pasarían las dos y la detección de reuso —el único mecanismo que detecta un
        refresh robado— no dispararía nunca.
        """
        with self._lock:
            sesion = self._por_id.get(session_id)
            if sesion is None or sesion.consumed_at is not None:
                return None
            consumida = sesion.model_copy(update={"consumed_at": at})
            self._por_id[session_id] = consumida
            return _copiar(consumida)

    async def list_active_for_user(self, user_id: UUID) -> list[IdentitySession]:
        with self._lock:
            return [
                _copiar(s)
                for s in self._por_id.values()
                if s.actor_user_id == user_id and s.revoked_at is None
            ]

    async def delete_expired(self, *, before: datetime) -> int:
        with self._lock:
            vencidas = [
                sid for sid, s in self._por_id.items() if s.expires_at < before
            ]
            for sid in vencidas:
                sesion = self._por_id.pop(sid)
                self._por_hash.pop(sesion.token_hash, None)
            return len(vencidas)


# ── Cuentas ───────────────────────────────────────────────────────────────────
class FakeAccountRepository(AbstractAccountRepository):
    """
    Cuentas en memoria: la credencial local y las de OAuth.

    Uso::

        contenedor = configure_identity(config, accounts=FakeAccountRepository())
    """

    def __init__(self, accounts: t.Iterable[Account] = ()) -> None:
        self._por_id: dict[UUID, Account] = {
            c.id: _copiar(c) for c in accounts
        }
        self._lock = threading.RLock()

    async def get_by_provider(
        self, provider_id: str, account_id: str
    ) -> Account | None:
        with self._lock:
            for cuenta in self._por_id.values():
                if (
                    cuenta.provider_id == provider_id
                    and cuenta.account_id == account_id
                ):
                    return _copiar(cuenta)
        return None

    async def get_credential(self, user_id: UUID) -> Account | None:
        """La cuenta del provider `credential`, que es donde vive el hash de la contraseña."""
        with self._lock:
            for cuenta in self._por_id.values():
                if (
                    cuenta.user_id == user_id
                    and cuenta.provider_id == CREDENTIAL_PROVIDER
                ):
                    return _copiar(cuenta)
        return None

    async def list_for_user(self, user_id: UUID) -> list[Account]:
        with self._lock:
            return [
                _copiar(c) for c in self._por_id.values() if c.user_id == user_id
            ]

    async def add(self, account: Account) -> Account:
        with self._lock:
            self._por_id[account.id] = _copiar(account)
        return _copiar(account)

    async def update(self, account: Account) -> Account:
        with self._lock:
            self._por_id[account.id] = _copiar(account)
        return _copiar(account)

    async def delete(self, account_id: UUID) -> None:
        with self._lock:
            self._por_id.pop(account_id, None)


# ── Verificaciones ────────────────────────────────────────────────────────────
class FakeVerificationRepository(AbstractVerificationRepository):
    """
    Tokens de un solo uso en memoria, con el canje atómico.

    Uso::

        contenedor = configure_identity(config, verifications=FakeVerificationRepository())
    """

    def __init__(self, verifications: t.Iterable[Verification] = ()) -> None:
        self._por_id: dict[UUID, Verification] = {
            v.id: _copiar(v) for v in verifications
        }
        self._lock = threading.RLock()

    async def add(self, verification: Verification) -> Verification:
        with self._lock:
            self._por_id[verification.id] = _copiar(verification)
        return _copiar(verification)

    async def consume(
        self,
        identifier: str,
        purpose: VerificationPurpose,
        value_hash: str,
        *,
        at: datetime,
    ) -> Verification | None:
        """
        Canjea el token, **en un solo paso**.

        Filtra por `purpose` además del identificador: un código de reset de contraseña no se
        puede canjear en el flujo de verificar el mail. Y no cede el control entre el chequeo y
        la escritura, así que de dos canjes concurrentes gana exactamente uno — igual que el
        `UPDATE ... RETURNING` real.
        """
        with self._lock:
            for vid, fila in self._por_id.items():
                if (
                    fila.identifier == identifier
                    and fila.purpose == purpose
                    and fila.value_hash == value_hash
                    and fila.consumed_at is None
                    and at < fila.expires_at
                ):
                    consumida = fila.model_copy(update={"consumed_at": at})
                    self._por_id[vid] = consumida
                    return _copiar(consumida)
        return None

    async def increment_attempts(self, verification_id: UUID) -> int:
        with self._lock:
            fila = self._por_id.get(verification_id)
            if fila is None:
                return 0
            nuevos = fila.attempts + 1
            self._por_id[verification_id] = fila.model_copy(
                update={"attempts": nuevos}
            )
            return nuevos

    async def invalidate_for(
        self, identifier: str, purpose: VerificationPurpose, *, at: datetime
    ) -> int:
        """
        Invalida los pendientes de ese identificador y propósito.

        Se llama al emitir uno nuevo: sin esto, cinco clicks en "reenviar" dejan cinco tokens
        válidos y el espacio a adivinar se multiplica por cinco.
        """
        invalidados = 0
        with self._lock:
            for vid, fila in list(self._por_id.items()):
                if (
                    fila.identifier == identifier
                    and fila.purpose == purpose
                    and fila.consumed_at is None
                ):
                    self._por_id[vid] = fila.model_copy(update={"consumed_at": at})
                    invalidados += 1
        return invalidados

    async def delete_expired(self, *, before: datetime) -> int:
        with self._lock:
            vencidos = [
                vid for vid, f in self._por_id.items() if f.expires_at < before
            ]
            for vid in vencidos:
                del self._por_id[vid]
            return len(vencidos)


# ── Revocación ────────────────────────────────────────────────────────────────
class FakeRevocationList(AbstractRevocationList):
    """
    Denylist de `sid` en memoria, **con vencimiento respetado**.

    Respetarlo importa: la implementación real guarda el vencimiento **dentro del valor** porque
    `MemoryCache.set()` ignora `expire` y nunca desaloja. Un fake que no venciera dejaría pasar un
    test donde una sesión revocada sigue en la lista para siempre, y el bug de la memoria que crece
    no aparecería.

    Args:
        clock: El reloj. Sin él usa `datetime.now(UTC)`, que hace que un test con `FixedClock` vea
            vencimientos incoherentes — pasalo siempre que uses un reloj controlado.

    Uso::

        reloj = FixedClock(AHORA)
        contenedor = configure_identity(
            config, clock=reloj, revocations=FakeRevocationList(clock=reloj)
        )
    """

    def __init__(self, *, clock: t.Any = None) -> None:
        self._clock = clock
        self._hasta: dict[UUID, datetime] = {}
        self._lock = threading.RLock()
        self.revoked: list[UUID] = []

    def _ahora(self) -> datetime:
        return self._clock.now() if self._clock is not None else datetime.now(UTC)

    async def revoke(self, session_id: UUID, *, until: datetime) -> None:
        with self._lock:
            self._hasta[session_id] = until
            self.revoked.append(session_id)

    async def is_revoked(self, session_id: UUID) -> bool:
        with self._lock:
            hasta = self._hasta.get(session_id)
            if hasta is None:
                return False
            if self._ahora() >= hasta:
                # Vencida: se saca, igual que el desalojo real.
                del self._hasta[session_id]
                return False
            return True


# ── Auditoría ─────────────────────────────────────────────────────────────────
class AuditRecord(t.NamedTuple):
    """Un registro de auditoría, tal como se lo pasaron al sink."""

    action: str
    actor_id: UUID | str | None
    subject_id: UUID | str | None
    impersonated: bool
    request_id: str | None
    metadata: dict[str, t.Any]


class RecordingAuditSink(AbstractAuditSink):
    """
    Sink de auditoría que guarda en una lista.

    Es el doble más útil del kit: la mitad de lo que Darwin promete —que toda impersonación queda
    registrada con los dos principales— sólo se puede aseverar mirando lo que llegó al sink.

    Uso::

        auditoria = RecordingAuditSink()
        contenedor = configure_identity(config, audit=auditoria)
        # ...
        assert auditoria.actions == ["impersonation.start"]
        assert auditoria.last.impersonated is True
    """

    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    async def record(
        self,
        *,
        action: str,
        actor_id: UUID | str | None,
        subject_id: UUID | str | None,
        impersonated: bool = False,
        request_id: str | None = None,
        metadata: t.Mapping[str, t.Any] | None = None,
    ) -> None:
        self.records.append(
            AuditRecord(
                action=action,
                actor_id=actor_id,
                subject_id=subject_id,
                impersonated=impersonated,
                request_id=request_id,
                metadata=dict(metadata or {}),
            )
        )

    @property
    def actions(self) -> list[str]:
        """Las acciones registradas, en orden. El assert más común."""
        return [r.action for r in self.records]

    @property
    def last(self) -> AuditRecord:
        """
        El último registro.

        Lanza `IndexError` si no hay ninguno, y eso es lo que se quiere: un test que asevera sobre
        `last` con la lista vacía tiene que fallar ahí y no en el atributo siguiente.
        """
        return self.records[-1]

    def for_action(self, action: str) -> list[AuditRecord]:
        """Los registros de una acción."""
        return [r for r in self.records if r.action == action]

    def clear(self) -> None:
        """Vacía la lista. Para aseverar sobre un tramo del test y no sobre todo."""
        self.records.clear()
