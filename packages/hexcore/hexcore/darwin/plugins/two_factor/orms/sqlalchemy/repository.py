"""
Adaptador SQLAlchemy de `AbstractTwoFactorRepository`.

Mismas dos reglas que el resto de la persistencia de Darwin: **no** hereda de
`BaseSQLAlchemyRepository` (`_repository_key_from_class_name` levanta `ValueError` ante una
colisión de clave, y un `TwoFactorRepository` autodescubrible rompería el UoW de cualquier
consumidor que ya tenga el suyo) y el modelo **no** hereda de `BaseModel[T]`.

Las tres operaciones que la seguridad exige atómicas —`confirm`, `consume_step` y
`record_failure`— son **una sola sentencia** ``UPDATE ... WHERE ... RETURNING``. Con
leer-y-después-escribir, dos peticiones con el mismo código robado pasan las dos, y la defensa
de replay —que es lo único que separa "el código es válido" de "el código no se usó"— no dispara
nunca.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, select, update

from hexcore.darwin.plugins.two_factor.domain import (
    AbstractTwoFactorRepository,
    TwoFactor,
)

if t.TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "SqlAlchemyTwoFactorRepository",
    "TwoFactorRepository",
]

SessionScope = t.Callable[[], t.AsyncContextManager["AsyncSession"]]


def _scope_por_defecto() -> SessionScope:
    from hexcore.infrastructure.uow.scopes import session_scope

    return session_scope


def _aware(valor: datetime | None) -> datetime | None:
    """
    Normaliza a UTC-aware.

    SQLite devuelve datetimes **naive** aunque la columna sea `DateTime(timezone=True)`, y
    comparar naive con aware levanta `TypeError`. Se normaliza al hidratar y no en cada
    comparación: al revés, el que se olvida de una es el que falla en producción.
    """
    if valor is None:
        return None
    return valor if valor.tzinfo is not None else valor.replace(tzinfo=UTC)


class SqlAlchemyTwoFactorRepository(AbstractTwoFactorRepository):
    """`AbstractTwoFactorRepository` sobre SQLAlchemy."""

    #: `t.Any` por lo mismo que en `hexcore/darwin/infrastructure/repositories.py`: el modelo
    #: es inyectable —el consumidor puede renombrar la tabla vía mixin— así que su tipo
    #: concreto no se conoce estáticamente.
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
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.plugins.two_factor.orms.sqlalchemy.models import TwoFactorModel

        return TwoFactorModel

    # ── Lectura ───────────────────────────────────────────────────────────────
    async def get_for_user(self, user_id: UUID) -> TwoFactor | None:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(self._model.user_id == user_id)
            )
            fila = resultado.scalar_one_or_none()
            return _a_entidad(fila) if fila is not None else None

    # ── Escritura ─────────────────────────────────────────────────────────────
    async def upsert(self, factor: TwoFactor) -> TwoFactor:
        """
        Reemplaza la fila del usuario, o la crea.

        Se borra y se inserta en vez de hacer un `ON CONFLICT DO UPDATE` porque una
        re-inscripción **es** un factor nuevo: arrastrar el `last_used_step` o los
        `failed_attempts` del secreto anterior no tiene sentido, y arrastrarlos sin querer
        dejaría al usuario nuevo bloqueado por los intentos del viejo.
        """
        async with self._session_scope() as session:
            await session.execute(
                delete(self._model).where(self._model.user_id == factor.user_id)
            )
            fila = self._model(
                id=factor.id,
                user_id=factor.user_id,
                secret_encrypted=factor.secret_encrypted,
                confirmed_at=factor.confirmed_at,
                last_used_step=factor.last_used_step,
                failed_attempts=factor.failed_attempts,
            )
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_entidad(fila)

    async def confirm(
        self, user_id: UUID, *, at: datetime, step: int
    ) -> TwoFactor | None:
        """
        Confirma el factor **sólo si `confirmed_at IS NULL`**, en una sola sentencia.

        El `WHERE` sobre `confirmed_at` es lo que hace que dos confirmaciones concurrentes con
        códigos distintos no se pisen: si ganara la última, el `last_used_step` guardado sería
        el del código que perdió y el del que ganó quedaría reusable.
        """
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(
                    self._model.user_id == user_id,
                    self._model.confirmed_at.is_(None),
                )
                .values(confirmed_at=at, last_used_step=step, failed_attempts=0)
                .returning(self._model)
            )
            fila = resultado.scalar_one_or_none()
            await session.commit()
            return _a_entidad(fila) if fila is not None else None

    async def consume_step(
        self, user_id: UUID, *, step: int, after_step: int | None
    ) -> bool:
        """
        Consume el paso TOTP. `True` si esta llamada fue la que lo consumió.

        La condición va en el `WHERE` y no en Python: es toda la defensa de replay. Leer
        `last_used_step`, comparar y después escribir deja la ventana donde dos peticiones con
        el mismo código leen el mismo valor viejo y las dos pasan — que es exactamente el
        escenario contra el que esto existe.
        """
        async with self._session_scope() as session:
            condicion = [self._model.user_id == user_id]
            if after_step is not None:
                condicion.append(self._model.last_used_step.is_not(None))
                condicion.append(self._model.last_used_step < step)
            else:
                # Sin paso previo, cualquiera sirve — pero igual se exige que la fila no haya
                # avanzado por otra petición concurrente.
                condicion.append(self._model.last_used_step.is_(None))

            resultado = await session.execute(
                update(self._model)
                .where(*condicion)
                .values(last_used_step=step, failed_attempts=0)
                .returning(self._model.id)
            )
            gano = resultado.scalar_one_or_none() is not None
            await session.commit()
            return gano

    async def record_failure(self, user_id: UUID) -> int:
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(self._model.user_id == user_id)
                .values(failed_attempts=self._model.failed_attempts + 1)
                .returning(self._model.failed_attempts)
            )
            nuevo = resultado.scalar_one_or_none()
            await session.commit()
            return int(nuevo or 0)

    async def reset_failures(self, user_id: UUID) -> None:
        async with self._session_scope() as session:
            await session.execute(
                update(self._model)
                .where(self._model.user_id == user_id)
                .values(failed_attempts=0)
            )
            await session.commit()

    async def delete_for_user(self, user_id: UUID) -> bool:
        async with self._session_scope() as session:
            resultado = await session.execute(
                delete(self._model)
                .where(self._model.user_id == user_id)
                .returning(self._model.id)
            )
            borro = resultado.scalar_one_or_none() is not None
            await session.commit()
            return borro


def _a_entidad(fila: t.Any) -> TwoFactor:
    return TwoFactor(
        id=fila.id,
        user_id=fila.user_id,
        secret_encrypted=fila.secret_encrypted,
        confirmed_at=_aware(fila.confirmed_at),
        last_used_step=fila.last_used_step,
        failed_attempts=fila.failed_attempts,
        created_at=_aware(fila.created_at) or datetime.now(UTC),
        updated_at=_aware(fila.updated_at) or datetime.now(UTC),
    )

# ── El contrato del backend ───────────────────────────────────────────────────
#
# Los alias con nombre neutro que `plugin_repositories()` busca. Mismo criterio que en el núcleo:
# el nombre con prefijo dice en qué está implementado —útil para quien lo instancia a mano— y el
# neutro es el nombre del rol, que es lo que el resolvedor necesita.
#
# ⚠️ Todo backend nuevo de este plugin tiene que exponer estos nombres.
TwoFactorRepository = SqlAlchemyTwoFactorRepository
