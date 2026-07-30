"""
Adaptadores de compatibilidad entre UseCase existente y el sistema CQRS.
"""
from __future__ import annotations

import typing as t

from hexcore.application.use_cases.base import UseCase
from hexcore.domain.cqrs.handlers import AbstractCommandHandler
from hexcore.domain.cqrs.commands import Command


T = t.TypeVar("T", bound=Command)
R = t.TypeVar("R")


class UseCaseCommandHandler(AbstractCommandHandler[T, R]):
    """
    Adaptador que envuelve un UseCase existente como un AbstractCommandHandler.
    Permite migrar progresivamente sin reescribir use cases.

    Uso::

        existing_use_case = CreateUserUseCase(repository)
        handler = UseCaseCommandHandler(existing_use_case)
        registry.register_command_handler(CreateUserCommand, handler)
    """

    def __init__(self, use_case: UseCase[t.Any, R]) -> None:
        self._use_case = use_case

    async def handle(self, command: T) -> R:
        return await self._use_case.execute(command)
