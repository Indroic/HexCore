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


class EventBus(abc.ABC):
    """
    Puerto para publicar y suscribir eventos de dominio.
    Reemplaza al antiguo IEventDispatcher con una API simplificada.
    """

    @abc.abstractmethod
    def subscribe(self, event_type: type, handler: EventHandler) -> None:
        """Registra un handler para un tipo de evento."""
        raise NotImplementedError

    @abc.abstractmethod
    async def publish(self, event: t.Any) -> None:
        """Publica un evento a todos los handlers suscritos."""
        raise NotImplementedError

    # --- Retrocompatibilidad (deprecado desde 5.0, se elimina en 6.0) ---
    def register(self, event_type: type, handler: EventHandler) -> None:
        """Alias deprecado de `subscribe`."""
        from hexcore._deprecation import warn_deprecated

        warn_deprecated(
            "EventBus.register()", "EventBus.subscribe()", kind="método", stacklevel=2
        )
        self.subscribe(event_type, handler)

    async def dispatch(self, event: t.Any) -> None:
        """Alias deprecado de `publish`."""
        from hexcore._deprecation import warn_deprecated

        warn_deprecated(
            "EventBus.dispatch()", "EventBus.publish()", kind="método", stacklevel=2
        )
        await self.publish(event)


# ── Alias de retrocompatibilidad (deprecado desde 5.0, se elimina en 6.0) ──────
from hexcore._deprecation import deprecated_aliases  # noqa: E402

_DEPRECATED_ALIASES = {"IEventDispatcher": "EventBus"}

__getattr__ = deprecated_aliases(__name__, _DEPRECATED_ALIASES, globals())

if t.TYPE_CHECKING:
    IEventDispatcher = EventBus
