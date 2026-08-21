"""
Adaptadores SQLAlchemy de los tres puertos de `organization`.

Dos operaciones importan y las dos son de una sola sentencia:

- `count_by_role`, que se cuenta **en la base** y no filtrando una lista en Python. Es la consulta
  de la invariante del último `owner`, y con dos peticiones concurrentes que degradan a los dos
  últimos, el conteo en memoria deja a la organización sin ninguno.
- `consume` de la invitación, un ``UPDATE ... WHERE status = 'pending' RETURNING``: dos
  aceptaciones concurrentes del mismo link crearían dos membresías, y el `UNIQUE` las rechazaría
  con un error de base en vez de con un mensaje.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import aliased

from hexcore.darwin.plugins.organization.domain import (
    AbstractInvitationRepository,
    AbstractMemberRepository,
    AbstractOrganizationRepository,
    Invitation,
    InvitationStatus,
    Member,
    Organization,
    OrgRole,
)

__all__ = [
    "SqlAlchemyOrganizationRepository",
    "SqlAlchemyMemberRepository",
    "SqlAlchemyInvitationRepository",
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


class _Base:
    """Base común. Modelo y scope inyectables, igual que el resto de la persistencia de Darwin."""

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


class SqlAlchemyOrganizationRepository(_Base, AbstractOrganizationRepository):
    """`AbstractOrganizationRepository` sobre SQLAlchemy."""

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.plugins.organization.models import OrganizationModel

        return OrganizationModel

    async def add(self, organization: Organization) -> Organization:
        async with self._session_scope() as session:
            fila = self._model(
                id=organization.id,
                name=organization.name,
                slug=organization.slug,
                org_metadata=dict(organization.metadata),
            )
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_organizacion(fila)

    async def get(self, organization_id: UUID) -> Organization | None:
        async with self._session_scope() as session:
            fila = await session.get(self._model, organization_id)
            return _a_organizacion(fila) if fila is not None else None

    async def get_by_slug(self, slug: str) -> Organization | None:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(self._model.slug == slug)
            )
            fila = resultado.scalar_one_or_none()
            return _a_organizacion(fila) if fila is not None else None

    async def update(self, organization: Organization) -> Organization:
        """Actualiza nombre y metadata. **El slug no se toca** — ver `Organization`."""
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(self._model.id == organization.id)
                .values(
                    name=organization.name,
                    org_metadata=dict(organization.metadata),
                )
                .returning(self._model)
            )
            fila = resultado.scalar_one_or_none()
            await session.commit()
            if fila is None:
                from hexcore.darwin.plugins.organization.domain import (
                    OrganizationNotFoundError,
                )

                raise OrganizationNotFoundError("No existe esa organización.")
            return _a_organizacion(fila)

    async def delete(self, organization_id: UUID) -> bool:
        async with self._session_scope() as session:
            resultado = await session.execute(
                delete(self._model)
                .where(self._model.id == organization_id)
                .returning(self._model.id)
            )
            borro = resultado.scalar_one_or_none() is not None
            await session.commit()
            return borro


class SqlAlchemyMemberRepository(_Base, AbstractMemberRepository):
    """`AbstractMemberRepository` sobre SQLAlchemy."""

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.plugins.organization.models import MemberModel

        return MemberModel

    async def add(self, member: Member) -> Member:
        async with self._session_scope() as session:
            fila = self._model(
                id=member.id,
                organization_id=member.organization_id,
                user_id=member.user_id,
                role=str(member.role),
            )
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_miembro(fila)

    async def get(self, organization_id: UUID, user_id: UUID) -> Member | None:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(
                    self._model.organization_id == organization_id,
                    self._model.user_id == user_id,
                )
            )
            fila = resultado.scalar_one_or_none()
            return _a_miembro(fila) if fila is not None else None

    async def list_for_organization(self, organization_id: UUID) -> list[Member]:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model)
                .where(self._model.organization_id == organization_id)
                .order_by(self._model.created_at)
            )
            return [_a_miembro(f) for f in resultado.scalars().all()]

    async def list_for_user(self, user_id: UUID) -> list[Member]:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model)
                .where(self._model.user_id == user_id)
                .order_by(self._model.created_at)
            )
            return [_a_miembro(f) for f in resultado.scalars().all()]

    async def count_by_role(self, organization_id: UUID, role: OrgRole) -> int:
        """
        Cuenta **en la base**. Ver el docstring del módulo y el del puerto.
        """
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(func.count())
                .select_from(self._model)
                .where(
                    self._model.organization_id == organization_id,
                    self._model.role == str(role),
                )
            )
            return int(resultado.scalar_one())

    def _quedan_otros_owners(self, organization_id: UUID, user_id: UUID) -> t.Any:
        """
        La condición "hay otro `owner` además de éste", como subconsulta correlacionada.

        ⚠️ Va **adentro** del `WHERE` de la sentencia que degrada o saca, y ese es todo el punto:
        contar antes y actualizar después es check-then-act, y dos degradaciones concurrentes
        dejarían la organización sin ningún `owner`. Con la subconsulta, la decisión la toma la
        base en una sola sentencia.

        Se escribe como `EXISTS` y no como `COUNT(*) > 1` porque el motor puede cortar en la
        primera fila que encuentra — y porque expresa la pregunta que se está haciendo: no cuántos
        hay, sino si queda **otro**.
        """
        otro = aliased(self._model)
        return (
            select(otro.id)
            .where(
                otro.organization_id == organization_id,
                otro.role == str(OrgRole.OWNER),
                otro.user_id != user_id,
            )
            .exists()
        )

    async def set_role(
        self,
        organization_id: UUID,
        user_id: UUID,
        *,
        role: OrgRole,
        keep_last_owner: bool = False,
    ) -> Member | None:
        condicion: list[t.Any] = [
            self._model.organization_id == organization_id,
            self._model.user_id == user_id,
        ]
        if keep_last_owner:
            # El guardián sólo aplica si el cambio **saca** un owner: ascender a owner nunca puede
            # dejar la organización sin ninguno.
            condicion.append(
                or_(
                    self._model.role != str(OrgRole.OWNER),
                    self._quedan_otros_owners(organization_id, user_id),
                )
            )

        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(*condicion)
                .values(role=str(role))
                .returning(self._model)
            )
            fila = resultado.scalar_one_or_none()
            await session.commit()
            return _a_miembro(fila) if fila is not None else None

    async def remove(
        self, organization_id: UUID, user_id: UUID, *, keep_last_owner: bool = False
    ) -> bool:
        condicion: list[t.Any] = [
            self._model.organization_id == organization_id,
            self._model.user_id == user_id,
        ]
        if keep_last_owner:
            condicion.append(
                or_(
                    self._model.role != str(OrgRole.OWNER),
                    self._quedan_otros_owners(organization_id, user_id),
                )
            )

        async with self._session_scope() as session:
            resultado = await session.execute(
                delete(self._model).where(*condicion).returning(self._model.id)
            )
            saco = resultado.scalar_one_or_none() is not None
            await session.commit()
            return saco


class SqlAlchemyInvitationRepository(_Base, AbstractInvitationRepository):
    """`AbstractInvitationRepository` sobre SQLAlchemy."""

    @staticmethod
    def _modelo_por_defecto() -> type:
        from hexcore.darwin.plugins.organization.models import InvitationModel

        return InvitationModel

    async def add(self, invitation: Invitation) -> Invitation:
        async with self._session_scope() as session:
            fila = self._model(
                id=invitation.id,
                organization_id=invitation.organization_id,
                email=invitation.email,
                role=str(invitation.role),
                invited_by=invitation.invited_by,
                token_hash=invitation.token_hash,
                status=str(invitation.status),
                expires_at=invitation.expires_at,
            )
            session.add(fila)
            await session.commit()
            await session.refresh(fila)
            return _a_invitacion(fila)

    async def get(self, invitation_id: UUID) -> Invitation | None:
        async with self._session_scope() as session:
            fila = await session.get(self._model, invitation_id)
            return _a_invitacion(fila) if fila is not None else None

    async def list_pending(self, organization_id: UUID) -> list[Invitation]:
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model)
                .where(
                    self._model.organization_id == organization_id,
                    self._model.status == str(InvitationStatus.PENDING),
                )
                .order_by(self._model.created_at)
            )
            return [_a_invitacion(f) for f in resultado.scalars().all()]

    async def get_by_token_hash(self, token_hash: str) -> Invitation | None:
        """Por el hash del token, que es `UNIQUE` — así que la consulta va por índice."""
        async with self._session_scope() as session:
            resultado = await session.execute(
                select(self._model).where(self._model.token_hash == token_hash)
            )
            fila = resultado.scalar_one_or_none()
            return _a_invitacion(fila) if fila is not None else None

    async def consume(self, token_hash: str, *, at: datetime) -> Invitation | None:
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(
                    self._model.token_hash == token_hash,
                    self._model.status == str(InvitationStatus.PENDING),
                    self._model.expires_at > at,
                )
                .values(status=str(InvitationStatus.ACCEPTED), consumed_at=at)
                .returning(self._model)
            )
            fila = resultado.scalar_one_or_none()
            await session.commit()
            return _a_invitacion(fila) if fila is not None else None

    async def revoke(self, invitation_id: UUID, *, at: datetime) -> bool:
        async with self._session_scope() as session:
            resultado = await session.execute(
                update(self._model)
                .where(
                    self._model.id == invitation_id,
                    self._model.status == str(InvitationStatus.PENDING),
                )
                .values(status=str(InvitationStatus.REVOKED), consumed_at=at)
                .returning(self._model.id)
            )
            revoco = resultado.scalar_one_or_none() is not None
            await session.commit()
            return revoco


def _a_organizacion(fila: t.Any) -> Organization:
    crudo: dict[str, t.Any] = dict(fila.org_metadata or {})
    return Organization(
        id=fila.id,
        name=fila.name,
        slug=fila.slug,
        metadata=crudo,
        created_at=_aware(fila.created_at),
    )


def _a_miembro(fila: t.Any) -> Member:
    return Member(
        id=fila.id,
        organization_id=fila.organization_id,
        user_id=fila.user_id,
        role=OrgRole(fila.role),
        created_at=_aware(fila.created_at),
    )


def _a_invitacion(fila: t.Any) -> Invitation:
    return Invitation(
        id=fila.id,
        organization_id=fila.organization_id,
        email=fila.email,
        role=OrgRole(fila.role),
        invited_by=fila.invited_by,
        token_hash=fila.token_hash,
        status=InvitationStatus(fila.status),
        expires_at=_aware(fila.expires_at) or datetime.now(UTC),
        consumed_at=_aware(fila.consumed_at),
        created_at=_aware(fila.created_at),
    )
