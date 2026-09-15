from __future__ import annotations

import typing as t

from hexcore.application.dtos.query import QueryRequestDTO, QueryResponseDTO
from hexcore.domain.base import BaseEntity
from hexcore.domain.repositories import IBaseRepository
from hexcore.domain.services import BaseDomainService

from .base import UseCase

T = t.TypeVar("T", bound=BaseEntity)


class QueryEntitiesUseCase(UseCase[QueryRequestDTO, QueryResponseDTO], t.Generic[T]):
    def __init__(
        self,
        repository: IBaseRepository[T],
        service: BaseDomainService | None = None,
    ) -> None:
        self.repository = repository
        self.service = service or BaseDomainService()

    async def execute(self, command: QueryRequestDTO) -> QueryResponseDTO:
        return await self.service.list_entities(self.repository, command)


class ListEntitiesUseCase(QueryEntitiesUseCase[T]):
    async def execute(self, command: QueryRequestDTO) -> QueryResponseDTO:
        command.search = None
        command.search_fields = []
        return await super().execute(command)


class SearchEntitiesUseCase(QueryEntitiesUseCase[T]):
    async def execute(self, command: QueryRequestDTO) -> QueryResponseDTO:
        return await super().execute(command)
