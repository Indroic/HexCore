from __future__ import annotations
import typing as t
import abc
from datetime import datetime, UTC
from uuid import UUID, uuid4
from pydantic import BaseModel, Field, ConfigDict, computed_field

from .base import BaseEntity

T = t.TypeVar("T", bound=BaseEntity)


class DomainEvent(BaseModel):
    """
    Clase base abstracta para todos los eventos de dominio.
    Los eventos de dominio representan algo significativo que ha ocurrido en el dominio.
    """

    # Identificador único del evento
    event_id: UUID = Field(default_factory=uuid4)
    # Marca de tiempo de cuándo ocurrió el evento
    occurred_on: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @computed_field
    @property
    def event_name(self) -> str:
        """Nombre de la clase del evento, usado para serialización/deserialización."""
        return self.__class__.__name__.replace("Event", "").upper()

    model_config = ConfigDict(
        from_attributes=True,
        frozen=True,  # Los eventos de dominio son inmutables
    )


class EntityCreatedEvent(DomainEvent, t.Generic[T]):
    """Evento base para cuando una entidad es creada."""

    entity_id: UUID
    entity_data: T


class EntityUpdatedEvent(DomainEvent, t.Generic[T]):
    """Evento base para cuando una entidad es actualizada."""

    entity_id: UUID
    entity_data: T


class EntityDeletedEvent(DomainEvent):
    """Evento base para cuando una entidad es eliminada."""

    entity_id: UUID


EventHandler = t.Callable[[DomainEvent], t.Awaitable[None]]


class IEventDispatcher(abc.ABC):
    """
    Interfaz (Puerto) para el despachador de eventos.
    """

    @abc.abstractmethod
    def register(self, event_type: type, handler: EventHandler) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def dispatch(self, event: t.Any) -> None:
        raise NotImplementedError
