"""
Adaptadores SQLAlchemy de los dos puertos de `passkey`.

La operación que importa es `bump_sign_count`: **una sentencia** ``UPDATE ... WHERE sign_count <
:nuevo RETURNING``. La condición va en el `WHERE` y no en Python porque leer-comparar-escribir deja
pasar las dos peticiones de un replay concurrente — y detectar el replay es justamente para lo que
el contador existe.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select, update

from hexcore.darwin.plugins.passkey.domain import (
    AbstractPasskeyChallengeRepository,
    AbstractPasskeyRepository,
    ChallengePurpose,
    Passkey,
    PasskeyChallenge,
)

__all__ = ["SqlAlchemyPasskeyRepository", "SqlAlchemyPasskeyChallengeRepository"]

SessionScope = t.Callable[[], t.AsyncContextManager[t.Any]]


def _scope_por_defecto() -> SessionScope:
    from hexcore.infrastructure.uow.scopes import session_scope

    return session_scope


def _aware(valor: datetime | None) -> datetime | None:
    """
    Normaliza a UTC-aware.

    SQLite devuelve datetimes naive aunque la columna sea `DateTime(timezone=True)`, y comparar
    naive con aware levanta `TypeError`.
    """
    if valor is None:
        return None
    return valor if valor.tzinfo is not None else valor.replace(tzinfo=UTC)


class _Base:
    """Base común. Igual que el resto de la persistencia de Darwin: modelo y scope inyectables."""

    #: `t.Any` por lo mismo que en `hexcore/darwin/infrastructure/repositories.py`: el modelo es
    #: inyectable, así que su tipo concreto no se conoce estáticamente.
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


class SqlAlchemyPasskeyRepository(_Base, AbstractPasskeyRepository):
    """`AbstractPasskeyRepository` sobre SQLAlchemy."""

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.plugins.passkey.orms.sqlalchemy.models import PasskeyModel

        return PasskeyModel

    async def add(self, passkey: Passkey) -> Passkey:
        async with self._session_scope() as session:
            fila = self._model(
                id=passkey.id,
                user_id=passkey.user_id,
                credential_id=passkey.credential_id,
                public_key=passkey.public_key,
                sign_count=passkey.sign_count,
                name=passkey.name,
                aaguid=passkey.aaguid,
                backed_up=passkey.backed_up,
                transports=list(passkey.transports),
            )
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_passkey(fila)

    async def get_by_credential_id(self, credential_id: str) -> Passkey | None:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(
                    self._model.credential_id == credential_id
                )
            )
            fila = resultado.scalar_one_or_none()
            return _a_passkey(fila) if fila is not None else None

    async def list_for_user(self, user_id: UUID) -> list[Passkey]:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model)
                .where(self._model.user_id == user_id)
                .order_by(self._model.created_at)
            )
            return [_a_passkey(f) for f in resultado.scalars().all()]

    async def bump_sign_count(
        self, credential_id: str, *, new_count: int, at: datetime
    ) -> bool:
        """
        Sube el contador **sólo si el nuevo es estrictamente mayor**.

        `False` si no subió, y eso es la señal de clonado o de replay. El `WHERE` es todo el
        mecanismo: con leer-comparar-escribir, dos aserciones concurrentes con el mismo contador
        leen el mismo valor viejo y las dos pasan.

        ⚠️ El caso `sign_count == 0` guardado **y** `new_count == 0` se acepta como "este
        autenticador no usa el contador": es lo que hacen varias llaves y varios navegadores, y
        rechazarlo dejaría afuera a credenciales legítimas. Lo que no se acepta es que un contador
        que **ya avanzó** deje de avanzar.
        """
        async with self._session_scope() as session:
            if new_count == 0:
                # El autenticador no lleva contador. Sólo se toca `last_used_at`, y se exige que
                # el guardado también sea 0: si alguna vez avanzó, un 0 ahora es una regresión.
                resultado = await session.execute(
                    update(self._model)
                    .where(
                        self._model.credential_id == credential_id,
                        self._model.sign_count == 0,
                    )
                    .values(last_used_at=at)
                    .returning(self._model.id)
                )
            else:
                resultado = await session.execute(
                    update(self._model)
                    .where(
                        self._model.credential_id == credential_id,
                        self._model.sign_count < new_count,
                    )
                    .values(sign_count=new_count, last_used_at=at)
                    .returning(self._model.id)
                )
            subio = resultado.scalar_one_or_none() is not None
            await session.commit()
            return subio

    async def delete(self, passkey_id: UUID) -> bool:
        async with self._session_scope() as session:
            resultado = await session.execute(
                delete(self._model)
                .where(self._model.id == passkey_id)
                .returning(self._model.id)
            )
            borro = resultado.scalar_one_or_none() is not None
            await session.commit()
            return borro


class SqlAlchemyPasskeyChallengeRepository(_Base, AbstractPasskeyChallengeRepository):
    """`AbstractPasskeyChallengeRepository` sobre SQLAlchemy."""

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.plugins.passkey.orms.sqlalchemy.models import PasskeyChallengeModel

        return PasskeyChallengeModel

    async def add(self, challenge: PasskeyChallenge) -> PasskeyChallenge:
        async with self._session_scope() as session:
            fila = self._model(
                id=challenge.id,
                challenge=challenge.challenge,
                purpose=challenge.purpose,
                user_id=challenge.user_id,
                expires_at=challenge.expires_at,
            )
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_challenge(fila)

    async def consume(
        self, purpose: ChallengePurpose, challenge: str, *, at: datetime
    ) -> PasskeyChallenge | None:
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(
                    self._model.challenge == challenge,
                    self._model.purpose == purpose,
                    self._model.consumed_at.is_(None),
                    self._model.expires_at > at,
                )
                .values(consumed_at=at)
                .returning(self._model)
            )
            fila = resultado.scalar_one_or_none()
            await session.commit()
            return _a_challenge(fila) if fila is not None else None

    async def delete_expired(self, *, before: datetime) -> int:
        async with self._session_scope() as session:
            resultado = await session.execute(
                delete(self._model).where(self._model.expires_at < before)
            )
            await session.commit()
            return int(resultado.rowcount or 0)


def _a_passkey(fila: t.Any) -> Passkey:
    # La columna es `JSON`, así que su contenido es `Any`: se anota el destino para que el
    # `tuple(...)` de abajo no propague un tipo desconocido.
    crudo: list[t.Any] = list(fila.transports or [])
    return Passkey(
        id=fila.id,
        user_id=fila.user_id,
        credential_id=fila.credential_id,
        public_key=fila.public_key,
        sign_count=fila.sign_count,
        name=fila.name,
        aaguid=fila.aaguid,
        backed_up=fila.backed_up,
        transports=tuple(str(x) for x in crudo),
        last_used_at=_aware(fila.last_used_at),
        created_at=_aware(fila.created_at),
    )


def _a_challenge(fila: t.Any) -> PasskeyChallenge:
    return PasskeyChallenge(
        id=fila.id,
        challenge=fila.challenge,
        purpose=t.cast(ChallengePurpose, fila.purpose),
        user_id=fila.user_id,
        expires_at=_aware(fila.expires_at) or datetime.now(UTC),
        consumed_at=_aware(fila.consumed_at),
    )

# ── El contrato del backend ───────────────────────────────────────────────────
#
# Los alias con nombre neutro que `plugin_repositories()` busca. Mismo criterio que en el núcleo:
# el nombre con prefijo dice en qué está implementado —útil para quien lo instancia a mano— y el
# neutro es el nombre del rol, que es lo que el resolvedor necesita.
#
# ⚠️ Todo backend nuevo de este plugin tiene que exponer estos nombres.
PasskeyRepository = SqlAlchemyPasskeyRepository
PasskeyChallengeRepository = SqlAlchemyPasskeyChallengeRepository
