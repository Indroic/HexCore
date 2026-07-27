import typing as t
from hexcore.domain.events import EventBus, DomainEvent


class InMemoryEventBus(EventBus):
    """
    Implementación básica en memoria del EventBus.
    Mantiene un diccionario de tipo_de_evento -> lista_de_handlers.
    """

    def __init__(self) -> None:
        # dict[type, list[EventHandler]]
        self._handlers: dict[type, list[t.Callable[[DomainEvent], t.Awaitable[None]]]] = {}

    def subscribe(
        self, event_type: type, handler: t.Callable[[DomainEvent], t.Awaitable[None]]
    ) -> None:
        """
        Registra un handler asíncrono para un tipo de evento específico.
        """
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    async def publish(self, event: DomainEvent) -> None:
        if event.__class__ in self._handlers:
            for handler in self._handlers[event.__class__]:
                await handler(event)


# Alias de retrocompatibilidad
InMemoryEventDispatcher = InMemoryEventBus
