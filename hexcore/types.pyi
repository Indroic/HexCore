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
    async def __call__(self, model: A, *, visited: set[str] | None = None, **kwargs: t.Any) -> t.Any: ...

VisitedType: t.TypeAlias
VisitedResultsType: t.TypeAlias
AsyncCycleResolver: t.TypeAlias
FieldResolversType: t.TypeAlias
FieldSerializersType: t.TypeAlias
ExcludeType: t.TypeAlias
