"""
Almacenamiento de `rbac` en Beanie. Requiere `[darwin-beanie]`.

⚠️ **El esquema se aparta del de SQL a propósito, otra vez por la misma razón que
`organization`: lo que en SQL es una tabla de unión, en Mongo es un array embebido.**

`darwin_rbac_role_permission` y `darwin_rbac_role_parent` existen en SQL porque un `JOIN` es
barato y porque las claves foráneas dan integridad referencial gratis. Ninguna de las dos
razones aplica acá: los permisos directos y los padres de un rol **son del rol**, nadie más los
consulta de forma independiente, y no hay ninguna invariante multi-documento que dependa de
ellos —a diferencia de los `owner`s de `organization`, que sí la tenían—. Embeberlos como
`permission_keys: list[str]` y `parent_role_ids: list[UUID]` en el propio documento del rol
convierte "leer los permisos directos de un rol" en una lectura de un campo, no un `$lookup`.

Por eso este backend tiene **cuatro** documentos donde SQL tiene seis mixins: el rol (con sus
dos arrays adentro), el catálogo de permisos, la asignación usuario↔rol, y la versión de
autorización. `test_darwin_schema_registration.py` fija ambos números.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pymongo
from beanie import Document
from beanie.odm.queries.update import UpdateResponse
from pydantic import Field
from pymongo import IndexModel

from hexcore.darwin.plugins.rbac.domain import (
    AbstractAuthzVersionRepository,
    AbstractRbacPermissionRepository,
    AbstractRbacRoleRepository,
    AbstractRbacUserRoleRepository,
    GLOBAL_SCOPE,
    RbacPermission,
    RbacRole,
    RoleAssignment,
    RoleNotFoundError,
)

__all__ = [
    "PLUGIN_DOCUMENTS",
    "RbacRoleDocument",
    "RbacPermissionDocument",
    "RbacUserRoleDocument",
    "AuthzVersionDocument",
    "BeanieRbacRoleRepository",
    "BeanieRbacPermissionRepository",
    "BeanieRbacUserRoleRepository",
    "BeanieAuthzVersionRepository",
    "RbacRoleRepository",
    "RbacPermissionRepository",
    "RbacUserRoleRepository",
    "AuthzVersionRepository",
    "RBAC_DOCUMENTS",
]


class RbacRoleDocument(Document):
    """
    Colección `darwin_rbac_role`, con los permisos directos y los padres embebidos.

    Ver el docstring del módulo para el porqué. `parent_role_ids` guarda `entity_id`s de otros
    `RbacRoleDocument`, no `ObjectId`s de Mongo: es el mismo id de dominio (`RbacRole.id`) que
    usa todo lo demás, así que un rol se puede referenciar sin importar en qué colección de
    Mongo terminó viviendo su `_id` interno.
    """

    entity_id: UUID = Field(default_factory=uuid4)
    scope_key: str = GLOBAL_SCOPE
    name: str
    description: str = ""
    is_system: bool = False
    permission_keys: list[str] = Field(default_factory=lambda: t.cast("list[str]", []))
    parent_role_ids: list[UUID] = Field(
        default_factory=lambda: t.cast("list[UUID]", [])
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "darwin_rbac_role"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            IndexModel(
                [("scope_key", pymongo.ASCENDING), ("name", pymongo.ASCENDING)],
                unique=True,
            ),
        ]


class RbacPermissionDocument(Document):
    """Colección `darwin_rbac_permission`: el catálogo. Ver el docstring de `RbacPermission`."""

    entity_id: UUID = Field(default_factory=uuid4)
    key: str
    description: str = ""

    class Settings:
        name = "darwin_rbac_permission"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            IndexModel([("key", pymongo.ASCENDING)], unique=True),
        ]


class RbacUserRoleDocument(Document):
    """Colección `darwin_rbac_user_role`: quién tiene qué rol, en qué scope, hasta cuándo."""

    entity_id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    role_id: UUID
    scope_key: str = GLOBAL_SCOPE
    granted_by: UUID | None = None
    expires_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "darwin_rbac_user_role"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            IndexModel(
                [
                    ("user_id", pymongo.ASCENDING),
                    ("role_id", pymongo.ASCENDING),
                    ("scope_key", pymongo.ASCENDING),
                ],
                unique=True,
            ),
            IndexModel([("user_id", pymongo.ASCENDING), ("scope_key", pymongo.ASCENDING)]),
            IndexModel([("role_id", pymongo.ASCENDING)]),
        ]


class AuthzVersionDocument(Document):
    """
    Colección `darwin_authz_version`: el contador monotónico por scope.

    `scope_key` es el único campo con significado: no hay un `entity_id` de dominio separado
    porque la versión no es una entidad, es un contador indexado por scope.
    """

    scope_key: str = GLOBAL_SCOPE
    version: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "darwin_authz_version"
        use_cache = False
        indexes = [IndexModel([("scope_key", pymongo.ASCENDING)], unique=True)]


RBAC_DOCUMENTS: tuple[type[Document], ...] = (
    RbacRoleDocument,
    RbacPermissionDocument,
    RbacUserRoleDocument,
    AuthzVersionDocument,
)


# ── Roles ─────────────────────────────────────────────────────────────────────
class BeanieRbacRoleRepository(AbstractRbacRoleRepository):
    """`AbstractRbacRoleRepository` sobre Beanie. Ver el docstring del módulo."""

    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or RbacRoleDocument

    async def add(self, role: RbacRole) -> RbacRole:
        doc = self._doc(
            entity_id=role.id,
            scope_key=role.scope_key,
            name=role.name,
            description=role.description,
            is_system=role.is_system,
        )
        await doc.insert()
        return _a_rol(doc)

    async def get(self, role_id: UUID) -> RbacRole | None:
        doc = await self._doc.find_one(self._doc.entity_id == role_id)
        return _a_rol(doc) if doc is not None else None

    async def get_by_name(self, scope_key: str, name: str) -> RbacRole | None:
        doc = await self._doc.find_one(
            self._doc.scope_key == scope_key, self._doc.name == name
        )
        return _a_rol(doc) if doc is not None else None

    async def list_for_scope(self, scope_key: str) -> list[RbacRole]:
        docs = await self._doc.find(self._doc.scope_key == scope_key).to_list()
        return [_a_rol(d) for d in sorted(docs, key=lambda d: d.name)]

    async def update(self, role: RbacRole) -> RbacRole:
        doc = await self._doc.find_one(self._doc.entity_id == role.id).update(
            {
                "$set": {
                    "name": role.name,
                    "description": role.description,
                    "updated_at": datetime.now(UTC),
                }
            },
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        if doc is None:
            raise RoleNotFoundError(f"No existe el rol {role.id}.")
        return _a_rol(doc)

    async def delete(self, role_id: UUID) -> bool:
        doc = await self._doc.find_one(self._doc.entity_id == role_id)
        if doc is None:
            return False
        await doc.delete()
        return True

    async def set_permissions(self, role_id: UUID, permission_keys: t.Iterable[str]) -> None:
        await self._doc.find_one(self._doc.entity_id == role_id).update(
            {
                "$set": {
                    "permission_keys": list(permission_keys),
                    "updated_at": datetime.now(UTC),
                }
            }
        )

    async def permission_keys_for(self, role_id: UUID) -> frozenset[str]:
        doc = await self._doc.find_one(self._doc.entity_id == role_id)
        return frozenset(doc.permission_keys) if doc is not None else frozenset()

    async def set_parents(self, role_id: UUID, parent_ids: t.Iterable[UUID]) -> None:
        await self._doc.find_one(self._doc.entity_id == role_id).update(
            {
                "$set": {
                    "parent_role_ids": list(parent_ids),
                    "updated_at": datetime.now(UTC),
                }
            }
        )

    async def parent_ids_for(self, role_id: UUID) -> frozenset[UUID]:
        doc = await self._doc.find_one(self._doc.entity_id == role_id)
        return frozenset(doc.parent_role_ids) if doc is not None else frozenset()


# ── Catálogo de permisos ────────────────────────────────────────────────────
class BeanieRbacPermissionRepository(AbstractRbacPermissionRepository):
    """`AbstractRbacPermissionRepository` sobre Beanie."""

    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or RbacPermissionDocument

    async def add(self, permission: RbacPermission) -> RbacPermission:
        doc = self._doc(
            entity_id=permission.id,
            key=permission.key,
            description=permission.description,
        )
        await doc.insert()
        return _a_permiso(doc)

    async def get_by_key(self, key: str) -> RbacPermission | None:
        doc = await self._doc.find_one(self._doc.key == key)
        return _a_permiso(doc) if doc is not None else None

    async def list_all(self) -> list[RbacPermission]:
        docs = await self._doc.find().to_list()
        return [_a_permiso(d) for d in sorted(docs, key=lambda d: d.key)]

    async def ensure(self, keys: t.Iterable[str]) -> None:
        """Igual criterio que el backend de SQL: se consulta antes y se inserta lo nuevo."""
        pedidas = frozenset(keys)
        if not pedidas:
            return

        existentes_docs = await self._doc.find({"key": {"$in": list(pedidas)}}).to_list()
        existentes = frozenset(d.key for d in existentes_docs)
        faltantes = pedidas - existentes
        if not faltantes:
            return

        await self._doc.insert_many(
            [self._doc(entity_id=uuid4(), key=clave, description="") for clave in faltantes]
        )


# ── Asignaciones ──────────────────────────────────────────────────────────────
class BeanieRbacUserRoleRepository(AbstractRbacUserRoleRepository):
    """`AbstractRbacUserRoleRepository` sobre Beanie."""

    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or RbacUserRoleDocument

    async def assign(self, assignment: RoleAssignment) -> RoleAssignment:
        doc = self._doc(
            entity_id=assignment.id,
            user_id=assignment.user_id,
            role_id=assignment.role_id,
            scope_key=assignment.scope_key,
            granted_by=assignment.granted_by,
            expires_at=assignment.expires_at,
            created_at=assignment.created_at or datetime.now(UTC),
        )
        await doc.insert()
        return _a_asignacion(doc)

    async def revoke(self, user_id: UUID, role_id: UUID, scope_key: str) -> bool:
        doc = await self._doc.find_one(
            self._doc.user_id == user_id,
            self._doc.role_id == role_id,
            self._doc.scope_key == scope_key,
        )
        if doc is None:
            return False
        await doc.delete()
        return True

    async def list_for_user(self, user_id: UUID, scope_key: str) -> list[RoleAssignment]:
        docs = await self._doc.find(
            self._doc.user_id == user_id, self._doc.scope_key == scope_key
        ).to_list()
        return [_a_asignacion(d) for d in sorted(docs, key=lambda d: d.created_at)]

    async def active_role_ids_for(
        self, user_id: UUID, scope_key: str, *, at: datetime
    ) -> frozenset[UUID]:
        docs = await self._doc.find(
            {
                "user_id": user_id,
                "scope_key": scope_key,
                "$or": [{"expires_at": None}, {"expires_at": {"$gt": at}}],
            }
        ).to_list()
        return frozenset(d.role_id for d in docs)


# ── Versión de autorización ────────────────────────────────────────────────
class BeanieAuthzVersionRepository(AbstractAuthzVersionRepository):
    """`AbstractAuthzVersionRepository` sobre Beanie."""

    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or AuthzVersionDocument

    async def get(self, scope_key: str) -> int:
        doc = await self._doc.find_one({"scope_key": scope_key})
        return int(doc.version) if doc is not None else 0

    async def bump(self, scope_key: str) -> int:
        """
        `$inc` con `upsert=True`: crea el contador la primera vez y lo sube atómicamente el
        resto. Mismo criterio que `GenerationGuard`/`bump_token_generation` en Beanie.
        """
        doc = await self._doc.find_one({"scope_key": scope_key}).update(
            {"$inc": {"version": 1}, "$set": {"updated_at": datetime.now(UTC)}},
            upsert=True,
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return int(doc.version)


# ── Mapeo ─────────────────────────────────────────────────────────────────────
def _a_rol(doc: t.Any) -> RbacRole:
    from hexcore.darwin.infrastructure.orms.beanie.repositories import to_utc

    return RbacRole(
        id=doc.entity_id,
        scope_key=doc.scope_key,
        name=doc.name,
        description=doc.description,
        is_system=doc.is_system,
        created_at=to_utc(doc.created_at),
        updated_at=to_utc(doc.updated_at),
    )


def _a_permiso(doc: t.Any) -> RbacPermission:
    return RbacPermission(id=doc.entity_id, key=doc.key, description=doc.description)


def _a_asignacion(doc: t.Any) -> RoleAssignment:
    from hexcore.darwin.infrastructure.orms.beanie.repositories import to_utc

    return RoleAssignment(
        id=doc.entity_id,
        user_id=doc.user_id,
        role_id=doc.role_id,
        scope_key=doc.scope_key,
        granted_by=doc.granted_by,
        expires_at=to_utc(doc.expires_at),
        created_at=to_utc(doc.created_at),
    )


# ── El contrato del backend ───────────────────────────────────────────────────
RbacRoleRepository = BeanieRbacRoleRepository
RbacPermissionRepository = BeanieRbacPermissionRepository
RbacUserRoleRepository = BeanieRbacUserRoleRepository
AuthzVersionRepository = BeanieAuthzVersionRepository


# ── El contrato de esquema ───────────────────────────────────────────
PLUGIN_DOCUMENTS = RBAC_DOCUMENTS
