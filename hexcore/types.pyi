import typing as t
from .domain.base import BaseEntity as BaseEntity
from hexcore.infrastructure.repositories.base import IBaseRepository as IBaseRepository
from typing import Protocol

A = t.TypeVar('A', contravariant=True)
T = t.TypeVar('T', bound=BaseEntity)
SelfRepoT = t.TypeVar('SelfRepoT', bound=IBaseRepository[t.Any])
EntityT = t.TypeVar('EntityT', bound=BaseEntity)
P = t.ParamSpec('P')
R = t.TypeVar('R')

class AsyncResolver(Protocol[A]):
    async def __call__(self, model: A, *, visited: set[str] | None = ..., **kwargs: t.Any) -> t.Any: ...

VisitedType: t.TypeAlias = set[int]
VisitedResultsType: t.TypeAlias = dict[int, t.Any]
AsyncCycleResolver: t.TypeAlias = t.Callable[[A], t.Awaitable[t.Any]]
FieldResolversType: t.TypeAlias = t.Dict[str, t.Tuple[str, AsyncCycleResolver[A]]]
FieldSerializersType: t.TypeAlias = t.Dict[str, t.Tuple[str, t.Callable[[A], t.Any]]]
ExcludeType: t.TypeAlias = t.Optional[set[str]]
