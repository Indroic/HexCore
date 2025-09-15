import typing as t
from typing import Protocol
from .domain.base import BaseEntity
from .infrastructure.repositories.orms.sqlalchemy import BaseModel
from .infrastructure.repositories.base import IBaseRepository
from uuid import UUID

# Tipos genéricos reutilizables para repositorios y decoradores
A = t.TypeVar("A", contravariant=True)
T = t.TypeVar("T", bound=BaseEntity)
SelfRepoT = t.TypeVar("SelfRepoT", bound=IBaseRepository[t.Any])
EntityT = t.TypeVar("EntityT", bound=BaseEntity)
P = t.ParamSpec("P")
R = t.TypeVar("R")


# Protocolo para resolvers asíncronos que aceptan visited como kwarg
class AsyncResolver(Protocol[A]):
    async def __call__(
        self, model: A, *, visited: t.Optional[set[str]] = None, **kwargs: t.Any
    ) -> t.Any: ...


# Tipos para los parámetros de contexto de protección de ciclo
VisitedType: t.TypeAlias = set[int]
VisitedResultsType: t.TypeAlias = dict[int, t.Any]
# Tipo para la firma de los resolvedores asíncronos con protección de ciclo (solo model)
AsyncCycleResolver: t.TypeAlias = t.Callable[[A], t.Awaitable[t.Any]]

FieldResolversType: t.TypeAlias = t.Dict[str, t.Tuple[str, AsyncCycleResolver[A]]]


FieldSerializersType: t.TypeAlias = t.Dict[str, t.Tuple[str, t.Callable[[A], t.Any]]]

ExcludeType: t.TypeAlias = t.Optional[set[str]]

RelationsType: t.TypeAlias = t.Dict[str, t.Tuple[t.Type[BaseModel[t.Any]], t.List[UUID]]]
