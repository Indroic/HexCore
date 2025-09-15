import typing as t
from hexcore.types import EntityT as EntityT, SelfRepoT as SelfRepoT

def register_entity_on_uow(method: t.Callable[[SelfRepoT, EntityT], t.Awaitable[EntityT]]) -> t.Callable[[SelfRepoT, EntityT], t.Awaitable[EntityT]]: ...
