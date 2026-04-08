import asyncio

from hexcore.application.dtos.query import (
    FilterConditionDTO,
    FilterOperator,
    QueryRequestDTO,
    SortConditionDTO,
    SortDirection,
)
from hexcore.domain.base import BaseEntity
from hexcore.domain.services import BaseDomainService


class _Entity(BaseEntity):
    name: str
    age: int
    city: str | None = None


def _sample_entities() -> list[_Entity]:
    return [
        _Entity(name="Ana", age=31, city="Lima"),
        _Entity(name="Bruno", age=24, city="Quito"),
        _Entity(name="Carla", age=28, city=None),
        _Entity(name="Daniel", age=35, city="Lima"),
    ]


def test_query_entities_applies_search_filters_sort_and_pagination() -> None:
    service = BaseDomainService()
    query = QueryRequestDTO(
        limit=1,
        offset=0,
        search="a",
        search_fields=["name"],
        filters=[
            FilterConditionDTO(
                field="age",
                operator=FilterOperator.GTE,
                value=28,
            )
        ],
        sort=[
            SortConditionDTO(
                field="age",
                direction=SortDirection.DESC,
            )
        ],
    )

    response = service.query_entities(_sample_entities(), query)

    assert response.total == 3
    assert response.has_next is True
    assert len(response.items) == 1
    assert response.items[0].name == "Daniel"


def test_query_entities_supports_in_and_is_null_filters() -> None:
    service = BaseDomainService()
    query = QueryRequestDTO(
        limit=10,
        offset=0,
        search=None,
        search_fields=[],
        filters=[
            FilterConditionDTO(
                field="city",
                operator=FilterOperator.IN,
                value=["Lima", "Quito"],
            ),
            FilterConditionDTO(
                field="name",
                operator=FilterOperator.CONTAINS,
                value="a",
            ),
        ],
        sort=[],
    )

    response = service.query_entities(_sample_entities(), query)

    assert response.total == 2
    assert sorted(entity.name for entity in response.items) == ["Ana", "Daniel"]

    null_query = QueryRequestDTO(
        limit=10,
        offset=0,
        search=None,
        search_fields=[],
        filters=[
            FilterConditionDTO(
                field="city",
                operator=FilterOperator.IS_NULL,
                value=None,
            )
        ],
        sort=[],
    )
    null_response = service.query_entities(_sample_entities(), null_query)
    assert null_response.total == 1
    assert null_response.items[0].name == "Carla"


def test_list_entities_works_with_repository_contract() -> None:
    class _Repo:
        async def list_all(self, limit: int | None = None, offset: int = 0):
            del limit, offset
            return _sample_entities()

    async def _run() -> None:
        service = BaseDomainService()
        query = QueryRequestDTO(
            limit=2,
            offset=1,
            search=None,
            search_fields=[],
            filters=[],
            sort=[],
        )
        response = await service.list_entities(_Repo(), query)  # type: ignore[arg-type]

        assert response.total == 4
        assert len(response.items) == 2
        assert response.items[0].name == "Bruno"

    asyncio.run(_run())


def test_query_entities_sort_numeric_values_without_lexicographic_bias() -> None:
    class _RankedEntity(BaseEntity):
        rank: int

    service = BaseDomainService()
    entities = [
        _RankedEntity(rank=2),
        _RankedEntity(rank=10),
        _RankedEntity(rank=1),
    ]

    response = service.query_entities(
        entities,
        QueryRequestDTO(
            limit=10,
            offset=0,
            search=None,
            search_fields=[],
            filters=[],
            sort=[SortConditionDTO(field="rank", direction=SortDirection.ASC)],
        ),
    )

    assert [item.rank for item in response.items] == [1, 2, 10]
