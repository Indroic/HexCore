import typing as t
from abc import ABC, abstractmethod
from hexcore.application.dtos.base import DTO

# Tipo del Input
T = t.TypeVar("T", bound=t.Union[DTO, list[DTO]])

# Tipo del Output(o resultado)
R = t.TypeVar("R", bound=t.Union[DTO, list[DTO]])


class UseCase(ABC, t.Generic[T, R]):
    @abstractmethod
    async def execute(self, command: T) -> R:
        raise NotImplementedError("Subclasses must implement this method")
