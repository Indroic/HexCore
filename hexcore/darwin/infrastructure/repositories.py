"""
Adaptadores SQLAlchemy de los puertos de identidad.

**Ninguno hereda `BaseSQLAlchemyRepository`.** Es la regla 2 de `cron_sql`, y acá el riesgo
es concreto: `_repository_key_from_class_name` mapea `UserRepository` a la clave ``user`` y
`_discover_repositories` **levanta `ValueError` ante una colisión**. Un repositorio de
identidad autodescubrible rompería el Unit of Work de todo consumidor que ya tenga el suyo —
que son prácticamente todos. Hay un test que declara un `UserRepository` de app y verifica
que el discovery lo devuelva sin levantar.

Cada operación abre su propia sesión con `session_scope()` en vez de guardar una, igual que
`SqlAlchemyCronJobRepository`: los flujos de auth los invoca un proceso de vida larga, y
sostener una sesión abierta es cómo se acumulan transacciones idle-in-transaction.
`session_scope` tampoco paga el auto-discovery de repositorios de dominio, que para estas
tablas no aporta nada.

Las operaciones que la seguridad exige atómicas —`consume_for_rotation`, `consume`,
`bump_token_generation`— se hacen con **una sola sentencia** ``UPDATE ... WHERE ...
RETURNING``. Con leer-y-después-escribir, dos peticiones concurrentes pasan las dos y la
detección de reuso de token, que es el único mecanismo que detecta un refresh robado, no
dispara nunca.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select, update

from hexcore.darwin.domain.entities import (
    CREDENTIAL_PROVIDER,
    Account,
    IdentitySession,
    User,
    Verification,
)
from hexcore.darwin.domain.ports import (
    AbstractAccountRepository,
    AbstractAuditSink,
    AbstractSessionRepository,
    AbstractUserRepository,
    AbstractVerificationRepository,
)
from hexcore.darwin.domain.value_objects import VerificationPurpose

if t.TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "SqlAlchemyUserRepository",
    "SqlAlchemySessionRepository",
    "SqlAlchemyAccountRepository",
    "SqlAlchemyVerificationRepository",
    "SqlAlchemyAuditSink",
]

SessionScope = t.Callable[[], t.AsyncContextManager["AsyncSession"]]


def _scope_por_defecto() -> SessionScope:
    from hexcore.infrastructure.uow.scopes import session_scope

    return session_scope


class _BaseIdentityRepository:
    """
    Base común. **No** hereda `BaseSQLAlchemyRepository`, y ese es el punto.

    `model` y `session_scope` son inyectables para que el consumidor pueda renombrar las
    tablas vía mixin y para que los tests puedan pasar una sesión propia.
    """

    #: El modelo SQLAlchemy. Se tipa `Any` **a propósito**, y es la única concesión de
    #: tipado del módulo: la clase es inyectable —el consumidor puede renombrar las tablas
    #: vía mixin y pasar la suya— así que su tipo concreto no se conoce estáticamente.
    #: Anotarlo `type` en vez de `Any` no mejora nada y empeora bastante: `self._model.email`
    #: pasa a ser un acceso a atributo desconocido, y eso contamina de "parcialmente
    #: desconocido" cada consulta del archivo. El contrato lo garantizan los puertos
    #: `Abstract*`, que sí están tipados, y `validate_user_model`, que lo verifica al
    #: arrancar.
    _model: t.Any

    def __init__(
        self,
        *,
        model: type | None = None,
        session_scope: SessionScope | None = None,
    ) -> None:
        self._model = model or self._modelo_por_defecto()
        self._session_scope = session_scope or _scope_por_defecto()

    @staticmethod
    def _modelo_por_defecto() -> type:  # pragma: no cover - lo define cada subclase
        raise NotImplementedError


class SqlAlchemyUserRepository(_BaseIdentityRepository, AbstractUserRepository):
    """`AbstractUserRepository` sobre SQLAlchemy."""

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.infrastructure.models import UserModel

        return UserModel

    async def get_by_id(self, user_id: UUID) -> User | None:
        async with self._session_scope() as session:
            fila = await session.get(self._model, user_id)
            return _a_usuario(fila) if fila is not None else None

    async def get_by_email(self, email: str) -> User | None:
        """`email` ya viene normalizado por `Email`; no se vuelve a normalizar acá."""
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(self._model.email == email)
            )
            fila = resultado.scalar_one_or_none()
            return _a_usuario(fila) if fila is not None else None

    async def add(self, user: User) -> User:
        async with self._session_scope() as session:
            fila = self._model(**_campos_usuario(user))
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_usuario(fila)

    async def update(self, user: User) -> User:
        async with self._session_scope() as session:
            fila = await session.get(self._model, user.id)
            if fila is None:
                raise LookupError(f"No existe el usuario {user.id}.")
            for campo, valor in _campos_usuario(user).items():
                setattr(fila, campo, valor)
            await session.commit()
            await session.refresh(fila)
            return _a_usuario(fila)

    async def bump_token_generation(self, user_id: UUID) -> int:
        """
        Incrementa `token_generation` con **una sola sentencia atómica**.

        Con leer-sumar-escribir, dos revocaciones masivas concurrentes dejarían una sin
        efecto — y el efecto que se pierde es "cerrá todas las sesiones de este usuario",
        que es exactamente lo que se ejecuta cuando alguien sospecha que le robaron la
        cuenta.
        """
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(self._model.id == user_id)
                .values(token_generation=self._model.token_generation + 1)
                .returning(self._model.token_generation)
            )
            nuevo = resultado.scalar_one_or_none()
            if nuevo is None:
                raise LookupError(f"No existe el usuario {user_id}.")
            await session.commit()
            return int(nuevo)


class SqlAlchemySessionRepository(_BaseIdentityRepository, AbstractSessionRepository):
    """`AbstractSessionRepository` sobre SQLAlchemy. El adaptador que hace posible revocar."""

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.infrastructure.models import SessionModel

        return SessionModel

    async def get(self, session_id: UUID) -> IdentitySession | None:
        async with self._session_scope() as session:
            fila = await session.get(self._model, session_id)
            return _a_sesion(fila) if fila is not None else None

    async def get_by_token_hash(self, token_hash: str) -> IdentitySession | None:
        """Se busca por el hash, nunca por el token en claro."""
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(self._model.token_hash == token_hash)
            )
            fila = resultado.scalar_one_or_none()
            return _a_sesion(fila) if fila is not None else None

    async def add(self, identity_session: IdentitySession) -> IdentitySession:
        async with self._session_scope() as session:
            fila = self._model(**_campos_sesion(identity_session))
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_sesion(fila)

    async def revoke(self, session_id: UUID, *, at: datetime, reason: str) -> None:
        del reason  # se registra en la auditoría, no en la fila
        async with self._session_scope() as session:
            await session.execute(
                update(self._model)
                .where(self._model.id == session_id, self._model.revoked_at.is_(None))
                .values(revoked_at=at)
            )
            await session.commit()

    async def revoke_family(
        self, family_id: UUID, *, at: datetime, reason: str
    ) -> int:
        """
        Revoca el linaje entero. Se usa ante un reuso de refresh token.

        Si el atacante y el usuario legítimo tienen los dos un token de la familia, revocar
        uno solo deja al otro adentro — y no hay forma de saber cuál es cuál.
        """
        del reason
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(
                    self._model.family_id == family_id,
                    self._model.revoked_at.is_(None),
                )
                .values(revoked_at=at)
            )
            await session.commit()
            return int(resultado.rowcount or 0)

    async def consume_for_rotation(
        self, session_id: UUID, *, at: datetime
    ) -> IdentitySession | None:
        """
        Marca la sesión consumida y la devuelve, o `None` si ya lo estaba.

        **Una sola sentencia**: el `WHERE consumed_at IS NULL` y el `UPDATE` son atómicos, así
        que de dos refresh concurrentes con el mismo token exactamente uno gana y el otro
        recibe `None` — que es lo que dispara la detección de reuso. Con
        leer-y-después-escribir pasarían los dos y el mecanismo no serviría para nada.
        """
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(
                    self._model.id == session_id,
                    self._model.consumed_at.is_(None),
                    self._model.revoked_at.is_(None),
                )
                .values(consumed_at=at)
                .returning(self._model)
            )
            fila = resultado.scalar_one_or_none()
            await session.commit()
            return _a_sesion(fila) if fila is not None else None

    async def list_active_for_user(self, user_id: UUID) -> list[IdentitySession]:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(
                    self._model.subject_user_id == user_id,
                    self._model.revoked_at.is_(None),
                )
            )
            return [_a_sesion(fila) for fila in resultado.scalars()]

    async def delete_expired(self, *, before: datetime) -> int:
        async with self._session_scope() as session:
            resultado = await session.execute(
                delete(self._model).where(self._model.expires_at < before)
            )
            await session.commit()
            return int(resultado.rowcount or 0)


class SqlAlchemyAccountRepository(_BaseIdentityRepository, AbstractAccountRepository):
    """`AbstractAccountRepository` sobre SQLAlchemy."""

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.infrastructure.models import AccountModel

        return AccountModel

    async def get_by_provider(
        self, provider_id: str, account_id: str
    ) -> Account | None:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(
                    self._model.provider_id == provider_id,
                    self._model.account_id == account_id,
                )
            )
            fila = resultado.scalar_one_or_none()
            return _a_cuenta(fila) if fila is not None else None

    async def get_credential(self, user_id: UUID) -> Account | None:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(
                    self._model.user_id == user_id,
                    self._model.provider_id == CREDENTIAL_PROVIDER,
                )
            )
            fila = resultado.scalar_one_or_none()
            return _a_cuenta(fila) if fila is not None else None

    async def list_for_user(self, user_id: UUID) -> list[Account]:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(self._model.user_id == user_id)
            )
            return [_a_cuenta(fila) for fila in resultado.scalars()]

    async def add(self, account: Account) -> Account:
        async with self._session_scope() as session:
            fila = self._model(**_campos_cuenta(account))
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_cuenta(fila)

    async def update(self, account: Account) -> Account:
        async with self._session_scope() as session:
            fila = await session.get(self._model, account.id)
            if fila is None:
                raise LookupError(f"No existe la cuenta {account.id}.")
            for campo, valor in _campos_cuenta(account).items():
                setattr(fila, campo, valor)
            await session.commit()
            await session.refresh(fila)
            return _a_cuenta(fila)

    async def delete(self, account_id: UUID) -> None:
        async with self._session_scope() as session:
            await session.execute(
                delete(self._model).where(self._model.id == account_id)
            )
            await session.commit()


class SqlAlchemyVerificationRepository(
    _BaseIdentityRepository, AbstractVerificationRepository
):
    """`AbstractVerificationRepository` sobre SQLAlchemy."""

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.infrastructure.models import VerificationModel

        return VerificationModel

    async def add(self, verification: Verification) -> Verification:
        async with self._session_scope() as session:
            fila = self._model(**_campos_verificacion(verification))
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_verificacion(fila)

    async def consume(
        self,
        identifier: str,
        purpose: VerificationPurpose,
        value_hash: str,
        *,
        at: datetime,
    ) -> Verification | None:
        """
        Canjea el token y lo marca consumido, en **una sola sentencia**.

        Atómico por el mismo motivo que `consume_for_rotation`: si no, el mismo magic link
        sirve dos veces y "de un solo uso" es una afirmación falsa.

        Se filtra por `purpose` además del identificador para que un código emitido para
        resetear la contraseña no se pueda canjear en el flujo de verificar el mail.
        """
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(
                    self._model.identifier == identifier,
                    self._model.purpose == purpose,
                    self._model.value_hash == value_hash,
                    self._model.consumed_at.is_(None),
                    self._model.expires_at > at,
                )
                .values(consumed_at=at)
                .returning(self._model)
            )
            fila = resultado.scalar_one_or_none()
            await session.commit()
            return _a_verificacion(fila) if fila is not None else None

    async def increment_attempts(self, verification_id: UUID) -> int:
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(self._model.id == verification_id)
                .values(attempts=self._model.attempts + 1)
                .returning(self._model.attempts)
            )
            nuevo = resultado.scalar_one_or_none()
            await session.commit()
            return int(nuevo or 0)

    async def invalidate_for(
        self, identifier: str, purpose: VerificationPurpose, *, at: datetime
    ) -> int:
        """
        Invalida los pendientes de ese identificador y propósito.

        Se llama al emitir uno nuevo: si no, cincuenta clicks en "reenviar" dejan cincuenta
        códigos válidos y el espacio a adivinar se multiplica por cincuenta.
        """
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(
                    self._model.identifier == identifier,
                    self._model.purpose == purpose,
                    self._model.consumed_at.is_(None),
                )
                .values(consumed_at=at)
            )
            await session.commit()
            return int(resultado.rowcount or 0)

    async def delete_expired(self, *, before: datetime) -> int:
        async with self._session_scope() as session:
            resultado = await session.execute(
                delete(self._model).where(self._model.expires_at < before)
            )
            await session.commit()
            return int(resultado.rowcount or 0)


class SqlAlchemyAuditSink(AbstractAuditSink):
    """
    `AbstractAuditSink` sobre SQLAlchemy.

    **Escribe en la sesión del llamador**, no en una propia, y ésa es toda su razón de ser:
    la auditoría de una impersonación tiene que quedar en la misma transacción que el cambio
    que registra. Con una sesión propia existe la ventana donde el cambio se confirma y el
    registro no —o al revés—, y una auditoría que puede faltar no sirve como auditoría.

    Por eso no compone `_BaseIdentityRepository`: no abre `session_scope`, recibe la sesión.

    Uso::

        async with uow_scope() as uow:
            sink = SqlAlchemyAuditSink(session=uow.session)
            await sink.record(action="impersonation.start", ...)
            await uow.commit()      # el cambio y su registro, juntos
    """

    _model: t.Any

    def __init__(self, *, session: "AsyncSession", model: type | None = None) -> None:
        self._session = session
        if model is None:
            from hexcore.darwin.infrastructure.models import AuditLogModel

            model = AuditLogModel
        self._model = model

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
        """
        Agrega la fila a la sesión del llamador. **No commitea**: el dueño de la transacción
        decide cuándo, que es lo que garantiza la atomicidad con el cambio registrado.
        """
        self._session.add(
            self._model(
                action=action,
                actor_id=str(actor_id) if actor_id is not None else None,
                subject_id=str(subject_id) if subject_id is not None else None,
                impersonated=impersonated,
                request_id=request_id,
                audit_metadata=dict(metadata or {}),
            )
        )
        await self._session.flush()


# ── Mapeo fila <-> entidad ────────────────────────────────────────────────────
# Explícito y no un `to_model` genérico: las entidades tienen campos que la tabla no lleva
# (`_domain_events`) y la tabla tiene columnas que la entidad deriva. Un mapeo automático
# acá escondería justamente los desajustes que importan.


def _aware(momento: t.Any) -> datetime:
    """
    Devuelve el `datetime` con zona horaria, asumiendo UTC si vino naive.

    Hace falta porque **SQLite no guarda zona horaria**: `DateTime(timezone=True)` se escribe
    bien y se lee naive. Sin esta normalización, el primer `momento < sesion.expires_at` de un
    chequeo de vencimiento lanza ``TypeError: can't compare offset-naive and offset-aware
    datetimes`` — y lo hace en el camino de verificación de sesión, o sea en cada petición
    autenticada.

    Se normaliza en el adaptador y no en la entidad a propósito: es una limitación del
    backend, y el dominio no tiene por qué enterarse de en qué motor está guardado.

    Toma `Any` porque la fila viene de un modelo inyectable (ver `_BaseIdentityRepository._model`).
    Para columnas nulables está `_aware_opt`.
    """
    if momento.tzinfo is None:
        return momento.replace(tzinfo=UTC)
    return momento


def _aware_opt(momento: t.Any) -> datetime | None:
    """`_aware` para columnas nulables."""
    return None if momento is None else _aware(momento)


def _campos_usuario(user: User) -> dict[str, t.Any]:
    return {
        "id": user.id,
        "email": user.email,
        "email_verified": user.email_verified,
        "name": user.name,
        "image": user.image,
        "token_generation": user.token_generation,
        "locked_until": user.locked_until,
        "is_active": user.is_active,
        "extra": dict(user.extra),
    }


def _a_usuario(fila: t.Any) -> User:
    return User(
        id=fila.id,
        email=fila.email,
        email_verified=fila.email_verified,
        name=fila.name,
        image=fila.image,
        token_generation=fila.token_generation,
        locked_until=_aware_opt(fila.locked_until),
        is_active=fila.is_active,
        extra=dict(fila.extra or {}),
        created_at=_aware(fila.created_at),
        updated_at=_aware(fila.updated_at),
    )


def _campos_sesion(sesion: IdentitySession) -> dict[str, t.Any]:
    return {
        "id": sesion.id,
        "actor_user_id": sesion.actor_user_id,
        "subject_user_id": sesion.subject_user_id,
        "token_hash": sesion.token_hash,
        "family_id": sesion.family_id,
        "transport": sesion.transport,
        "expires_at": sesion.expires_at,
        "revoked_at": sesion.revoked_at,
        "consumed_at": sesion.consumed_at,
        "ip_address": sesion.ip_address,
        "user_agent": sesion.user_agent,
        "impersonation_reason": sesion.impersonation_reason,
        "impersonation_granted_by": sesion.impersonation_granted_by,
        "impersonation_expires_at": sesion.impersonation_expires_at,
        "is_active": sesion.is_active,
    }


def _a_sesion(fila: t.Any) -> IdentitySession:
    return IdentitySession(
        id=fila.id,
        actor_user_id=fila.actor_user_id,
        subject_user_id=fila.subject_user_id,
        token_hash=fila.token_hash,
        family_id=fila.family_id,
        transport=fila.transport,
        expires_at=_aware(fila.expires_at),
        revoked_at=_aware_opt(fila.revoked_at),
        consumed_at=_aware_opt(fila.consumed_at),
        ip_address=fila.ip_address,
        user_agent=fila.user_agent,
        impersonation_reason=fila.impersonation_reason,
        impersonation_granted_by=fila.impersonation_granted_by,
        impersonation_expires_at=_aware_opt(fila.impersonation_expires_at),
        created_at=_aware(fila.created_at),
        updated_at=_aware(fila.updated_at),
    )


def _campos_cuenta(cuenta: Account) -> dict[str, t.Any]:
    return {
        "id": cuenta.id,
        "user_id": cuenta.user_id,
        "provider_id": cuenta.provider_id,
        "account_id": cuenta.account_id,
        "password": cuenta.password,
        "access_token": cuenta.access_token,
        "refresh_token": cuenta.refresh_token,
        "id_token": cuenta.id_token,
        "scope": cuenta.scope,
        "access_token_expires_at": cuenta.access_token_expires_at,
        "refresh_token_expires_at": cuenta.refresh_token_expires_at,
        "is_active": cuenta.is_active,
    }


def _a_cuenta(fila: t.Any) -> Account:
    return Account(
        id=fila.id,
        user_id=fila.user_id,
        provider_id=fila.provider_id,
        account_id=fila.account_id,
        password=fila.password,
        access_token=fila.access_token,
        refresh_token=fila.refresh_token,
        id_token=fila.id_token,
        scope=fila.scope,
        access_token_expires_at=_aware_opt(fila.access_token_expires_at),
        refresh_token_expires_at=_aware_opt(fila.refresh_token_expires_at),
        is_active=fila.is_active,
        created_at=_aware(fila.created_at),
        updated_at=_aware(fila.updated_at),
    )


def _campos_verificacion(v: Verification) -> dict[str, t.Any]:
    return {
        "id": v.id,
        "identifier": v.identifier,
        "value_hash": v.value_hash,
        "purpose": v.purpose,
        "expires_at": v.expires_at,
        "consumed_at": v.consumed_at,
        "attempts": v.attempts,
        "is_active": v.is_active,
    }


def _a_verificacion(fila: t.Any) -> Verification:
    return Verification(
        id=fila.id,
        identifier=fila.identifier,
        value_hash=fila.value_hash,
        purpose=fila.purpose,
        expires_at=_aware(fila.expires_at),
        consumed_at=_aware_opt(fila.consumed_at),
        attempts=fila.attempts,
        is_active=fila.is_active,
        created_at=_aware(fila.created_at),
        updated_at=_aware(fila.updated_at),
    )
