"""
Adaptadores SQLAlchemy de los tres puertos de `drbac`.

`Policy` es el dominio embebiendo `rules`, pero la tabla las guarda aparte (`darwin_drbac_rule`,
por `position`) — ver el docstring de `domain.Policy`. Este repositorio es el que tapa esa
diferencia: `add`/`update` borran e insertan el conjunto de reglas entero en la misma
transacción que la política (reemplazo completo, mismo criterio que
`SqlAlchemyRbacRoleRepository.set_permissions`), y `get`/`list_*` las traen con un segundo
`SELECT` ordenado por `position`.

`AuthzVersionRepository.bump` es el mismo `INSERT ... ON CONFLICT DO UPDATE ... RETURNING` que
`rbac`, sobre su propia tabla — ver el docstring de
`domain.AbstractDrbacAuthzVersionRepository`.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, or_, select, update

from hexcore.darwin.plugins.drbac.conditions import parse_condition
from hexcore.darwin.plugins.drbac.domain import (
    AbstractDrbacAuthzVersionRepository,
    AbstractDrbacPolicyRepository,
    AbstractDrbacRoleBindingRepository,
    Policy,
    PolicyNotFoundError,
    RoleBinding,
    Rule,
)

__all__ = [
    "SqlAlchemyDrbacPolicyRepository",
    "PolicyRepository",
    "SqlAlchemyDrbacRoleBindingRepository",
    "RoleBindingRepository",
    "SqlAlchemyDrbacAuthzVersionRepository",
    "AuthzVersionRepository",
]

SessionScope = t.Callable[[], t.AsyncContextManager[t.Any]]


def _scope_por_defecto() -> SessionScope:
    from hexcore.infrastructure.uow.scopes import session_scope

    return session_scope


def _aware(valor: datetime | None) -> datetime | None:
    """SQLite devuelve datetimes naive; compararlos con aware levanta `TypeError`."""
    if valor is None:
        return None
    return valor if valor.tzinfo is not None else valor.replace(tzinfo=UTC)


# ── Políticas ──────────────────────────────────────────────────────────────────
class SqlAlchemyDrbacPolicyRepository(AbstractDrbacPolicyRepository):
    """`AbstractDrbacPolicyRepository` sobre SQLAlchemy."""

    _model: t.Any
    _rule_model: t.Any

    def __init__(
        self,
        *,
        model: type | None = None,
        rule_model: type | None = None,
        session_scope: SessionScope | None = None,
    ) -> None:
        self._model = model or self._modelo_por_defecto()
        self._rule_model = rule_model or self._modelo_de_regla_por_defecto()
        self._session_scope = session_scope or _scope_por_defecto()

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.plugins.drbac.orms.sqlalchemy.models import DrbacPolicyModel

        return DrbacPolicyModel

    @staticmethod
    def _modelo_de_regla_por_defecto() -> type:
        from hexcore.darwin.plugins.drbac.orms.sqlalchemy.models import DrbacRuleModel

        return DrbacRuleModel

    async def add(self, policy: Policy) -> Policy:
        async with self._session_scope() as session:
            fila = self._model(
                id=policy.id,
                scope_key=policy.scope_key,
                name=policy.name,
                description=policy.description,
                enabled=policy.enabled,
                priority=policy.priority,
                created_by=policy.created_by,
            )
            session.add(fila)
            session.add_all(self._filas_de_reglas(policy.id, policy.rules))
            await session.commit()
            await session.refresh(fila)
            return await self._ensamblar(session, fila)

    async def get(self, policy_id: UUID) -> Policy | None:
        async with self._session_scope() as session:
            fila = await session.get(self._model, policy_id)
            return await self._ensamblar(session, fila) if fila is not None else None

    async def get_by_name(self, scope_key: str, name: str) -> Policy | None:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(
                    self._model.scope_key == scope_key, self._model.name == name
                )
            )
            fila = resultado.scalar_one_or_none()
            return await self._ensamblar(session, fila) if fila is not None else None

    async def list_for_scope(self, scope_key: str) -> list[Policy]:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model)
                .where(self._model.scope_key == scope_key)
                .order_by(self._model.priority, self._model.name)
            )
            return [await self._ensamblar(session, f) for f in resultado.scalars().all()]

    async def list_enabled_for_scopes(self, scope_keys: t.Iterable[str]) -> list[Policy]:
        claves = list(scope_keys)
        if not claves:
            return []
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model)
                .where(self._model.scope_key.in_(claves), self._model.enabled.is_(True))
                .order_by(self._model.priority, self._model.name)
            )
            return [await self._ensamblar(session, f) for f in resultado.scalars().all()]

    async def update(self, policy: Policy) -> Policy:
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(self._model.id == policy.id)
                .values(
                    name=policy.name,
                    description=policy.description,
                    enabled=policy.enabled,
                    priority=policy.priority,
                )
                .returning(self._model)
            )
            fila = resultado.scalar_one_or_none()
            if fila is None:
                raise PolicyNotFoundError(f"No existe la política {policy.id}.")

            await session.execute(
                delete(self._rule_model).where(self._rule_model.policy_id == policy.id)
            )
            session.add_all(self._filas_de_reglas(policy.id, policy.rules))
            await session.commit()
            await session.refresh(fila)
            return await self._ensamblar(session, fila)

    async def delete(self, policy_id: UUID) -> bool:
        async with self._session_scope() as session:
            resultado = await session.execute(
                delete(self._model).where(self._model.id == policy_id).returning(self._model.id)
            )
            borro = resultado.scalar_one_or_none() is not None
            await session.commit()
            return borro

    def _filas_de_reglas(self, policy_id: UUID, rules: t.Iterable[Rule]) -> list[t.Any]:
        return [
            self._rule_model(
                id=regla.id,
                policy_id=policy_id,
                position=regla.position,
                effect=regla.effect,
                actions=list(regla.actions),
                resource_type=regla.resource_type,
                condition=(
                    regla.condition.model_dump(mode="json") if regla.condition is not None else None
                ),
                client_evaluable=regla.client_evaluable,
            )
            for regla in rules
        ]

    async def _ensamblar(self, session: t.Any, fila: t.Any) -> Policy:
        resultado = await session.execute(
            select(self._rule_model)
            .where(self._rule_model.policy_id == fila.id)
            .order_by(self._rule_model.position)
        )
        reglas = [_a_regla(f) for f in resultado.scalars().all()]
        return _a_politica(fila, reglas)


# ── Bindings ────────────────────────────────────────────────────────────────────
class SqlAlchemyDrbacRoleBindingRepository(AbstractDrbacRoleBindingRepository):
    """`AbstractDrbacRoleBindingRepository` sobre SQLAlchemy."""

    _model: t.Any

    def __init__(self, *, model: type | None = None, session_scope: SessionScope | None = None) -> None:
        self._model = model or self._modelo_por_defecto()
        self._session_scope = session_scope or _scope_por_defecto()

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.plugins.drbac.orms.sqlalchemy.models import DrbacRoleBindingModel

        return DrbacRoleBindingModel

    async def add(self, binding: RoleBinding) -> RoleBinding:
        async with self._session_scope() as session:
            fila = self._model(
                id=binding.id,
                subject_id=binding.subject_id,
                role_name=binding.role_name,
                scope_path=binding.scope_path,
                condition=(
                    binding.condition.model_dump(mode="json")
                    if binding.condition is not None
                    else None
                ),
                expires_at=binding.expires_at,
                granted_by=binding.granted_by,
                created_at=binding.created_at or datetime.now(UTC),
            )
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_binding(fila)

    async def get(self, binding_id: UUID) -> RoleBinding | None:
        async with self._session_scope() as session:
            fila = await session.get(self._model, binding_id)
            return _a_binding(fila) if fila is not None else None

    async def delete(self, binding_id: UUID) -> bool:
        async with self._session_scope() as session:
            resultado = await session.execute(
                delete(self._model)
                .where(self._model.id == binding_id)
                .returning(self._model.id)
            )
            borro = resultado.scalar_one_or_none() is not None
            await session.commit()
            return borro

    async def list_for_subject(self, subject_id: UUID) -> list[RoleBinding]:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model)
                .where(self._model.subject_id == subject_id)
                .order_by(self._model.created_at)
            )
            return [_a_binding(f) for f in resultado.scalars().all()]

    async def active_for_subject_at_scopes(
        self, subject_id: UUID, scope_paths: t.Iterable[str], *, at: datetime
    ) -> list[RoleBinding]:
        claves = list(scope_paths)
        if not claves:
            return []
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(
                    self._model.subject_id == subject_id,
                    self._model.scope_path.in_(claves),
                    or_(self._model.expires_at.is_(None), self._model.expires_at > at),
                )
            )
            return [_a_binding(f) for f in resultado.scalars().all()]


# ── Versión de autorización ────────────────────────────────────────────────────
class SqlAlchemyDrbacAuthzVersionRepository(AbstractDrbacAuthzVersionRepository):
    """`AbstractDrbacAuthzVersionRepository` sobre SQLAlchemy. Ver `rbac`'s
    `SqlAlchemyAuthzVersionRepository` — mismo algoritmo, tabla propia."""

    _model: t.Any

    def __init__(self, *, model: type | None = None, session_scope: SessionScope | None = None) -> None:
        self._model = model or self._modelo_por_defecto()
        self._session_scope = session_scope or _scope_por_defecto()

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.plugins.drbac.orms.sqlalchemy.models import DrbacAuthzVersionModel

        return DrbacAuthzVersionModel

    async def get(self, scope_key: str) -> int:
        async with self._session_scope() as session:
            fila = await session.get(self._model, scope_key)
            return int(fila.version) if fila is not None else 0

    async def bump(self, scope_key: str) -> int:
        async with self._session_scope() as session:
            dialecto = session.get_bind().dialect.name
            tabla = self._model.__table__
            ahora = datetime.now(UTC)

            if dialecto == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as construir_insert
            elif dialecto == "sqlite":
                from sqlalchemy.dialects.sqlite import insert as construir_insert
            else:
                raise NotImplementedError(
                    f"AuthzVersionRepository.bump() no soporta el dialecto {dialecto!r}. "
                    f"Sólo PostgreSQL y SQLite tienen ON CONFLICT."
                )

            sentencia = construir_insert(tabla).values(
                scope_key=scope_key, version=1, updated_at=ahora
            )
            sentencia = sentencia.on_conflict_do_update(
                index_elements=["scope_key"],
                set_={"version": tabla.c.version + 1, "updated_at": ahora},
            ).returning(tabla.c.version)

            resultado = await session.execute(sentencia)
            nuevo = resultado.scalar_one()
            await session.commit()
            return int(nuevo)


# ── Mapeo ─────────────────────────────────────────────────────────────────────
def _a_regla(fila: t.Any) -> Rule:
    return Rule(
        id=fila.id,
        position=fila.position,
        effect=fila.effect,
        actions=tuple(fila.actions),
        resource_type=fila.resource_type,
        condition=parse_condition(fila.condition) if fila.condition is not None else None,
        client_evaluable=fila.client_evaluable,
    )


def _a_politica(fila: t.Any, rules: list[Rule]) -> Policy:
    return Policy(
        id=fila.id,
        scope_key=fila.scope_key,
        name=fila.name,
        description=fila.description,
        enabled=fila.enabled,
        priority=fila.priority,
        rules=tuple(rules),
        created_by=fila.created_by,
        created_at=_aware(fila.created_at),
        updated_at=_aware(fila.updated_at),
    )


def _a_binding(fila: t.Any) -> RoleBinding:
    return RoleBinding(
        id=fila.id,
        subject_id=fila.subject_id,
        role_name=fila.role_name,
        scope_path=fila.scope_path,
        condition=parse_condition(fila.condition) if fila.condition is not None else None,
        expires_at=_aware(fila.expires_at),
        granted_by=fila.granted_by,
        created_at=_aware(fila.created_at),
    )


# ── El contrato del backend ───────────────────────────────────────────────────
PolicyRepository = SqlAlchemyDrbacPolicyRepository
RoleBindingRepository = SqlAlchemyDrbacRoleBindingRepository
AuthzVersionRepository = SqlAlchemyDrbacAuthzVersionRepository
