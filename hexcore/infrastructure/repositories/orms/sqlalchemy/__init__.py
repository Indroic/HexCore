from __future__ import annotations
import typing as t
from uuid import uuid4, UUID as PythonUUID
from datetime import datetime, UTC
from sqlalchemy import UUID, DateTime, Boolean

from sqlalchemy.orm import Mapped, mapped_column, DeclarativeBase

from hexcore.domain.base import BaseEntity


T = t.TypeVar("T", bound=BaseEntity)


class Base(DeclarativeBase):
    pass


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
