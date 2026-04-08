import asyncio
import typing as t

from hexcore.application.dtos.query import (
    FilterConditionDTO,
    FilterOperator,
    QueryRequestDTO,
)
from hexcore.infrastructure.repositories.orms.beanie.utils import db_query


class _Field:
    def __init__(self, annotation: t.Any) -> None:
        self.annotation = annotation


class _DummyDocument:
    model_fields = {
        "name": _Field(str),
        "description": _Field(str | None),
        "age": _Field(int),
    }
    last_filter: dict[str, t.Any] = {}

    @classmethod
    def find(cls, filter_query: dict[str, t.Any]) -> t.Any:
        cls.last_filter = filter_query
        return _DummyQuery()


class _DummyQuery:
    async def count(self) -> int:
        return 0

    def sort(self, *_args: t.Any) -> "_DummyQuery":
        return self

    def skip(self, _offset: int) -> "_DummyQuery":
        return self

    def limit(self, _limit: int) -> "_DummyQuery":
        return self

    async def to_list(self) -> list[t.Any]:
        return []


def test_build_filter_query_infers_text_fields_when_search_fields_empty() -> None:
    async def _run() -> None:
        query = QueryRequestDTO(
            limit=10,
            offset=0,
            search="ana",
            search_fields=[],
            filters=[],
            sort=[],
        )

        await db_query(t.cast(t.Any, _DummyDocument), query)

        assert "$and" in _DummyDocument.last_filter
        or_clause = _DummyDocument.last_filter["$and"][0]["$or"]
        fields = {next(iter(item.keys())) for item in or_clause}
        assert fields == {"name", "description"}

    asyncio.run(_run())


def test_build_filter_query_uses_explicit_search_fields_when_provided() -> None:
    async def _run() -> None:
        query = QueryRequestDTO(
            limit=10,
            offset=0,
            search="ana",
            search_fields=["name"],
            filters=[],
            sort=[],
        )

        await db_query(t.cast(t.Any, _DummyDocument), query)

        assert "$and" in _DummyDocument.last_filter
        or_clause = _DummyDocument.last_filter["$and"][0]["$or"]
        fields = [next(iter(item.keys())) for item in or_clause]
        assert fields == ["name"]

    asyncio.run(_run())


def test_build_filter_query_rejects_invalid_explicit_search_field() -> None:
    async def _run() -> None:
        query = QueryRequestDTO(
            limit=10,
            offset=0,
            search="ana",
            search_fields=["unknown_field"],
            filters=[],
            sort=[],
        )

        raised = None
        try:
            await db_query(t.cast(t.Any, _DummyDocument), query)
        except ValueError as exc:
            raised = exc

        assert raised is not None
        assert "Campo de busqueda no soportado" in str(raised)

    asyncio.run(_run())


def test_build_filter_query_escapes_regex_metacharacters() -> None:
    async def _run() -> None:
        query = QueryRequestDTO(
            limit=10,
            offset=0,
            search="a.*(b",
            search_fields=["name"],
            filters=[
                FilterConditionDTO(
                    field="name",
                    operator=FilterOperator.CONTAINS,
                    value="x+y",
                )
            ],
            sort=[],
        )

        await db_query(t.cast(t.Any, _DummyDocument), query)

        assert "$and" in _DummyDocument.last_filter
        search_regex = _DummyDocument.last_filter["$and"][0]["$or"][0]["name"]["$regex"]
        contains_regex = _DummyDocument.last_filter["$and"][1]["name"]["$regex"]
        assert search_regex == "a\\.\\*\\(b"
        assert contains_regex == "x\\+y"

    asyncio.run(_run())
