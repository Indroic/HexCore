import abc
import typing as t
from abc import ABC, abstractmethod
from hexcore.application.dtos.base import DTO as DTO

T = t.TypeVar('T', bound=DTO)
R = t.TypeVar('R', bound=DTO)

class UseCase(ABC, t.Generic[T, R], metaclass=abc.ABCMeta):
    @abstractmethod
    async def execute(self, command: T) -> R: ...
