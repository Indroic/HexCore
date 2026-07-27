"""
Contratos para Handlers de Commands y Queries.
"""
from __future__ import annotations

import abc
import typing as t

if t.TYPE_CHECKING:
    from .commands import Command
    from .queries import Query

TCommand = t.TypeVar("TCommand", bound="Command")
TCommandResult = t.TypeVar("TCommandResult")
TQuery = t.TypeVar("TQuery", bound="Query[t.Any]")
TResult = t.TypeVar("TResult")


class AbstractCommandHandler(abc.ABC, t.Generic[TCommand, TCommandResult]):
    """
    Contrato para un handler que procesa un tipo específico de Command.

    Type Parameters:
        TCommand: El tipo concreto de Command que este handler procesa.
        TCommandResult: El tipo de resultado tras ejecutar el command.
                        Usar ``None`` para commands fire-and-forget.
    """

    @abc.abstractmethod
    async def handle(self, command: TCommand) -> TCommandResult:
        """Ejecuta la lógica de negocio asociada al command."""
        raise NotImplementedError


class AbstractQueryHandler(abc.ABC, t.Generic[TQuery, TResult]):
    """
    Contrato para un handler que procesa un tipo específico de Query.

    Type Parameters:
        TQuery: El tipo concreto de Query que este handler procesa.
        TResult: El tipo de dato retornado por la query.
    """

    @abc.abstractmethod
    async def handle(self, query: TQuery) -> TResult:
        """Ejecuta la consulta y retorna el resultado."""
        raise NotImplementedError


# Aliases de retrocompatibilidad
ICommandHandler = AbstractCommandHandler
IQueryHandler = AbstractQueryHandler
