from __future__ import annotations
import abc
import typing as t

if t.TYPE_CHECKING:
    # Bajo `TYPE_CHECKING` y no en un `try/except ImportError` con una clase vacía de
    # respaldo, que es como estaba. `AsyncSession` acá sólo aparece en anotaciones, y con
    # `from __future__ import annotations` esas anotaciones son strings que nadie evalúa en
    # runtime: el import nunca hacía falta. Lo que sí hacía la clase vacía era ganarle la
    # resolución del nombre a la real —Pyright analiza las dos ramas y se queda con la
    # última—, así que `self.session` tipaba como un objeto sin métodos y todo repositorio
    # que la usara reportaba un `AsyncSession` incompatible con el `AsyncSession` de verdad.
    from sqlalchemy.ext.asyncio import AsyncSession

from hexcore.domain.base import BaseEntity
from hexcore.domain.repositories import IBaseRepository
from hexcore.domain.uow import IUnitOfWork

T = t.TypeVar("T", bound=BaseEntity)


class BaseSQLAlchemyRepository(IBaseRepository[T], abc.ABC, t.Generic[T]):
    def __init__(self, uow: IUnitOfWork):
        self._session: t.Optional["AsyncSession"] = getattr(uow, "session", None)

        super().__init__(uow)
        
    @property
    def session(self) -> "AsyncSession":
        if self._session is None:
            raise ValueError("El repositorio no está asociado a una sesión de base de datos.")
        return self._session
    
    
class BaseBeanieRepository(IBaseRepository[T], abc.ABC, t.Generic[T]):
    """Repositorio base para Beanie ODM. Similar a BaseSQLAlchemyRepository pero sin sesión."""
    pass
