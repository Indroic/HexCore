"""
Almacenamiento de `drbac` en Beanie. Requiere `[darwin-beanie]`.

**Las reglas van embebidas en el propio documento de la política**, al revés que SQL (que las
guarda en `darwin_drbac_rule`, tabla aparte por `position`): nadie consulta una regla suelta,
sin su política, y no hay ninguna invariante multi-documento que dependa de ellas. Es la misma
razón que `rbac` ya documenta para `permission_keys`/`parent_role_ids` embebidos en el rol.

Por eso este backend tiene **tres** documentos donde SQL tiene cuatro mixins: la política (con
sus reglas adentro), el binding de rol contextual, y la versión de autorización propia del
plugin.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pymongo
from beanie import Document
from beanie.odm.queries.update import UpdateResponse
from pydantic import BaseModel, ConfigDict, Field
from pymongo import IndexModel

from hexcore.darwin.plugins.drbac.conditions import parse_condition
from hexcore.darwin.plugins.drbac.domain import (
    AbstractDrbacAuthzVersionRepository,
    AbstractDrbacPolicyRepository,
    AbstractDrbacRoleBindingRepository,
    GLOBAL_SCOPE,
    Policy,
    PolicyNotFoundError,
    RoleBinding,
    Rule,
)

__all__ = [
    "PLUGIN_DOCUMENTS",
    "EmbeddedRule",
    "DrbacPolicyDocument",
    "DrbacRoleBindingDocument",
    "DrbacAuthzVersionDocument",
    "BeanieDrbacPolicyRepository",
    "BeanieDrbacRoleBindingRepository",
    "BeanieDrbacAuthzVersionRepository",
    "PolicyRepository",
    "RoleBindingRepository",
    "AuthzVersionRepository",
    "DRBAC_DOCUMENTS",
]


class EmbeddedRule(BaseModel):
    """La forma embebida de `Rule` adentro de `DrbacPolicyDocument.rules`."""

    model_config = ConfigDict(frozen=True)

    entity_id: UUID = Field(default_factory=uuid4)
    position: int = 0
    effect: str
    actions: list[str]
    resource_type: str
    condition: dict[str, t.Any] | None = None
    client_evaluable: bool = False


class DrbacPolicyDocument(Document):
    """Colección `darwin_drbac_policy`, con las reglas embebidas. Ver el docstring del módulo."""

    entity_id: UUID = Field(default_factory=uuid4)
    scope_key: str = GLOBAL_SCOPE
    name: str
    description: str = ""
    enabled: bool = True
    priority: int = 100
    rules: list[EmbeddedRule] = Field(default_factory=lambda: t.cast("list[EmbeddedRule]", []))
    created_by: UUID | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "darwin_drbac_policy"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            IndexModel(
                [("scope_key", pymongo.ASCENDING), ("name", pymongo.ASCENDING)], unique=True
            ),
            IndexModel([("scope_key", pymongo.ASCENDING), ("enabled", pymongo.ASCENDING)]),
        ]


class DrbacRoleBindingDocument(Document):
    """Colección `darwin_drbac_role_binding`."""

    entity_id: UUID = Field(default_factory=uuid4)
    subject_id: UUID
    role_name: str
    scope_path: str = GLOBAL_SCOPE
    condition: dict[str, t.Any] | None = None
    expires_at: datetime | None = None
    granted_by: UUID | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "darwin_drbac_role_binding"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            IndexModel([("subject_id", pymongo.ASCENDING), ("scope_path", pymongo.ASCENDING)]),
            IndexModel([("expires_at", pymongo.ASCENDING)]),
        ]


class DrbacAuthzVersionDocument(Document):
    """Colección `darwin_drbac_authz_version`. Propia del plugin — ver el docstring de
    `domain.AbstractDrbacAuthzVersionRepository`."""

    scope_key: str = GLOBAL_SCOPE
    version: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "darwin_drbac_authz_version"
        use_cache = False
        indexes = [IndexModel([("scope_key", pymongo.ASCENDING)], unique=True)]


DRBAC_DOCUMENTS: tuple[type[Document], ...] = (
    DrbacPolicyDocument,
    DrbacRoleBindingDocument,
    DrbacAuthzVersionDocument,
)


# ── Políticas ──────────────────────────────────────────────────────────────────
class BeanieDrbacPolicyRepository(AbstractDrbacPolicyRepository):
    """`AbstractDrbacPolicyRepository` sobre Beanie. Ver el docstring del módulo."""

    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or DrbacPolicyDocument

    async def add(self, policy: Policy) -> Policy:
        doc = self._doc(
            entity_id=policy.id,
            scope_key=policy.scope_key,
            name=policy.name,
            description=policy.description,
            enabled=policy.enabled,
            priority=policy.priority,
            rules=[_a_regla_embebida(r) for r in policy.rules],
            created_by=policy.created_by,
        )
        await doc.insert()
        return _a_politica(doc)

    async def get(self, policy_id: UUID) -> Policy | None:
        doc = await self._doc.find_one(self._doc.entity_id == policy_id)
        return _a_politica(doc) if doc is not None else None

    async def get_by_name(self, scope_key: str, name: str) -> Policy | None:
        doc = await self._doc.find_one(
            self._doc.scope_key == scope_key, self._doc.name == name
        )
        return _a_politica(doc) if doc is not None else None

    async def list_for_scope(self, scope_key: str) -> list[Policy]:
        docs = await self._doc.find(self._doc.scope_key == scope_key).to_list()
        return [_a_politica(d) for d in sorted(docs, key=lambda d: (d.priority, d.name))]

    async def list_enabled_for_scopes(self, scope_keys: t.Iterable[str]) -> list[Policy]:
        claves = list(scope_keys)
        if not claves:
            return []
        docs = await self._doc.find(
            {"scope_key": {"$in": claves}, "enabled": True}
        ).to_list()
        return [_a_politica(d) for d in sorted(docs, key=lambda d: (d.priority, d.name))]

    async def update(self, policy: Policy) -> Policy:
        doc = await self._doc.find_one(self._doc.entity_id == policy.id).update(
            {
                "$set": {
                    "name": policy.name,
                    "description": policy.description,
                    "enabled": policy.enabled,
                    "priority": policy.priority,
                    "rules": [_a_regla_embebida(r).model_dump() for r in policy.rules],
                    "updated_at": datetime.now(UTC),
                }
            },
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        if doc is None:
            raise PolicyNotFoundError(f"No existe la política {policy.id}.")
        return _a_politica(doc)

    async def delete(self, policy_id: UUID) -> bool:
        doc = await self._doc.find_one(self._doc.entity_id == policy_id)
        if doc is None:
            return False
        await doc.delete()
        return True


# ── Bindings ────────────────────────────────────────────────────────────────────
class BeanieDrbacRoleBindingRepository(AbstractDrbacRoleBindingRepository):
    """`AbstractDrbacRoleBindingRepository` sobre Beanie."""

    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or DrbacRoleBindingDocument

    async def add(self, binding: RoleBinding) -> RoleBinding:
        doc = self._doc(
            entity_id=binding.id,
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
        await doc.insert()
        return _a_binding(doc)

    async def get(self, binding_id: UUID) -> RoleBinding | None:
        doc = await self._doc.find_one(self._doc.entity_id == binding_id)
        return _a_binding(doc) if doc is not None else None

    async def delete(self, binding_id: UUID) -> bool:
        doc = await self._doc.find_one(self._doc.entity_id == binding_id)
        if doc is None:
            return False
        await doc.delete()
        return True

    async def list_for_subject(self, subject_id: UUID) -> list[RoleBinding]:
        docs = await self._doc.find(self._doc.subject_id == subject_id).to_list()
        return [_a_binding(d) for d in sorted(docs, key=lambda d: d.created_at)]

    async def active_for_subject_at_scopes(
        self, subject_id: UUID, scope_paths: t.Iterable[str], *, at: datetime
    ) -> list[RoleBinding]:
        claves = list(scope_paths)
        if not claves:
            return []
        docs = await self._doc.find(
            {
                "subject_id": subject_id,
                "scope_path": {"$in": claves},
                "$or": [{"expires_at": None}, {"expires_at": {"$gt": at}}],
            }
        ).to_list()
        return [_a_binding(d) for d in docs]


# ── Versión de autorización ────────────────────────────────────────────────────
class BeanieDrbacAuthzVersionRepository(AbstractDrbacAuthzVersionRepository):
    """`AbstractDrbacAuthzVersionRepository` sobre Beanie."""

    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or DrbacAuthzVersionDocument

    async def get(self, scope_key: str) -> int:
        doc = await self._doc.find_one({"scope_key": scope_key})
        return int(doc.version) if doc is not None else 0

    async def bump(self, scope_key: str) -> int:
        doc = await self._doc.find_one({"scope_key": scope_key}).update(
            {"$inc": {"version": 1}, "$set": {"updated_at": datetime.now(UTC)}},
            upsert=True,
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return int(doc.version)


# ── Mapeo ─────────────────────────────────────────────────────────────────────
def _a_regla_embebida(regla: Rule) -> EmbeddedRule:
    return EmbeddedRule(
        entity_id=regla.id,
        position=regla.position,
        effect=regla.effect,
        actions=list(regla.actions),
        resource_type=regla.resource_type,
        condition=regla.condition.model_dump(mode="json") if regla.condition is not None else None,
        client_evaluable=regla.client_evaluable,
    )


def _a_regla(embebida: EmbeddedRule) -> Rule:
    return Rule(
        id=embebida.entity_id,
        position=embebida.position,
        effect=embebida.effect,  # type: ignore[arg-type]
        actions=tuple(embebida.actions),
        resource_type=embebida.resource_type,
        condition=parse_condition(embebida.condition) if embebida.condition is not None else None,
        client_evaluable=embebida.client_evaluable,
    )


def _a_politica(doc: t.Any) -> Policy:
    from hexcore.darwin.infrastructure.orms.beanie.repositories import to_utc

    return Policy(
        id=doc.entity_id,
        scope_key=doc.scope_key,
        name=doc.name,
        description=doc.description,
        enabled=doc.enabled,
        priority=doc.priority,
        rules=tuple(_a_regla(r) for r in doc.rules),
        created_by=doc.created_by,
        created_at=to_utc(doc.created_at),
        updated_at=to_utc(doc.updated_at),
    )


def _a_binding(doc: t.Any) -> RoleBinding:
    from hexcore.darwin.infrastructure.orms.beanie.repositories import to_utc

    return RoleBinding(
        id=doc.entity_id,
        subject_id=doc.subject_id,
        role_name=doc.role_name,
        scope_path=doc.scope_path,
        condition=parse_condition(doc.condition) if doc.condition is not None else None,
        expires_at=to_utc(doc.expires_at),
        granted_by=doc.granted_by,
        created_at=to_utc(doc.created_at),
    )


# ── El contrato del backend ───────────────────────────────────────────────────
PolicyRepository = BeanieDrbacPolicyRepository
RoleBindingRepository = BeanieDrbacRoleBindingRepository
AuthzVersionRepository = BeanieDrbacAuthzVersionRepository

# ── El contrato de esquema ───────────────────────────────────────────
PLUGIN_DOCUMENTS = DRBAC_DOCUMENTS
