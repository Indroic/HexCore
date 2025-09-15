import typing as t
from hexcore.domain.events import IEventDispatcher, DomainEvent


class InMemoryEventDispatcher(IEventDispatcher):
    def __init__(self) -> None:
        self._events: list[tuple[str, dict[str, t.Any]]] = []

    async def dispatch(self, event: DomainEvent) -> None:
        self._events.append((event.__class__.__name__, event.model_dump()))

    def register(
        self,
        event_type: t.Type[DomainEvent],
        handler: t.Callable[[DomainEvent], t.Awaitable[None]],
    ) -> None:
        pass

    def clear_events(self) -> None:
        self._events.clear()
