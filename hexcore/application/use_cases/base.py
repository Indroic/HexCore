import typing as t
from abc import ABC, abstractmethod
from collections.abc import Sequence
from hexcore.application.dtos.base import DTO

type DTOType = DTO

T = t.TypeVar("T", bound=DTOType | Sequence[DTOType])

R = t.TypeVar("R")


class UseCase(ABC, t.Generic[T, R]):
    @abstractmethod
    async def execute(self, command: T) -> R:
        raise NotImplementedError("Subclasses must implement this method")
