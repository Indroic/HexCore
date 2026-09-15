"""
Los mixins de `organization`. **Importarlos no registra ninguna tabla.**

Tres tablas: la organización, la membresía y la invitación. Igual que con los mixins del núcleo, el
consumidor declara las clases concretas en su propio paquete ``models/``, porque un
`--autogenerate` que no ve la tabla le emite ``op.drop_table``.

Uso, en el paquete del consumidor::

    # myapp/models/identity.py
    from hexcore.darwin.plugins.organization import (
        InvitationMixin,
        MemberMixin,
        OrganizationMixin,
    )
    from hexcore.sql import Base

    class Organization(OrganizationMixin, Base):
        __tablename__ = "darwin_organization"

    class Member(MemberMixin, Base):
        __tablename__ = "darwin_member"

    class Invitation(InvitationMixin, Base):
        __tablename__ = "darwin_invitation"
"""
from __future__ import annotations

import typing as t
from datetime import datetime
from uuid import UUID as PythonUUID, uuid4

from sqlalchemy import (
    JSON,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from hexcore.darwin.infrastructure.orms.sqlalchemy.models_mixins import (
    DEFAULT_USER_TABLE,
    TimestampMixin,
)

__all__ = [
    "DEFAULT_ORGANIZATION_TABLE",
    "DEFAULT_MEMBER_TABLE",
    "DEFAULT_INVITATION_TABLE",
    "OrganizationMixin",
    "MemberMixin",
    "InvitationMixin",
]

DEFAULT_ORGANIZATION_TABLE = "darwin_organization"
DEFAULT_MEMBER_TABLE = "darwin_member"
DEFAULT_INVITATION_TABLE = "darwin_invitation"


class OrganizationMixin(TimestampMixin):
    """Columnas de `organization`."""

    #: Lo declara la clase concreta. Se anota acá —sin asignar— para que el type checker sepa que
    #: existe cuando `__table_args__` lo usa. SQLAlchemy saltea las anotaciones `ClassVar`.
    __tablename__: t.ClassVar[str]

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    #: Lo que va en la URL. Único, y **no se cambia por el endpoint de actualización**: un slug
    #: que cambia rompe cada link guardado, cada bookmark y cada integración que lo tenga fijo.
    slug: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Datos del consumidor: plan, límites, configuración. `JSON` y no columnas porque cada
    #: producto necesita otras, y agregarlas al framework las volvería un contrato.
    org_metadata: Mapped[t.Any] = mapped_column(
        "metadata_", JSON, nullable=False, default=dict
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (UniqueConstraint("slug", name=f"uq_{nombre}_slug"),)


class MemberMixin(TimestampMixin):
    """
    Columnas de `member`: alguien dentro de una organización, con su rol.

    El `UNIQUE(organization_id, user_id)` es lo que hace que aceptar dos veces la misma invitación
    no cree dos membresías con roles distintos — que serían dos respuestas a "¿qué rol tiene?"
    según el orden en que la consulta las devuelva.
    """

    __tablename__: t.ClassVar[str]
    __darwin_user_table__: t.ClassVar[str] = DEFAULT_USER_TABLE
    __darwin_organization_table__: t.ClassVar[str] = DEFAULT_ORGANIZATION_TABLE

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)

    @declared_attr
    def organization_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        return mapped_column(
            ForeignKey(
                f"{cls.__darwin_organization_table__}.id", ondelete="CASCADE"
            ),
            nullable=False,
        )

    @declared_attr
    def user_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        return mapped_column(
            ForeignKey(f"{cls.__darwin_user_table__}.id", ondelete="CASCADE"),
            nullable=False,
        )

    #: `owner`, `admin` o `member`. `String` y no `Enum` de base: agregar un rol con un `Enum`
    #: nativo es una migración de tipo en Postgres, y el chequeo lo hace el dominio igual.
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member")

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            UniqueConstraint(
                "organization_id", "user_id", name=f"uq_{nombre}_org_user"
            ),
            Index(f"ix_{nombre}_user_id", "user_id"),
            # Para el conteo de owners, que es la consulta de la invariante más importante.
            Index(f"ix_{nombre}_organization_id_role", "organization_id", "role"),
        )


class InvitationMixin(TimestampMixin):
    """
    Columnas de `invitation`.

    **`token_hash` y no el token**: el link viaja por mail y queda en el buzón, en los logs del
    proveedor y en el historial del cliente. Un dump de esta tabla no debería sumar la capacidad de
    entrar a una organización ajena.
    """

    __tablename__: t.ClassVar[str]
    __darwin_user_table__: t.ClassVar[str] = DEFAULT_USER_TABLE
    __darwin_organization_table__: t.ClassVar[str] = DEFAULT_ORGANIZATION_TABLE

    id: Mapped[PythonUUID] = mapped_column(primary_key=True, default=uuid4)

    @declared_attr
    def organization_id(cls) -> Mapped[PythonUUID]:  # noqa: N805
        return mapped_column(
            ForeignKey(
                f"{cls.__darwin_organization_table__}.id", ondelete="CASCADE"
            ),
            nullable=False,
        )

    @declared_attr
    def invited_by(cls) -> Mapped[PythonUUID]:  # noqa: N805
        """Quién invitó. Es información de auditoría: sin ella, "¿quién dejó entrar a X?" no tiene
        respuesta."""
        return mapped_column(
            ForeignKey(f"{cls.__darwin_user_table__}.id", ondelete="CASCADE"),
            nullable=False,
        )

    #: El mail invitado. La invitación está **atada** a él: aceptarla exige que la cuenta tenga
    #: ese mail, porque si no, reenviar el link deja entrar a cualquiera.
    email: Mapped[str] = mapped_column(String(320), nullable=False)

    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member")

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    #: `pending`, `accepted` o `revoked`. Revocada y no borrada: una invitación revocada es
    #: información de auditoría — dice que alguien invitó y después se arrepintió.
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple[t.Any, ...]:  # noqa: N805
        nombre = cls.__tablename__
        return (
            UniqueConstraint("token_hash", name=f"uq_{nombre}_token_hash"),
            Index(f"ix_{nombre}_organization_id_status", "organization_id", "status"),
            Index(f"ix_{nombre}_expires_at", "expires_at"),
        )
