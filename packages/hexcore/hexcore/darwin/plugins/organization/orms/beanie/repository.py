"""
Almacenamiento de `organization` en Beanie. Requiere `[darwin-beanie]`.

⚠️ **Acá el modelo de datos se aparta del de SQL a propósito, y es la decisión más interesante de
todo el backend de Mongo: los miembros van embebidos en el documento de la organización.**

El motivo es la invariante que más cuidado necesitó en SQL — *una organización nunca queda sin
`owner`*. En SQL la implementación correcta terminó siendo una subconsulta correlacionada `EXISTS`
adentro del `WHERE` del `UPDATE`, porque contar antes y escribir después es check-then-act y dos
degradaciones concurrentes dejaban la organización con cero owners (lo demostró un test).

En Mongo esa subconsulta **no existe**: un `findOneAndUpdate` sólo puede referirse a campos del
propio documento. Con los miembros en su propia colección, la invariante exigiría una transacción
multi-documento — o sea un replica set, o sea una condición de topología que el framework no puede
asumir.

Con los miembros **embebidos**, la misma invariante se vuelve una condición sobre un solo
documento::

    find_one({
      entity_id: org,
      "members.user_id": objetivo,
      $expr: {$gt: [{$size: {$filter: {input: "$members", cond: {$eq: ["$$this.role", "owner"]}}}}, 1]}
    }).update({$set: {"members.$.role": nuevo}})

Un documento, atómico, sin transacción. Y de paso la unicidad `(organization_id, user_id)` —que en
SQL es un `UNIQUE` que produce un `IntegrityError` cuando alguien lo viola— pasa a ser un filtro
`{"members.user_id": {$ne: usuario}}` en el `$push`, o sea que agregar dos veces no falla: no hace
nada, y el llamador se entera por el valor de retorno.

El costo, y hay que decirlo: el documento de la organización tiene el techo de 16 MB de BSON, así
que el modelo embebido pone un límite práctico de decenas de miles de miembros por organización.
Para el caso de uso de `organization` —equipos, empresas, workspaces— sobra. Para una organización
con millones de miembros haría falta el modelo en colección aparte y la transacción.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pymongo
from beanie import Document
from beanie.odm.queries.update import UpdateResponse
from pydantic import BaseModel, Field
from pymongo import IndexModel

from hexcore.darwin.plugins.organization.domain import (
    AbstractInvitationRepository,
    AbstractMemberRepository,
    AbstractOrganizationRepository,
    Invitation,
    InvitationStatus,
    Member,
    Organization,
    OrganizationNotFoundError,
    OrgRole,
)

__all__ = [
    "PLUGIN_DOCUMENTS",
    "EmbeddedMember",
    "OrganizationDocument",
    "InvitationDocument",
    "BeanieOrganizationRepository",
    "BeanieMemberRepository",
    "BeanieInvitationRepository",
    "OrganizationRepository",
    "MemberRepository",
    "InvitationRepository",
    "ORGANIZATION_DOCUMENTS",
]


class EmbeddedMember(BaseModel):
    """
    Un miembro, embebido en el documento de su organización. Ver el docstring del módulo.

    Lleva su propio `entity_id` porque la entidad de dominio `Member` tiene id: el embebido no es
    una simplificación del modelo, es otra forma de guardarlo.
    """

    entity_id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    role: str = "member"
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class OrganizationDocument(Document):
    """Colección `darwin_organization`, con los miembros adentro."""

    entity_id: UUID = Field(default_factory=uuid4)
    name: str
    slug: str
    org_metadata: dict[str, t.Any] = Field(default_factory=dict)
    # `default_factory=list` deja el tipo del elemento desconocido para pyright; la lambda
    # anotada lo fija.
    members: list[EmbeddedMember] = Field(
        default_factory=lambda: t.cast("list[EmbeddedMember]", [])
    )
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "darwin_organization"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            IndexModel([("slug", pymongo.ASCENDING)], unique=True),
            # Índice multikey sobre el array: es lo que hace que "las organizaciones de este
            # usuario" siga siendo una consulta por índice y no un scan.
            IndexModel([("members.user_id", pymongo.ASCENDING)]),
            IndexModel(
                [
                    ("members.user_id", pymongo.ASCENDING),
                    ("members.role", pymongo.ASCENDING),
                ]
            ),
        ]


class InvitationDocument(Document):
    """
    Colección `darwin_invitation`. **No** embebida: a diferencia de los miembros, las invitaciones
    no participan de ninguna invariante multi-documento, y son efímeras — así que van aparte y con
    índice TTL, que es lo que las limpia solas.
    """

    entity_id: UUID = Field(default_factory=uuid4)
    organization_id: UUID
    email: str
    role: str = "member"
    invited_by: UUID
    #: **SHA-256 del token, nunca el token.** El link viaja por mail y queda en el buzón.
    token_hash: str
    status: str = "pending"
    expires_at: datetime
    consumed_at: datetime | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    class Settings:
        name = "darwin_invitation"
        use_cache = False
        indexes = [
            IndexModel([("entity_id", pymongo.ASCENDING)], unique=True),
            IndexModel([("token_hash", pymongo.ASCENDING)], unique=True),
            IndexModel(
                [
                    ("organization_id", pymongo.ASCENDING),
                    ("status", pymongo.ASCENDING),
                ]
            ),
            IndexModel([("expires_at", pymongo.ASCENDING)], expireAfterSeconds=0),
        ]


ORGANIZATION_DOCUMENTS: tuple[type[Document], ...] = (
    OrganizationDocument,
    InvitationDocument,
)


# ── Organizaciones ────────────────────────────────────────────────────────────
class BeanieOrganizationRepository(AbstractOrganizationRepository):
    """`AbstractOrganizationRepository` sobre Beanie."""

    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or OrganizationDocument

    async def add(self, organization: Organization) -> Organization:
        doc = self._doc(
            entity_id=organization.id,
            name=organization.name,
            slug=organization.slug,
            org_metadata=dict(organization.metadata),
        )
        await doc.insert()
        return _a_organizacion(doc)

    async def get(self, organization_id: UUID) -> Organization | None:
        doc = await self._doc.find_one(self._doc.entity_id == organization_id)
        return _a_organizacion(doc) if doc is not None else None

    async def get_by_slug(self, slug: str) -> Organization | None:
        doc = await self._doc.find_one(self._doc.slug == slug)
        return _a_organizacion(doc) if doc is not None else None

    async def update(self, organization: Organization) -> Organization:
        """Nombre y metadata. **El slug no se toca** — ver la entidad."""
        doc = await self._doc.find_one(
            self._doc.entity_id == organization.id
        ).update(
            {
                "$set": {
                    "name": organization.name,
                    "org_metadata": dict(organization.metadata),
                    "updated_at": datetime.now(UTC),
                }
            },
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        if doc is None:
            raise OrganizationNotFoundError("No existe esa organización.")
        return _a_organizacion(doc)

    async def delete(self, organization_id: UUID) -> bool:
        """
        Borra la organización **y sus miembros con ella**, porque están adentro del documento.

        Es la otra ventaja del modelo embebido: no hay `CASCADE` que declarar ni una limpieza que
        se pueda olvidar. Las invitaciones sí quedan, y las barre su índice TTL.
        """
        doc = await self._doc.find_one(self._doc.entity_id == organization_id)
        if doc is None:
            return False
        await doc.delete()
        return True


# ── Miembros ──────────────────────────────────────────────────────────────────
class BeanieMemberRepository(AbstractMemberRepository):
    """
    `AbstractMemberRepository` sobre el array embebido. Ver el docstring del módulo.

    Cada método opera sobre **un** documento de organización, y las dos operaciones que en SQL
    necesitaban un constraint o una subconsulta pasan a ser condiciones del filtro.
    """

    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or OrganizationDocument

    async def add(self, member: Member) -> Member:
        """
        Suma al miembro **sólo si no está ya**, con un `$push` guardado.

        El filtro `{"members.user_id": {$ne: user}}` reemplaza al `UNIQUE(org, user)` de SQL, y con
        una diferencia a favor: agregar dos veces no levanta un `IntegrityError` que hay que
        atrapar — simplemente no hace nada, y el llamador se entera por el valor de retorno.
        """
        embebido = EmbeddedMember(
            entity_id=member.id,
            user_id=member.user_id,
            role=str(member.role),
            created_at=member.created_at or datetime.now(UTC),
        )

        doc = await self._doc.find_one(
            {
                "entity_id": member.organization_id,
                "members.user_id": {"$ne": member.user_id},
            }
        ).update(
            {
                "$push": {"members": embebido.model_dump(mode="python")},
                "$set": {"updated_at": datetime.now(UTC)},
            },
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        if doc is None:
            from hexcore.darwin.plugins.organization.domain import AlreadyAMemberError

            # O la organización no existe, o ya es miembro. Se distingue mirando de nuevo: es una
            # lectura extra en el camino de error, que no es el caliente.
            existe = await self._doc.find_one(
                self._doc.entity_id == member.organization_id
            )
            if existe is None:
                raise OrganizationNotFoundError("No existe esa organización.")
            raise AlreadyAMemberError("Esa persona ya es miembro de la organización.")

        return member

    async def get(self, organization_id: UUID, user_id: UUID) -> Member | None:
        doc = await self._doc.find_one(self._doc.entity_id == organization_id)
        if doc is None:
            return None
        return _buscar_miembro(doc, organization_id, user_id)

    async def list_for_organization(self, organization_id: UUID) -> list[Member]:
        doc = await self._doc.find_one(self._doc.entity_id == organization_id)
        if doc is None:
            return []
        return [
            _a_miembro(m, organization_id)
            for m in sorted(doc.members, key=lambda m: m.created_at)
        ]

    async def list_for_user(self, user_id: UUID) -> list[Member]:
        """
        Las organizaciones de alguien.

        Consulta por el índice multikey `members.user_id`, así que no es un scan. Devuelve una
        membresía por organización, y hay que filtrar el array del lado del cliente: la proyección
        `$elemMatch` traería sólo el elemento, pero complica el mapeo por un ahorro que en un
        documento de este tamaño no se nota.
        """
        docs = await self._doc.find({"members.user_id": user_id}).to_list()

        membresias: list[Member] = []
        for doc in docs:
            encontrada = _buscar_miembro(doc, doc.entity_id, user_id)
            if encontrada is not None:
                membresias.append(encontrada)
        return sorted(membresias, key=lambda m: m.created_at or datetime.now(UTC))

    async def count_by_role(self, organization_id: UUID, role: OrgRole) -> int:
        """
        Cuántos hay con ese rol. **Informativo**, igual que en SQL.

        La decisión de si se puede degradar al último `owner` la toma la base, adentro de la
        sentencia — ver `set_role`. Esto sirve para mostrarle al usuario "sos el único owner" antes
        de que apriete el botón.
        """
        doc = await self._doc.find_one(self._doc.entity_id == organization_id)
        if doc is None:
            return 0
        return sum(1 for m in doc.members if m.role == str(role))

    async def set_role(
        self,
        organization_id: UUID,
        user_id: UUID,
        *,
        role: OrgRole,
        keep_last_owner: bool = False,
    ) -> Member | None:
        """
        Cambia el rol con el guardián **adentro del filtro**.

        ⚠️ Con `keep_last_owner=True` se exige, en la misma sentencia, que queden al menos dos
        owners. Es la invariante que en SQL necesitó una subconsulta correlacionada y que acá es
        una condición sobre el propio documento — ver el docstring del módulo.

        `None` si no era miembro **o si el guardián lo bloqueó**.
        """
        filtro: dict[str, t.Any] = {
            "entity_id": organization_id,
            "members.user_id": user_id,
        }
        if keep_last_owner:
            filtro["$expr"] = _quedan_dos_owners()

        doc = await self._doc.find_one(filtro).update(
            {
                "$set": {
                    "members.$.role": str(role),
                    "updated_at": datetime.now(UTC),
                }
            },
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        if doc is None:
            return None
        return _buscar_miembro(doc, organization_id, user_id)

    async def remove(
        self, organization_id: UUID, user_id: UUID, *, keep_last_owner: bool = False
    ) -> bool:
        """
        Saca al miembro con el mismo guardián. `False` si no estaba o si lo bloqueó.

        `$pull` sobre el array, así que es una sola escritura sobre un documento.
        """
        filtro: dict[str, t.Any] = {
            "entity_id": organization_id,
            "members.user_id": user_id,
        }
        if keep_last_owner:
            filtro["$expr"] = _quedan_dos_owners()

        doc = await self._doc.find_one(filtro).update(
            {
                "$pull": {"members": {"user_id": user_id}},
                "$set": {"updated_at": datetime.now(UTC)},
            },
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return doc is not None


def _quedan_dos_owners() -> dict[str, t.Any]:
    """
    La expresión `$expr` que exige **más de un** `owner` en el array del propio documento.

    Es el equivalente exacto de la subconsulta correlacionada `EXISTS` del backend de SQL, y la
    razón por la que los miembros están embebidos: sin eso, esta condición cruzaría documentos y
    haría falta una transacción.
    """
    return {
        "$gt": [
            {
                "$size": {
                    "$filter": {
                        "input": "$members",
                        "cond": {"$eq": ["$$this.role", str(OrgRole.OWNER)]},
                    }
                }
            },
            1,
        ]
    }


# ── Invitaciones ──────────────────────────────────────────────────────────────
class BeanieInvitationRepository(AbstractInvitationRepository):
    """`AbstractInvitationRepository` sobre Beanie."""

    _doc: t.Any

    def __init__(self, *, document: type | None = None) -> None:
        self._doc = document or InvitationDocument

    async def add(self, invitation: Invitation) -> Invitation:
        doc = self._doc(
            entity_id=invitation.id,
            organization_id=invitation.organization_id,
            email=invitation.email,
            role=str(invitation.role),
            invited_by=invitation.invited_by,
            token_hash=invitation.token_hash,
            status=str(invitation.status),
            expires_at=invitation.expires_at,
        )
        await doc.insert()
        return _a_invitacion(doc)

    async def get(self, invitation_id: UUID) -> Invitation | None:
        doc = await self._doc.find_one(self._doc.entity_id == invitation_id)
        return _a_invitacion(doc) if doc is not None else None

    async def get_by_token_hash(self, token_hash: str) -> Invitation | None:
        doc = await self._doc.find_one(self._doc.token_hash == token_hash)
        return _a_invitacion(doc) if doc is not None else None

    async def list_pending(self, organization_id: UUID) -> list[Invitation]:
        docs = await self._doc.find(
            self._doc.organization_id == organization_id,
            self._doc.status == str(InvitationStatus.PENDING),
        ).to_list()
        return [_a_invitacion(d) for d in sorted(docs, key=lambda d: d.created_at)]

    async def consume(self, token_hash: str, *, at: datetime) -> Invitation | None:
        """
        Canjea la invitación en un solo `findOneAndUpdate`.

        Sin la atomicidad, dos aceptaciones concurrentes del mismo link crearían dos membresías —
        y aunque el `$push` guardado del repositorio de miembros las frenaría, el error saldría de
        ahí y no de acá, que es donde el usuario lo entiende.
        """
        doc = await self._doc.find_one(
            self._doc.token_hash == token_hash,
            self._doc.status == str(InvitationStatus.PENDING),
            self._doc.expires_at > at,
        ).update(
            {
                "$set": {
                    "status": str(InvitationStatus.ACCEPTED),
                    "consumed_at": at,
                    "updated_at": datetime.now(UTC),
                }
            },
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return _a_invitacion(doc) if doc is not None else None

    async def revoke(self, invitation_id: UUID, *, at: datetime) -> bool:
        """Marca revocada **sólo si estaba pendiente**. `False` si ya no lo estaba."""
        doc = await self._doc.find_one(
            self._doc.entity_id == invitation_id,
            self._doc.status == str(InvitationStatus.PENDING),
        ).update(
            {
                "$set": {
                    "status": str(InvitationStatus.REVOKED),
                    "consumed_at": at,
                    "updated_at": datetime.now(UTC),
                }
            },
            response_type=UpdateResponse.NEW_DOCUMENT,
        )
        return doc is not None


# ── Mapeo ─────────────────────────────────────────────────────────────────────
def _a_organizacion(doc: t.Any) -> Organization:
    from hexcore.darwin.infrastructure.orms.beanie.repositories import to_utc

    return Organization(
        id=doc.entity_id,
        name=doc.name,
        slug=doc.slug,
        metadata=dict(doc.org_metadata or {}),
        created_at=to_utc(doc.created_at),
    )


def _a_miembro(embebido: t.Any, organization_id: UUID) -> Member:
    from hexcore.darwin.infrastructure.orms.beanie.repositories import to_utc

    return Member(
        id=embebido.entity_id,
        organization_id=organization_id,
        user_id=embebido.user_id,
        role=OrgRole(embebido.role),
        created_at=to_utc(embebido.created_at),
    )


def _buscar_miembro(
    doc: t.Any, organization_id: UUID, user_id: UUID
) -> Member | None:
    """
    El miembro dentro del array, o `None`.

    Se filtra del lado del cliente y no con una proyección `$elemMatch`: la proyección traería sólo
    el elemento, pero complica el mapeo por un ahorro que en un documento de este tamaño no se
    nota. Ver `list_for_user`.
    """
    for embebido in doc.members:
        if embebido.user_id == user_id:
            return _a_miembro(embebido, organization_id)
    return None


def _a_invitacion(doc: t.Any) -> Invitation:
    from hexcore.darwin.infrastructure.orms.beanie.repositories import to_utc

    return Invitation(
        id=doc.entity_id,
        organization_id=doc.organization_id,
        email=doc.email,
        role=OrgRole(doc.role),
        invited_by=doc.invited_by,
        token_hash=doc.token_hash,
        status=InvitationStatus(doc.status),
        expires_at=to_utc(doc.expires_at) or datetime.now(UTC),
        consumed_at=to_utc(doc.consumed_at),
        created_at=to_utc(doc.created_at),
    )


# ── El contrato del backend ───────────────────────────────────────────────────
OrganizationRepository = BeanieOrganizationRepository
MemberRepository = BeanieMemberRepository
InvitationRepository = BeanieInvitationRepository


# ── El contrato de esquema ───────────────────────────────────────────
# El nombre neutro que busca `identity_documents`. En Mongo no es una comodidad: un `Document`
# que `init_beanie` no ve no funciona, asi que sin esto el plugin arranca y falla en la primera
# consulta con `CollectionWasNotInitialized`.
PLUGIN_DOCUMENTS = ORGANIZATION_DOCUMENTS
