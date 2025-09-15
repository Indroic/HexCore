import typing as t
from . import IUnitOfWork as IUnitOfWork
from hexcore.infrastructure.repositories.base import IBaseRepository as IBaseRepository

T = t.TypeVar('T', bound=IBaseRepository[t.Any])

def get_repository(uow: IUnitOfWork, repo_name: str, repo_type: type[T]) -> T: ...
