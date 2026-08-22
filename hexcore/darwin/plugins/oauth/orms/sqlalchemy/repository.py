"""
Persistencia del `state` de OAuth.

Una sola operación importa y es `consume`: **una sentencia** ``UPDATE ... WHERE consumed_at IS
NULL RETURNING``. Con leer-y-después-escribir, un `state` interceptado se puede canjear dos veces
—dos callbacks concurrentes con el mismo `code`— y el `state` deja de ser de un solo uso, que es
la mitad de su valor como defensa anti-CSRF.

El puerto y la entidad viven en `domain.py` y no acá: este módulo importa sqlalchemy en el nivel
superior, así que tenerlos acá haría que importar el servicio —que los necesita— exija el extra
`[sql]`. Hay un test que lo verifica.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime

from sqlalchemy import delete, update

from hexcore.darwin.plugins.oauth.domain import (
    AbstractOAuthStateRepository,
    OAuthState,
)

__all__ = [
    "SqlAlchemyOAuthStateRepository",
    "OAuthStateRepository",
]

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


class SqlAlchemyOAuthStateRepository(AbstractOAuthStateRepository):
    """`AbstractOAuthStateRepository` sobre SQLAlchemy."""

    #: `t.Any` por lo mismo que en el resto de la persistencia de Darwin: el modelo es
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
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.plugins.oauth.orms.sqlalchemy.models import OAuthStateModel

        return OAuthStateModel

    async def add(self, state: OAuthState) -> OAuthState:
        async with self._session_scope() as session:
            fila = self._model(
                id=state.id,
                provider_id=state.provider_id,
                state_hash=state.state_hash,
                code_verifier_encrypted=state.code_verifier_encrypted,
                redirect_uri=state.redirect_uri,
                link_user_id=state.link_user_id,
                expires_at=state.expires_at,
            )
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_entidad(fila)

    async def consume(
        self, provider_id: str, state_hash: str, *, at: datetime
    ) -> OAuthState | None:
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(
                    self._model.provider_id == provider_id,
                    self._model.state_hash == state_hash,
                    self._model.consumed_at.is_(None),
                    self._model.expires_at > at,
                )
                .values(consumed_at=at)
                .returning(self._model)
            )
            fila = resultado.scalar_one_or_none()
            await session.commit()
            return _a_entidad(fila) if fila is not None else None

    async def delete_expired(self, *, before: datetime) -> int:
        async with self._session_scope() as session:
            resultado = await session.execute(
                delete(self._model).where(self._model.expires_at < before)
            )
            await session.commit()
            return int(resultado.rowcount or 0)


def _a_entidad(fila: t.Any) -> OAuthState:
    return OAuthState(
        id=fila.id,
        provider_id=fila.provider_id,
        state_hash=fila.state_hash,
        code_verifier_encrypted=fila.code_verifier_encrypted,
        redirect_uri=fila.redirect_uri,
        link_user_id=fila.link_user_id,
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
OAuthStateRepository = SqlAlchemyOAuthStateRepository
