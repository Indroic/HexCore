"""
Adaptadores SQLAlchemy de los cuatro puertos de `rbac`.

Dos operaciones son de una sola sentencia atómica, y las dos importan:

- `AuthzVersionRepository.bump`, un `INSERT ... ON CONFLICT DO UPDATE ... RETURNING`: subir la
  versión tiene que ser upsert-y-sumar en un solo paso, porque leer-sumar-escribir deja que dos
  revocaciones concurrentes en el mismo scope pierdan una.
- `RbacUserRoleRepository.active_role_ids_for`, que filtra los vencidos **en la consulta**: es
  el camino caliente que el resolver de principales llama en cada sign-in y cada refresh (Fase
  F2), así que no puede traer de más y filtrar después en Python.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, or_, select, update

from hexcore.darwin.plugins.rbac.domain import (
    AbstractAuthzVersionRepository,
    AbstractRbacPermissionRepository,
    AbstractRbacRoleRepository,
    AbstractRbacUserRoleRepository,
    RbacPermission,
    RbacRole,
    RoleAssignment,
    RoleNotFoundError,
)

__all__ = [
    "SqlAlchemyRbacRoleRepository",
    "RbacRoleRepository",
    "SqlAlchemyRbacPermissionRepository",
    "RbacPermissionRepository",
    "SqlAlchemyRbacUserRoleRepository",
    "RbacUserRoleRepository",
    "SqlAlchemyAuthzVersionRepository",
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


class _Base:
    """Base común. Modelo y scope inyectables, igual que el resto de la persistencia de Darwin."""

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


# ── Roles ─────────────────────────────────────────────────────────────────────
class SqlAlchemyRbacRoleRepository(_Base, AbstractRbacRoleRepository):
    """`AbstractRbacRoleRepository` sobre SQLAlchemy."""

    #: El modelo de la tabla de unión de permisos directos, y el de herencia. Inyectables por
    #: separado del rol: los tres son tablas distintas y un consumidor puede haber renombrado
    #: cualquiera de ellas.
    _role_permission_model: t.Any
    _role_parent_model: t.Any

    def __init__(
        self,
        *,
        model: type | None = None,
        role_permission_model: type | None = None,
        role_parent_model: type | None = None,
        session_scope: SessionScope | None = None,
    ) -> None:
        super().__init__(model=model, session_scope=session_scope)
        self._role_permission_model = (
            role_permission_model or self._modelo_role_permission_por_defecto()
        )
        self._role_parent_model = (
            role_parent_model or self._modelo_role_parent_por_defecto()
        )

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.plugins.rbac.orms.sqlalchemy.models import RbacRoleModel

        return RbacRoleModel

    @staticmethod
    def _modelo_role_permission_por_defecto() -> type:
        from hexcore.darwin.plugins.rbac.orms.sqlalchemy.models import (
            RbacRolePermissionModel,
        )

        return RbacRolePermissionModel

    @staticmethod
    def _modelo_role_parent_por_defecto() -> type:
        from hexcore.darwin.plugins.rbac.orms.sqlalchemy.models import (
            RbacRoleParentModel,
        )

        return RbacRoleParentModel

    async def add(self, role: RbacRole) -> RbacRole:
        async with self._session_scope() as session:
            fila = self._model(
                id=role.id,
                scope_key=role.scope_key,
                name=role.name,
                description=role.description,
                is_system=role.is_system,
            )
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_rol(fila)

    async def get(self, role_id: UUID) -> RbacRole | None:
        async with self._session_scope() as session:
            fila = await session.get(self._model, role_id)
            return _a_rol(fila) if fila is not None else None

    async def get_by_name(self, scope_key: str, name: str) -> RbacRole | None:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(
                    self._model.scope_key == scope_key, self._model.name == name
                )
            )
            fila = resultado.scalar_one_or_none()
            return _a_rol(fila) if fila is not None else None

    async def list_for_scope(self, scope_key: str) -> list[RbacRole]:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model)
                .where(self._model.scope_key == scope_key)
                .order_by(self._model.name)
            )
            return [_a_rol(f) for f in resultado.scalars().all()]

    async def update(self, role: RbacRole) -> RbacRole:
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(self._model.id == role.id)
                .values(name=role.name, description=role.description)
                .returning(self._model)
            )
            fila = resultado.scalar_one_or_none()
            await session.commit()
            if fila is None:
                raise RoleNotFoundError(f"No existe el rol {role.id}.")
            return _a_rol(fila)

    async def delete(self, role_id: UUID) -> bool:
        async with self._session_scope() as session:
            resultado = await session.execute(
                delete(self._model)
                .where(self._model.id == role_id)
                .returning(self._model.id)
            )
            borro = resultado.scalar_one_or_none() is not None
            await session.commit()
            return borro

    async def set_permissions(self, role_id: UUID, permission_keys: t.Iterable[str]) -> None:
        """
        Reemplaza el set: borra las filas de unión del rol y inserta las nuevas.

        Dos sentencias en la misma transacción, no una condición de carrera que haya que
        resolver: esto es "guardá lo que eligió el formulario", no una invariante que dos
        peticiones concurrentes puedan violar.
        """
        from hexcore.darwin.plugins.rbac.orms.sqlalchemy.models import (
            RbacPermissionModel,
        )

        claves = list(permission_keys)
        async with self._session_scope() as session:
            await session.execute(
                delete(self._role_permission_model).where(
                    self._role_permission_model.role_id == role_id
                )
            )
            if claves:
                resultado = await session.execute(
                    select(RbacPermissionModel.id, RbacPermissionModel.key).where(
                        RbacPermissionModel.key.in_(claves)
                    )
                )
                ids = [fila.id for fila in resultado.all()]
                session.add_all(
                    self._role_permission_model(role_id=role_id, permission_id=pid)
                    for pid in ids
                )
            await session.commit()

    async def permission_keys_for(self, role_id: UUID) -> frozenset[str]:
        from hexcore.darwin.plugins.rbac.orms.sqlalchemy.models import (
            RbacPermissionModel,
        )

        async with self._session_scope() as session:
            resultado = await session.execute(
                select(RbacPermissionModel.key)
                .join(
                    self._role_permission_model,
                    self._role_permission_model.permission_id == RbacPermissionModel.id,
                )
                .where(self._role_permission_model.role_id == role_id)
            )
            return frozenset(resultado.scalars().all())

    async def set_parents(self, role_id: UUID, parent_ids: t.Iterable[UUID]) -> None:
        async with self._session_scope() as session:
            await session.execute(
                delete(self._role_parent_model).where(
                    self._role_parent_model.role_id == role_id
                )
            )
            session.add_all(
                self._role_parent_model(role_id=role_id, parent_id=pid)
                for pid in parent_ids
            )
            await session.commit()

    async def parent_ids_for(self, role_id: UUID) -> frozenset[UUID]:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._role_parent_model.parent_id).where(
                    self._role_parent_model.role_id == role_id
                )
            )
            return frozenset(resultado.scalars().all())


# ── Catálogo de permisos ────────────────────────────────────────────────────
class SqlAlchemyRbacPermissionRepository(_Base, AbstractRbacPermissionRepository):
    """`AbstractRbacPermissionRepository` sobre SQLAlchemy."""

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.plugins.rbac.orms.sqlalchemy.models import (
            RbacPermissionModel,
        )

        return RbacPermissionModel

    async def add(self, permission: RbacPermission) -> RbacPermission:
        async with self._session_scope() as session:
            fila = self._model(
                id=permission.id,
                key=permission.key,
                description=permission.description,
            )
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_permiso(fila)

    async def get_by_key(self, key: str) -> RbacPermission | None:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(self._model.key == key)
            )
            fila = resultado.scalar_one_or_none()
            return _a_permiso(fila) if fila is not None else None

    async def list_all(self) -> list[RbacPermission]:
        async with self._session_scope() as session:
            resultado = await session.execute(select(self._model).order_by(self._model.key))
            return [_a_permiso(f) for f in resultado.scalars().all()]

    async def ensure(self, keys: t.Iterable[str]) -> None:
        """
        Da de alta las que falten. Se consulta antes y se inserta lo nuevo: no hace falta un
        `ON CONFLICT DO NOTHING` para un seed que corre una vez al arrancar y no bajo
        concurrencia real — la carrera entre dos réplicas arrancando a la vez la resuelve el
        `UNIQUE(key)`, que rechaza el duplicado sin tumbar el proceso porque el `INSERT` va
        fila por fila dentro de la misma sesión.
        """
        from uuid import uuid4

        pedidas = frozenset(keys)
        if not pedidas:
            return

        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model.key).where(self._model.key.in_(pedidas))
            )
            existentes = frozenset(resultado.scalars().all())
            faltantes = pedidas - existentes
            for clave in faltantes:
                session.add(self._model(id=uuid4(), key=clave, description=""))
            await session.commit()


# ── Asignaciones ──────────────────────────────────────────────────────────────
class SqlAlchemyRbacUserRoleRepository(_Base, AbstractRbacUserRoleRepository):
    """`AbstractRbacUserRoleRepository` sobre SQLAlchemy."""

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.plugins.rbac.orms.sqlalchemy.models import (
            RbacUserRoleModel,
        )

        return RbacUserRoleModel

    async def assign(self, assignment: RoleAssignment) -> RoleAssignment:
        async with self._session_scope() as session:
            fila = self._model(
                id=assignment.id,
                user_id=assignment.user_id,
                role_id=assignment.role_id,
                scope_key=assignment.scope_key,
                granted_by=assignment.granted_by,
                expires_at=assignment.expires_at,
                created_at=assignment.created_at or datetime.now(UTC),
            )
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_asignacion(fila)

    async def revoke(self, user_id: UUID, role_id: UUID, scope_key: str) -> bool:
        async with self._session_scope() as session:
            resultado = await session.execute(
                delete(self._model)
                .where(
                    self._model.user_id == user_id,
                    self._model.role_id == role_id,
                    self._model.scope_key == scope_key,
                )
                .returning(self._model.id)
            )
            revoco = resultado.scalar_one_or_none() is not None
            await session.commit()
            return revoco

    async def list_for_user(self, user_id: UUID, scope_key: str) -> list[RoleAssignment]:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model)
                .where(
                    self._model.user_id == user_id, self._model.scope_key == scope_key
                )
                .order_by(self._model.created_at)
            )
            return [_a_asignacion(f) for f in resultado.scalars().all()]

    async def active_role_ids_for(
        self, user_id: UUID, scope_key: str, *, at: datetime
    ) -> frozenset[UUID]:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model.role_id).where(
                    self._model.user_id == user_id,
                    self._model.scope_key == scope_key,
                    or_(
                        self._model.expires_at.is_(None),
                        self._model.expires_at > at,
                    ),
                )
            )
            return frozenset(resultado.scalars().all())


# ── Versión de autorización ────────────────────────────────────────────────
class SqlAlchemyAuthzVersionRepository(_Base, AbstractAuthzVersionRepository):
    """`AbstractAuthzVersionRepository` sobre SQLAlchemy."""

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.plugins.rbac.orms.sqlalchemy.models import (
            AuthzVersionModel,
        )

        return AuthzVersionModel

    async def get(self, scope_key: str) -> int:
        async with self._session_scope() as session:
            fila = await session.get(self._model, scope_key)
            return int(fila.version) if fila is not None else 0

    async def bump(self, scope_key: str) -> int:
        """
        Upsert atómico: `INSERT ... ON CONFLICT (scope_key) DO UPDATE SET version = version + 1`.

        El dialecto se lee de la conexión, mismo criterio que `_insert_ignore` en
        `cron_sql.py`: sólo PostgreSQL y SQLite tienen `ON CONFLICT`, que son los dos
        backends que Darwin soporta hoy.
        """
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
def _a_rol(fila: t.Any) -> RbacRole:
    return RbacRole(
        id=fila.id,
        scope_key=fila.scope_key,
        name=fila.name,
        description=fila.description,
        is_system=fila.is_system,
        created_at=_aware(fila.created_at),
        updated_at=_aware(fila.updated_at),
    )


def _a_permiso(fila: t.Any) -> RbacPermission:
    return RbacPermission(id=fila.id, key=fila.key, description=fila.description)


def _a_asignacion(fila: t.Any) -> RoleAssignment:
    return RoleAssignment(
        id=fila.id,
        user_id=fila.user_id,
        role_id=fila.role_id,
        scope_key=fila.scope_key,
        granted_by=fila.granted_by,
        expires_at=_aware(fila.expires_at),
        created_at=_aware(fila.created_at),
    )


# ── El contrato del backend ───────────────────────────────────────────────────
# Los alias con nombre neutro que `plugin_repositories()` busca. Ver el docstring homólogo en
# `organization/orms/sqlalchemy/repository.py`.
RbacRoleRepository = SqlAlchemyRbacRoleRepository
RbacPermissionRepository = SqlAlchemyRbacPermissionRepository
RbacUserRoleRepository = SqlAlchemyRbacUserRoleRepository
AuthzVersionRepository = SqlAlchemyAuthzVersionRepository
