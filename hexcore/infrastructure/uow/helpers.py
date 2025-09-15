import typing as t
from . import IUnitOfWork
from hexcore.infrastructure.repositories.base import IBaseRepository

T = t.TypeVar("T", bound=IBaseRepository[t.Any])


def get_repository(uow: IUnitOfWork, repo_name: str, repo_type: t.Type[T]) -> T:
    return t.cast(T, getattr(uow, repo_name))
