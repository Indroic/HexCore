import typing as t
from . import IUnitOfWork
from hexcore.infrastructure.repositories.base import IBaseRepository

T = t.TypeVar("T", bound=IBaseRepository[t.Any])


def get_repository(uow: IUnitOfWork, repo_name: str, repo_type: t.Type[T]) -> T:
    try:
        return t.cast(T, getattr(uow, repo_name))
    except AttributeError as exc:
        available_repositories = sorted(
            name
            for name, value in vars(uow).items()
            if isinstance(value, IBaseRepository)
        )
        available_text = ", ".join(available_repositories) or "ninguno"
        raise AttributeError(
            f"El repositorio '{repo_name}' ({repo_type.__name__}) no esta disponible en "
            f"{uow.__class__.__name__}. Repositorios detectados: {available_text}."
        ) from exc
