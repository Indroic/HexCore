from hexcore.domain.events import IEventDispatcher
from hexcore.config import LazyConfig


class BaseDomainService:
    def __init__(
        self,
        event_dispatcher: IEventDispatcher = LazyConfig().get_config().event_dispatcher,
    ) -> None:
        self.config = LazyConfig.get_config()
        self.event_dispatcher = event_dispatcher
