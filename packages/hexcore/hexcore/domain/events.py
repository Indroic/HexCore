from __future__ import annotations
import typing as t
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
        """
        Etiqueta legible del evento, para el ruteo de AMQP y para los logs.

        Usa `removesuffix` y no `replace`, que quitaba **todas** las apariciones:
        `EventLogCreatedEvent` daba `"LOGCREATED"` en vez de `"EVENTLOGCREATED"`, y cualquier
        evento con "Event" en el medio del nombre quedaba mal ruteado.

        **El event store no usa esto.** Persiste el FQN (`build_fqn`), que es único entre
        módulos y es lo que el serializador sabe resolver; `event_name` no cumple ninguna de
        las dos cosas. Sirve como routing key de AMQP —donde el nombre corto es la
        convención— y como etiqueta en un log.
        """
        return self.__class__.__name__.removesuffix("Event").upper()

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
