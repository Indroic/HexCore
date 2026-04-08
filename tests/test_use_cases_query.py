import asyncio
import typing as t

from fastapi import APIRouter, HTTPException

from hexcore.application.dtos.query import QueryRequestDTO, QueryResponseDTO
from hexcore.application.use_cases.query import QueryEntitiesUseCase
from hexcore.domain.base import BaseEntity
from hexcore.domain.repositories import IBaseRepository
from hexcore.domain.services import BaseDomainService
from hexcore.infrastructure.api.utils import (
    build_query_endpoint,
    register_query_endpoint,
)


class _Entity(BaseEntity):
    name: str
    age: int


class _Repo:
    async def list_all(
        self, limit: int | None = None, offset: int = 0
    ) -> list[_Entity]:
        del limit, offset
        return [
            _Entity(name="Ana", age=30),
            _Entity(name="Beto", age=20),
            _Entity(name="Carlos", age=40),
        ]


def test_query_use_case_executes_with_base_service() -> None:
    async def _run() -> None:
        use_case: QueryEntitiesUseCase[_Entity] = QueryEntitiesUseCase(  # type: ignore[arg-type]
            t.cast(IBaseRepository[_Entity], _Repo()), BaseDomainService()
        )
        response = await use_case.execute(
            QueryRequestDTO(
                limit=10,
                offset=0,
                search="a",
                search_fields=["name"],
                filters=[],
                sort=[],
            )
        )
        assert response.total == 2
        assert sorted(item.name for item in response.items) == ["Ana", "Carlos"]

    asyncio.run(_run())


def test_build_query_endpoint_parses_filters_and_sort() -> None:
    async def _run() -> None:
        use_case: QueryEntitiesUseCase[_Entity] = QueryEntitiesUseCase(  # type: ignore[arg-type]
            t.cast(IBaseRepository[_Entity], _Repo()), BaseDomainService()
        )

        def _factory() -> QueryEntitiesUseCase[_Entity]:
            return use_case

        endpoint = build_query_endpoint(_factory)

        response = await endpoint(
            limit=10,
            offset=0,
            search=None,
            search_fields=[],
            filters=["age:gte:30"],
            sort=["age:desc"],
        )

        assert response.total == 2
        assert [item.name for item in response.items] == ["Carlos", "Ana"]

    asyncio.run(_run())


def test_build_query_endpoint_rejects_invalid_filter_format() -> None:
    async def _run() -> None:
        use_case: QueryEntitiesUseCase[_Entity] = QueryEntitiesUseCase(  # type: ignore[arg-type]
            t.cast(IBaseRepository[_Entity], _Repo()), BaseDomainService()
        )

        def _factory() -> QueryEntitiesUseCase[_Entity]:
            return use_case

        endpoint = build_query_endpoint(_factory)

        raised = None
        try:
            await endpoint(
                limit=10,
                offset=0,
                search=None,
                search_fields=[],
                filters=["invalid-filter"],
                sort=[],
            )
        except HTTPException as exc:
            raised = exc

        assert raised is not None
        assert raised.status_code == 422

    asyncio.run(_run())


def test_register_query_endpoint_adds_get_route_to_router() -> None:
    async def _run() -> None:
        use_case: QueryEntitiesUseCase[_Entity] = QueryEntitiesUseCase(  # type: ignore[arg-type]
            t.cast(IBaseRepository[_Entity], _Repo()), BaseDomainService()
        )

        def _factory() -> QueryEntitiesUseCase[_Entity]:
            return use_case

        router = APIRouter()
        endpoint = register_query_endpoint(
            router,
            path="/entities/query",
            use_case_factory=_factory,
            name="query_entities",
            tags=["entities"],
        )

        matching_routes = [
            route
            for route in router.routes
            if getattr(route, "path", None) == "/entities/query"
        ]

        assert len(matching_routes) == 1
        route = t.cast(t.Any, matching_routes[0])
        assert "GET" in route.methods
        assert route.endpoint is endpoint

    asyncio.run(_run())


def test_build_query_endpoint_translates_value_error_to_http_422() -> None:
    class _FailingUseCase:
        async def execute(self, _query: QueryRequestDTO):
            raise ValueError("Campo de filtro no soportado: typo")

    async def _run() -> None:
        endpoint = build_query_endpoint(t.cast(t.Any, lambda: _FailingUseCase()))

        raised = None
        try:
            await endpoint(
                limit=10,
                offset=0,
                search=None,
                search_fields=[],
                filters=[],
                sort=[],
            )
        except HTTPException as exc:
            raised = exc

        assert raised is not None
        assert raised.status_code == 422
        assert raised.detail == "Campo de filtro no soportado: typo"

    asyncio.run(_run())


def test_build_query_endpoint_keeps_comma_for_text_operators() -> None:
    class _CaptureUseCase:
        captured: QueryRequestDTO | None = None

        async def execute(self, query: QueryRequestDTO) -> QueryResponseDTO:
            self.captured = query
            return QueryResponseDTO(items=[], total=0, limit=query.limit, offset=query.offset)

    async def _run() -> None:
        use_case = _CaptureUseCase()
        endpoint = build_query_endpoint(t.cast(t.Any, lambda: use_case))

        await endpoint(
            limit=10,
            offset=0,
            search=None,
            search_fields=[],
            filters=["name:contains:ana,beta"],
            sort=[],
        )

        assert use_case.captured is not None
        assert len(use_case.captured.filters) == 1
        assert use_case.captured.filters[0].value == "ana,beta"

    asyncio.run(_run())


def test_build_query_endpoint_splits_comma_for_in_operator() -> None:
    class _CaptureUseCase:
        captured: QueryRequestDTO | None = None

        async def execute(self, query: QueryRequestDTO) -> QueryResponseDTO:
            self.captured = query
            return QueryResponseDTO(items=[], total=0, limit=query.limit, offset=query.offset)

    async def _run() -> None:
        use_case = _CaptureUseCase()
        endpoint = build_query_endpoint(t.cast(t.Any, lambda: use_case))

        await endpoint(
            limit=10,
            offset=0,
            search=None,
            search_fields=[],
            filters=["age:in:20,30"],
            sort=[],
        )

        assert use_case.captured is not None
        assert len(use_case.captured.filters) == 1
        assert use_case.captured.filters[0].value == [20, 30]

    asyncio.run(_run())
