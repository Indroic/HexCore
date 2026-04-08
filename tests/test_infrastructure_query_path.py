import asyncio
import typing as t
from unittest.mock import AsyncMock, patch

from hexcore.application.dtos.query import QueryRequestDTO
from hexcore.domain.base import BaseEntity
from hexcore.domain.repositories import IBaseRepository
from hexcore.domain.services import BaseDomainService
from hexcore.domain.uow import IUnitOfWork
from hexcore.infrastructure.repositories.implementations import (
    BeanieODMCommonImplementationsRepo,
    SQLAlchemyCommonImplementationsRepo,
)


class _Entity(BaseEntity):
    name: str


class _SqlRepo(SQLAlchemyCommonImplementationsRepo[_Entity, t.Any]):
    @property
    def entity_cls(self):
        return _Entity

    @property
    def model_cls(self):
        return t.cast(t.Any, object)

    @property
    def not_found_exception(self):
        return ValueError


class _NoSqlRepo(BeanieODMCommonImplementationsRepo[_Entity, t.Any]):
    @property
    def entity_cls(self):
        return _Entity

    @property
    def document_cls(self):
        return t.cast(t.Any, object)

    @property
    def not_found_exception(self):
        return ValueError


class _FakeUoW:
    def __init__(self) -> None:
        self.session = object()


def test_base_domain_service_prefers_repository_query_all() -> None:
    class _Repo:
        async def query_all(
            self, query: QueryRequestDTO
        ) -> tuple[list[_Entity], int]:
            del query
            return [_Entity(name="Ana")], 3

        async def list_all(
            self, limit: int | None = None, offset: int = 0
        ) -> list[_Entity]:
            del limit, offset
            raise AssertionError("No debe usar list_all cuando query_all existe")

    async def _run() -> None:
        service = BaseDomainService()
        response = await service.list_entities(
            t.cast(IBaseRepository[_Entity], _Repo()),
            QueryRequestDTO(
                limit=1,
                offset=0,
                search=None,
                search_fields=[],
                filters=[],
                sort=[],
            ),
        )

        assert response.total == 3
        assert response.has_next is True
        assert len(response.items) == 1
        assert response.items[0].name == "Ana"

    asyncio.run(_run())


def test_sqlalchemy_common_repo_query_all_delegates_to_db_query() -> None:
    async def _run() -> None:
        repo = _SqlRepo(t.cast(IUnitOfWork, _FakeUoW()))
        query = QueryRequestDTO(
            limit=10,
            offset=0,
            search=None,
            search_fields=[],
            filters=[],
            sort=[],
        )

        with (
            patch(
                "hexcore.infrastructure.repositories.implementations.sql_db_query",
                new=AsyncMock(return_value=([object(), object()], 2)),
            ) as mock_query,
            patch(
                "hexcore.infrastructure.repositories.implementations.to_entity_from_model_or_document",
                new=AsyncMock(side_effect=[_Entity(name="A"), _Entity(name="B")]),
            ) as mock_to_entity,
        ):
            entities, total = await repo.query_all(query)

        assert total == 2
        assert [item.name for item in entities] == ["A", "B"]
        mock_query.assert_awaited_once()
        assert mock_to_entity.await_count == 2

    asyncio.run(_run())


def test_beanie_common_repo_query_all_delegates_to_db_query() -> None:
    async def _run() -> None:
        repo = _NoSqlRepo(t.cast(IUnitOfWork, _FakeUoW()))
        query = QueryRequestDTO(
            limit=10,
            offset=0,
            search=None,
            search_fields=[],
            filters=[],
            sort=[],
        )

        with (
            patch(
                "hexcore.infrastructure.repositories.implementations.nosql_db_query",
                new=AsyncMock(return_value=([object(), object()], 2)),
            ) as mock_query,
            patch(
                "hexcore.infrastructure.repositories.implementations.to_entity_from_model_or_document",
                new=AsyncMock(side_effect=[_Entity(name="X"), _Entity(name="Y")]),
            ) as mock_to_entity,
        ):
            entities, total = await repo.query_all(query)

        assert total == 2
        assert [item.name for item in entities] == ["X", "Y"]
        mock_query.assert_awaited_once()
        assert mock_to_entity.await_count == 2

    asyncio.run(_run())
