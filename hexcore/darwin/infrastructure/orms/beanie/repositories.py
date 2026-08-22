"""
Adaptadores Beanie de los puertos de identidad. Requiere el extra `[darwin-beanie]`.

**Toda la traducción interesante está en las tres operaciones atómicas.** En SQL eran
``UPDATE ... WHERE ... RETURNING``; acá son ``findOneAndUpdate``, que Mongo garantiza atómico
**por documento**. La condición va en el filtro, igual que allá iba en el `WHERE`:

| Operación | SQL | Mongo |
| :-- | :-- | :-- |
| `consume_for_rotation` | `UPDATE … WHERE consumed_at IS NULL RETURNING` | `find_one({consumed_at: None}).update(...)` |
| `consume` (verificación) | idem, filtrando por `purpose` | idem |
| `bump_token_generation` | `UPDATE … SET gen = gen + 1 RETURNING` | `$inc` en un `findOneAndUpdate` |

Y las tres siguen siendo **una sola operación**. Es lo que hace que la detección de reuso —el único
mecanismo que detecta un refresh robado— dispare bajo concurrencia. Con leer-y-después-escribir, dos
peticiones con el mismo token pasan las dos, y en Mongo eso es más fácil de escribir por accidente
que en SQL: `doc = await X.find_one(...)`, `doc.consumed_at = ahora`, `await doc.save()` se lee
natural y es exactamente el bug.

⚠️ **No hay `session_scope` ni transacciones acá.** Cada operación es una sentencia contra un
documento, así que no hace falta: el análogo de la transacción de SQL es la atomicidad del
`findOneAndUpdate`. La única operación de Darwin que necesitaría más de un documento a la vez es la
invariante del último `owner` de `organization`, y ese caso lo trata su propio adaptador.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID

from beanie.odm.queries.update import UpdateResponse

from hexcore.darwin.domain.entities import Account, IdentitySession, User, Verification
from hexcore.darwin.domain.ports import (
    AbstractAccountRepository,
    AbstractAuditSink,
    AbstractSessionRepository,
    AbstractUserRepository,
    AbstractVerificationRepository,
)
from hexcore.darwin.domain.value_objects import VerificationPurpose

__all__ = [
    "to_utc",
    "BeanieUserRepository",
    "BeanieSessionRepository",
    "BeanieAccountRepository",
    "BeanieVerificationRepository",
    "BeanieAuditSink",
    "UserRepository",
    "SessionRepository",
    "AccountRepository",
    "VerificationRepository",
    "AuditSink",
]


def to_utc(valor: datetime | None) -> datetime | None:
    """
    Normaliza a UTC-aware. **Público**: lo usan los repositorios de los plugins.

    Empezó como `_aware`, privado, y pyright lo señaló con `reportPrivateUsage` en los cinco
    módulos que lo importaban. Tenía razón: un helper que cruza módulos es parte de la API interna
    del backend, y llamarlo privado sólo hacía que cada uso pareciera una violación.

    Mongo guarda los `datetime` como BSON UTC y los devuelve **naive**, así que comparar con un
    `datetime.now(UTC)` levanta `TypeError`. Es el mismo problema que SQLite en el otro backend, y
    se resuelve igual: normalizando al hidratar y no en cada comparación.
    """
    if valor is None:
        return None
    return valor if valor.tzinfo is not None else valor.replace(tzinfo=UTC)


class _BaseBeanieRepository:
    """
    Base común. El documento es inyectable, igual que el modelo en el backend de SQL.

    No hay `session_scope`: ver el docstring del módulo.
    """

    #: `t.Any` por lo mismo que `_model` en SQLAlchemy: la clase es inyectable —el consumidor
    #: puede subclasear el documento de usuario— así que su tipo concreto no se conoce
    #: estáticamente. El contrato lo garantizan los puertos `Abstract*`.
    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or self._documento_por_defecto()

    @staticmethod
    def _documento_por_defecto() -> type:  # pragma: no cover - lo define cada subclase
        raise NotImplementedError


# ── Usuarios ──────────────────────────────────────────────────────────────────
class BeanieUserRepository(_BaseBeanieRepository, AbstractUserRepository):
    """`AbstractUserRepository` sobre Beanie."""

    def __init__(
        self, *, document: type | None = None, model: type | None = None
    ) -> None:
        # `model=` se acepta como alias de `document=` para que el contenedor pueda pasar
        # `IdentityConfig.user_model` sin saber en qué backend está. Es la única concesión de
        # nombre del adaptador, y evita un `if backend == ...` en el contenedor.
        super().__init__(document=document or model)

    @staticmethod
    def _documento_por_defecto() -> type:
        from hexcore.darwin.infrastructure.orms.beanie.documents import UserDocument

        return UserDocument

    async def get_by_id(self, user_id: UUID) -> User | None:
        doc = await self._doc.find_one(self._doc.entity_id == user_id)
        return _a_usuario(doc) if doc is not None else None

    async def get_by_email(self, email: str) -> User | None:
        """`email` ya viene normalizado por `Email`; no se vuelve a normalizar acá."""
        doc = await self._doc.find_one(self._doc.email == email)
        return _a_usuario(doc) if doc is not None else None

    async def add(self, user: User) -> User:
        doc = self._doc(
            entity_id=user.id,
            email=user.email,
            email_verified=user.email_verified,
            name=user.name,
            image=user.image,
            token_generation=user.token_generation,
            locked_until=user.locked_until,
            is_active=user.is_active,
            extra=dict(user.extra),
            created_at=user.created_at,
            updated_at=user.updated_at,
        )
        await doc.insert()
        return _a_usuario(doc)

    async def update(self, user: User) -> User:
        actualizado = await self._doc.find_one(
            self._doc.entity_id == user.id
        ).update(
            {
                "$set": {
                    "email": user.email,
                    "email_verified": user.email_verified,
                    "name": user.name,
                    "image": user.image,
                    "token_generation": user.token_generation,
                    "locked_until": user.locked_until,
                    "is_active": user.is_active,
                    "extra": dict(user.extra),
                    "updated_at": datetime.now(UTC),
                }
            },
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        if actualizado is None:
            raise KeyError(f"No existe el usuario {user.id}.")
        return _a_usuario(actualizado)

    async def bump_token_generation(self, user_id: UUID) -> int:
        """
        Sube la generación con **`$inc` en un `findOneAndUpdate`**.

        Es la capa 3 de la revocación. Leer el valor, sumarle uno y escribirlo dejaría que dos
        revocaciones masivas concurrentes suban una sola generación — y la mitad de los tokens
        seguiría valiendo. `$inc` lo resuelve en el servidor.
        """
        actualizado = await self._doc.find_one(
            self._doc.entity_id == user_id
        ).update(
            {"$inc": {"token_generation": 1}, "$set": {"updated_at": datetime.now(UTC)}},
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return int(actualizado.token_generation) if actualizado is not None else 0


# ── Sesiones ──────────────────────────────────────────────────────────────────
class BeanieSessionRepository(_BaseBeanieRepository, AbstractSessionRepository):
    """`AbstractSessionRepository` sobre Beanie."""

    @staticmethod
    def _documento_por_defecto() -> type:
        from hexcore.darwin.infrastructure.orms.beanie.documents import SessionDocument

        return SessionDocument

    async def get(self, session_id: UUID) -> IdentitySession | None:
        doc = await self._doc.find_one(self._doc.entity_id == session_id)
        return _a_sesion(doc) if doc is not None else None

    async def get_by_token_hash(self, token_hash: str) -> IdentitySession | None:
        doc = await self._doc.find_one(self._doc.token_hash == token_hash)
        return _a_sesion(doc) if doc is not None else None

    async def add(self, identity_session: IdentitySession) -> IdentitySession:
        doc = self._doc(
            entity_id=identity_session.id,
            actor_user_id=identity_session.actor_user_id,
            subject_user_id=identity_session.subject_user_id,
            token_hash=identity_session.token_hash,
            family_id=identity_session.family_id,
            transport=identity_session.transport,
            expires_at=identity_session.expires_at,
            revoked_at=identity_session.revoked_at,
            consumed_at=identity_session.consumed_at,
            ip_address=identity_session.ip_address,
            user_agent=identity_session.user_agent,
            impersonation_reason=identity_session.impersonation_reason,
            impersonation_granted_by=identity_session.impersonation_granted_by,
            impersonation_expires_at=identity_session.impersonation_expires_at,
            is_active=identity_session.is_active,
            created_at=identity_session.created_at,
            updated_at=identity_session.updated_at,
        )
        await doc.insert()
        return _a_sesion(doc)

    async def revoke(self, session_id: UUID, *, at: datetime, reason: str) -> None:
        """
        Marca la sesión revocada. El `reason` va al evento y a `audit_log`, no al documento.

        `del reason` explícito para que quede claro que no se descarta por olvido: la fila no
        tiene columna de motivo en ninguno de los dos backends.
        """
        del reason
        await self._doc.find_one(self._doc.entity_id == session_id).update(
            {"$set": {"revoked_at": at, "updated_at": datetime.now(UTC)}}
        )

    async def revoke_family(
        self, family_id: UUID, *, at: datetime, reason: str
    ) -> int:
        """
        Revoca la familia entera. Es lo que dispara la detección de reuso.

        Un `update_many` y no un bucle de `findOneAndUpdate`: son N documentos y el resultado que
        importa es cuántos se tocaron, no cuáles.
        """
        del reason
        resultado = await self._doc.find(
            self._doc.family_id == family_id,
            self._doc.revoked_at == None,  # noqa: E711  — Beanie traduce el `==` a un filtro
        ).update({"$set": {"revoked_at": at, "updated_at": datetime.now(UTC)}})
        return int(getattr(resultado, "modified_count", 0) or 0)

    async def consume_for_rotation(
        self, session_id: UUID, *, at: datetime
    ) -> IdentitySession | None:
        """
        Consume la sesión para rotar, en **un solo `findOneAndUpdate`**.

        ⚠️ El `consumed_at: None` va **en el filtro**. Con leer-comprobar-escribir, dos rotaciones
        concurrentes con el mismo token pasan las dos y la detección de reuso no dispara nunca —
        y en Mongo esa versión es más fácil de escribir por accidente que en SQL.

        `None` si ya estaba consumida.
        """
        doc = await self._doc.find_one(
            self._doc.entity_id == session_id,
            self._doc.consumed_at == None,  # noqa: E711
        ).update(
            {"$set": {"consumed_at": at, "updated_at": datetime.now(UTC)}},
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return _a_sesion(doc) if doc is not None else None

    async def list_active_for_user(self, user_id: UUID) -> list[IdentitySession]:
        docs = await self._doc.find(
            self._doc.actor_user_id == user_id,
            self._doc.revoked_at == None,  # noqa: E711
        ).to_list()
        return [_a_sesion(d) for d in docs]

    async def delete_expired(self, *, before: datetime) -> int:
        """
        Borra las vencidas.

        En Mongo esto es una **red de contención** y no el mecanismo principal: la colección tiene
        un índice TTL sobre `expires_at`, así que el servidor las borra solo. Se mantiene porque el
        barrido del TTL corre cada 60 s y un test necesita determinismo.
        """
        resultado = await self._doc.find(self._doc.expires_at < before).delete()
        return int(getattr(resultado, "deleted_count", 0) or 0)


# ── Cuentas ───────────────────────────────────────────────────────────────────
class BeanieAccountRepository(_BaseBeanieRepository, AbstractAccountRepository):
    """`AbstractAccountRepository` sobre Beanie."""

    @staticmethod
    def _documento_por_defecto() -> type:
        from hexcore.darwin.infrastructure.orms.beanie.documents import AccountDocument

        return AccountDocument

    async def get_by_provider(
        self, provider_id: str, account_id: str
    ) -> Account | None:
        doc = await self._doc.find_one(
            self._doc.provider_id == provider_id,
            self._doc.account_id == account_id,
        )
        return _a_cuenta(doc) if doc is not None else None

    async def get_credential(self, user_id: UUID) -> Account | None:
        """La cuenta del provider `credential`, que es donde vive el hash de la contraseña."""
        from hexcore.darwin.domain.entities import CREDENTIAL_PROVIDER

        doc = await self._doc.find_one(
            self._doc.user_id == user_id,
            self._doc.provider_id == CREDENTIAL_PROVIDER,
        )
        return _a_cuenta(doc) if doc is not None else None

    async def list_for_user(self, user_id: UUID) -> list[Account]:
        docs = await self._doc.find(self._doc.user_id == user_id).to_list()
        return [_a_cuenta(d) for d in docs]

    async def add(self, account: Account) -> Account:
        doc = self._doc(**_campos_cuenta(account))
        await doc.insert()
        return _a_cuenta(doc)

    async def update(self, account: Account) -> Account:
        campos = _campos_cuenta(account)
        campos.pop("entity_id")
        campos["updated_at"] = datetime.now(UTC)

        doc = await self._doc.find_one(self._doc.entity_id == account.id).update(
            {"$set": campos}, response_type=UpdateResponse.NEW_DOCUMENT
        )
        if doc is None:
            raise KeyError(f"No existe la cuenta {account.id}.")
        return _a_cuenta(doc)

    async def delete(self, account_id: UUID) -> None:
        await self._doc.find_one(self._doc.entity_id == account_id).delete()


# ── Verificaciones ────────────────────────────────────────────────────────────
class BeanieVerificationRepository(
    _BaseBeanieRepository, AbstractVerificationRepository
):
    """`AbstractVerificationRepository` sobre Beanie."""

    @staticmethod
    def _documento_por_defecto() -> type:
        from hexcore.darwin.infrastructure.orms.beanie.documents import (
            VerificationDocument,
        )

        return VerificationDocument

    async def add(self, verification: Verification) -> Verification:
        doc = self._doc(
            entity_id=verification.id,
            identifier=verification.identifier,
            value_hash=verification.value_hash,
            purpose=verification.purpose,
            expires_at=verification.expires_at,
            consumed_at=verification.consumed_at,
            attempts=verification.attempts,
            is_active=verification.is_active,
            created_at=verification.created_at,
            updated_at=verification.updated_at,
        )
        await doc.insert()
        return _a_verificacion(doc)

    async def consume(
        self,
        identifier: str,
        purpose: VerificationPurpose,
        value_hash: str,
        *,
        at: datetime,
    ) -> Verification | None:
        """
        Canjea el token en **un solo `findOneAndUpdate`**.

        Los cuatro filtros van en la consulta: identificador, propósito, hash y `consumed_at: None`,
        más el vencimiento. Filtrar por `purpose` es lo que impide canjear un código de reset de
        contraseña en el flujo de verificar el mail.
        """
        doc = await self._doc.find_one(
            self._doc.identifier == identifier,
            self._doc.purpose == purpose,
            self._doc.value_hash == value_hash,
            self._doc.consumed_at == None,  # noqa: E711
            self._doc.expires_at > at,
        ).update(
            {"$set": {"consumed_at": at, "updated_at": datetime.now(UTC)}},
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return _a_verificacion(doc) if doc is not None else None

    async def increment_attempts(self, verification_id: UUID) -> int:
        doc = await self._doc.find_one(
            self._doc.entity_id == verification_id
        ).update(
            {"$inc": {"attempts": 1}, "$set": {"updated_at": datetime.now(UTC)}},
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return int(doc.attempts) if doc is not None else 0

    async def invalidate_for(
        self, identifier: str, purpose: VerificationPurpose, *, at: datetime
    ) -> int:
        """
        Invalida los pendientes de ese identificador y propósito.

        Se llama al emitir uno nuevo: sin esto, cincuenta clicks en "reenviar" dejan cincuenta
        tokens válidos y el espacio a adivinar se multiplica por cincuenta.
        """
        resultado = await self._doc.find(
            self._doc.identifier == identifier,
            self._doc.purpose == purpose,
            self._doc.consumed_at == None,  # noqa: E711
        ).update({"$set": {"consumed_at": at, "updated_at": datetime.now(UTC)}})
        return int(getattr(resultado, "modified_count", 0) or 0)

    async def delete_expired(self, *, before: datetime) -> int:
        """Igual que en sesiones: red de contención sobre el índice TTL."""
        resultado = await self._doc.find(self._doc.expires_at < before).delete()
        return int(getattr(resultado, "deleted_count", 0) or 0)


# ── Auditoría ─────────────────────────────────────────────────────────────────
class BeanieAuditSink(_BaseBeanieRepository, AbstractAuditSink):
    """
    `AbstractAuditSink` sobre Beanie.

    ⚠️ **Acá hay una diferencia real con el backend de SQL, y hay que decirla.** El sink de
    SQLAlchemy escribe en la **misma transacción** que el cambio que registra, así que no existe la
    ventana donde la acción ocurrió y el registro no. En Mongo, sin una transacción multi-documento,
    esa garantía no está: el `insert` de la auditoría es una operación aparte.

    Qué significa en la práctica: si el proceso muere entre el cambio y el registro, queda un cambio
    sin auditar. Para tener la garantía hace falta un replica set y envolver los dos en una
    transacción, que es una decisión de topología del consumidor — no algo que el framework pueda
    asumir. Está documentado y no escondido, porque quien necesita auditoría transaccional
    necesita saberlo antes de elegir el backend.
    """

    @staticmethod
    def _documento_por_defecto() -> type:
        from hexcore.darwin.infrastructure.orms.beanie.documents import AuditLogDocument

        return AuditLogDocument

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
        doc = self._doc(
            action=action,
            actor_id=str(actor_id) if actor_id is not None else None,
            subject_id=str(subject_id) if subject_id is not None else None,
            impersonated=impersonated,
            request_id=request_id,
            audit_metadata=dict(metadata or {}),
        )
        await doc.insert()


# ── Mapeo documento → entidad ─────────────────────────────────────────────────
def _a_usuario(doc: t.Any) -> User:
    return User(
        id=doc.entity_id,
        email=doc.email,
        email_verified=doc.email_verified,
        name=doc.name,
        image=doc.image,
        token_generation=doc.token_generation,
        locked_until=to_utc(doc.locked_until),
        is_active=doc.is_active,
        extra=dict(doc.extra or {}),
        created_at=to_utc(doc.created_at) or datetime.now(UTC),
        updated_at=to_utc(doc.updated_at) or datetime.now(UTC),
    )


def _a_sesion(doc: t.Any) -> IdentitySession:
    return IdentitySession(
        id=doc.entity_id,
        actor_user_id=doc.actor_user_id,
        subject_user_id=doc.subject_user_id,
        token_hash=doc.token_hash,
        family_id=doc.family_id,
        transport=doc.transport,
        expires_at=to_utc(doc.expires_at) or datetime.now(UTC),
        revoked_at=to_utc(doc.revoked_at),
        consumed_at=to_utc(doc.consumed_at),
        ip_address=doc.ip_address,
        user_agent=doc.user_agent,
        impersonation_reason=doc.impersonation_reason,
        impersonation_granted_by=doc.impersonation_granted_by,
        impersonation_expires_at=to_utc(doc.impersonation_expires_at),
        is_active=doc.is_active,
        created_at=to_utc(doc.created_at) or datetime.now(UTC),
        updated_at=to_utc(doc.updated_at) or datetime.now(UTC),
    )


def _campos_cuenta(account: Account) -> dict[str, t.Any]:
    return {
        "entity_id": account.id,
        "user_id": account.user_id,
        "provider_id": account.provider_id,
        "account_id": account.account_id,
        "password": account.password,
        "access_token": account.access_token,
        "refresh_token": account.refresh_token,
        "id_token": account.id_token,
        "scope": account.scope,
        "access_token_expires_at": account.access_token_expires_at,
        "refresh_token_expires_at": account.refresh_token_expires_at,
        "is_active": account.is_active,
    }


def _a_cuenta(doc: t.Any) -> Account:
    return Account(
        id=doc.entity_id,
        user_id=doc.user_id,
        provider_id=doc.provider_id,
        account_id=doc.account_id,
        password=doc.password,
        access_token=doc.access_token,
        refresh_token=doc.refresh_token,
        id_token=doc.id_token,
        scope=doc.scope,
        access_token_expires_at=to_utc(doc.access_token_expires_at),
        refresh_token_expires_at=to_utc(doc.refresh_token_expires_at),
        is_active=doc.is_active,
        created_at=to_utc(doc.created_at) or datetime.now(UTC),
        updated_at=to_utc(doc.updated_at) or datetime.now(UTC),
    )


def _a_verificacion(doc: t.Any) -> Verification:
    return Verification(
        id=doc.entity_id,
        identifier=doc.identifier,
        value_hash=doc.value_hash,
        purpose=doc.purpose,
        expires_at=to_utc(doc.expires_at) or datetime.now(UTC),
        consumed_at=to_utc(doc.consumed_at),
        attempts=doc.attempts,
        is_active=doc.is_active,
        created_at=to_utc(doc.created_at) or datetime.now(UTC),
        updated_at=to_utc(doc.updated_at) or datetime.now(UTC),
    )


# ── El contrato del backend ───────────────────────────────────────────────────
#
# Los cinco nombres neutros que el contenedor busca. Ver el bloque equivalente en el backend de
# SQLAlchemy: hay un test que verifica que los dos los expongan.
UserRepository = BeanieUserRepository
SessionRepository = BeanieSessionRepository
AccountRepository = BeanieAccountRepository
VerificationRepository = BeanieVerificationRepository
AuditSink = BeanieAuditSink
