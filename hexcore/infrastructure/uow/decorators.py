import typing as t
from functools import wraps
from hexcore.types import SelfRepoT, EntityT


def register_entity_on_uow(
    method: t.Callable[[SelfRepoT, EntityT], t.Awaitable[EntityT]],
) -> t.Callable[[SelfRepoT, EntityT], t.Awaitable[EntityT]]:
    @wraps(method)
    async def wrapper(self: SelfRepoT, entity: EntityT) -> EntityT:
        result = await method(self, entity)
        if getattr(entity, "_domain_events", None):
            self.uow.collect_entity(entity)
        return result

    return wrapper
