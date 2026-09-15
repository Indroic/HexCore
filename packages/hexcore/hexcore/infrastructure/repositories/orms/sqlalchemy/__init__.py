from __future__ import annotations
import typing as t
from uuid import uuid4, UUID as PythonUUID
from datetime import datetime, UTC
from sqlalchemy import UUID, DateTime, Boolean, MetaData

from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase

from hexcore.domain.base import BaseEntity


T = t.TypeVar("T", bound=BaseEntity)


#: Convención de nombres de constraints.
#:
#: SQLAlchemy sólo trae `ix` por defecto, así que uniques, checks, FKs y PKs quedaban con
#: el nombre que les asignara el backend. Eso trae dos problemas concretos:
#:
#: - **SQLite no puede dropear un constraint sin nombre**, así que una migración de bajada
#:   que lo intente falla y no hay forma de escribirla a mano.
#: - Los nombres difieren entre backends, así que la misma migración no se comporta igual
#:   en el SQLite de desarrollo que en el PostgreSQL de producción.
#:
#: Se declara **antes** de la primera tabla que use uniques o FKs —las de identidad—
#: porque agregarla después es en sí una migración rompedora: hay que renombrar todo
#: constraint ya existente, y para eso hace falta poder nombrarlos, que es justamente lo
#: que falta.
#:
#: `ix` se deja **exactamente** como el default de SQLAlchemy a propósito. Cambiarlo haría
#: que el próximo `--autogenerate` de todo proyecto existente quisiera renombrar cada
#: índice que ya tiene, y no compra nada.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class BaseModel(Base, t.Generic[T]):
    __abstract__ = True
    __tablename__ = "base_model"

    id: Mapped[PythonUUID] = mapped_column(UUID, primary_key=True, default=uuid4)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    _domain_entity: T

    def set_domain_entity(self, entity: T) -> None:
        self._domain_entity = entity

    def get_domain_entity(self) -> T:
        return self._domain_entity

    def __repr__(self):
        return f"<{self.__class__.__name__}(id={self.id!r})>"
