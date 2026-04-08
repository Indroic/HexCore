import asyncio
import typing as t

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from hexcore.application.dtos.query import (
    FilterConditionDTO,
    FilterOperator,
    QueryRequestDTO,
    SortConditionDTO,
    SortDirection,
)
from hexcore.infrastructure.repositories.orms.beanie.utils import db_query as beanie_db_query
from hexcore.infrastructure.repositories.orms.sqlalchemy import BaseModel
from hexcore.infrastructure.repositories.orms.sqlalchemy.utils import (
    db_query as sqlalchemy_db_query,
)


class _SqlModel(BaseModel[t.Any]):
    __tablename__ = "test_items"

    rank: Mapped[int] = mapped_column(Integer)
    name: Mapped[str] = mapped_column(String)


class _FakeCountResult:
    def scalar_one(self) -> int:
        return 0


class _FakeSqlSession:
    async def execute(self, _stmt: t.Any) -> _FakeCountResult:
        return _FakeCountResult()


class _DummyField:
    def __init__(self, annotation: t.Any) -> None:
        self.annotation = annotation


class _DummyBeanieQuery:
    async def count(self) -> int:
        return 0

    def sort(self, *_args: t.Any) -> "_DummyBeanieQuery":
        return self

    def skip(self, _offset: int) -> "_DummyBeanieQuery":
        return self

    def limit(self, _limit: int) -> "_DummyBeanieQuery":
        return self

    async def to_list(self) -> list[t.Any]:
        return []


class _DummyBeanieDocument:
    model_fields = {
        "name": _DummyField(str),
        "age": _DummyField(int),
    }

    @classmethod
    def find(cls, _filter_query: dict[str, t.Any]) -> _DummyBeanieQuery:
        return _DummyBeanieQuery()


def test_sqlalchemy_db_query_raises_on_invalid_filter_field() -> None:
    async def _run() -> None:
        query = QueryRequestDTO(
            limit=10,
            offset=0,
            search=None,
            search_fields=[],
            filters=[
                FilterConditionDTO(
                    field="unknown_field",
                    operator=FilterOperator.EQ,
                    value=1,
                )
            ],
            sort=[],
        )

        raised = None
        try:
            await sqlalchemy_db_query(t.cast(t.Any, _FakeSqlSession()), _SqlModel, query)
        except ValueError as exc:
            raised = exc

        assert raised is not None
        assert "Campo de filtro no soportado" in str(raised)

    asyncio.run(_run())


def test_sqlalchemy_db_query_raises_on_invalid_sort_field() -> None:
    async def _run() -> None:
        query = QueryRequestDTO(
            limit=10,
            offset=0,
            search=None,
            search_fields=[],
            filters=[],
            sort=[SortConditionDTO(field="unknown_sort", direction=SortDirection.ASC)],
        )

        raised = None
        try:
            await sqlalchemy_db_query(t.cast(t.Any, _FakeSqlSession()), _SqlModel, query)
        except ValueError as exc:
            raised = exc

        assert raised is not None
        assert "Campo de orden no soportado" in str(raised)

    asyncio.run(_run())


def test_sqlalchemy_db_query_raises_on_invalid_search_field() -> None:
    async def _run() -> None:
        query = QueryRequestDTO(
            limit=10,
            offset=0,
            search="ana",
            search_fields=["unknown_search"],
            filters=[],
            sort=[],
        )

        raised = None
        try:
            await sqlalchemy_db_query(t.cast(t.Any, _FakeSqlSession()), _SqlModel, query)
        except ValueError as exc:
            raised = exc

        assert raised is not None
        assert "Campo de busqueda no soportado" in str(raised)

    asyncio.run(_run())


def test_beanie_db_query_raises_on_invalid_filter_field() -> None:
    async def _run() -> None:
        query = QueryRequestDTO(
            limit=10,
            offset=0,
            search=None,
            search_fields=[],
            filters=[
                FilterConditionDTO(
                    field="unknown_field",
                    operator=FilterOperator.EQ,
                    value=1,
                )
            ],
            sort=[],
        )

        raised = None
        try:
            await beanie_db_query(t.cast(t.Any, _DummyBeanieDocument), query)
        except ValueError as exc:
            raised = exc

        assert raised is not None
        assert "Campo de filtro no soportado" in str(raised)

    asyncio.run(_run())


def test_beanie_db_query_raises_on_invalid_sort_field() -> None:
    async def _run() -> None:
        query = QueryRequestDTO(
            limit=10,
            offset=0,
            search=None,
            search_fields=[],
            filters=[],
            sort=[SortConditionDTO(field="unknown_sort", direction=SortDirection.ASC)],
        )

        raised = None
        try:
            await beanie_db_query(t.cast(t.Any, _DummyBeanieDocument), query)
        except ValueError as exc:
            raised = exc

        assert raised is not None
        assert "Campo de orden no soportado" in str(raised)

    asyncio.run(_run())


def test_beanie_db_query_raises_on_invalid_search_field() -> None:
    async def _run() -> None:
        query = QueryRequestDTO(
            limit=10,
            offset=0,
            search="ana",
            search_fields=["unknown_search"],
            filters=[],
            sort=[],
        )

        raised = None
        try:
            await beanie_db_query(t.cast(t.Any, _DummyBeanieDocument), query)
        except ValueError as exc:
            raised = exc

        assert raised is not None
        assert "Campo de busqueda no soportado" in str(raised)

    asyncio.run(_run())
