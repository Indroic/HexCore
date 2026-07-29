"""
F10 (mejoras a `build_query_endpoint`) y F15 (paginación por cursor).
"""
from __future__ import annotations

import asyncio
import typing as t
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import APIRouter, Depends, FastAPI, HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from hexcore.application.dtos.cursor import (  # noqa: E402
    CursorPageDTO,
    CursorRequestDTO,
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
)
from hexcore.application.dtos.errors import UnsupportedQueryFieldError  # noqa: E402
from hexcore.application.dtos.query import (  # noqa: E402
    QueryRequestDTO,
    QueryResponseDTO,
    SortDirection,
)
from hexcore.infrastructure.api.utils import (  # noqa: E402
    build_query_endpoint,
    register_query_endpoint,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ── F15: el cursor opaco ───────────────────────────────────────────────────────


def test_cursor_round_trips_a_datetime_and_a_uuid():
    moment = datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc)
    entity_id = uuid4()

    sort_value, decoded_id = decode_cursor(encode_cursor(moment, entity_id))

    assert sort_value == moment.isoformat()
    assert decoded_id == str(entity_id)


def test_cursor_round_trips_scalars():
    assert decode_cursor(encode_cursor(42, 7)) == (42, 7)
    assert decode_cursor(encode_cursor("abc", "def")) == ("abc", "def")
    assert decode_cursor(encode_cursor(None, 1)) == (None, 1)


def test_cursor_is_opaque_and_url_safe():
    cursor = encode_cursor(datetime(2026, 1, 1, tzinfo=timezone.utc), uuid4())

    assert "2026" not in cursor
    assert "=" not in cursor
    assert "/" not in cursor and "+" not in cursor


@pytest.mark.parametrize(
    "bad", ["", "!!!!", "bm90LWpzb24", "eyJ4IjogMX0"]
)
def test_invalid_cursor_raises_a_value_error(bad):
    with pytest.raises(InvalidCursorError):
        decode_cursor(bad)


def test_invalid_cursor_error_is_a_value_error():
    """Así los handlers de F5 lo traducen a 422 sin configuración extra."""
    assert issubclass(InvalidCursorError, ValueError)


def test_cursor_page_dto_defaults():
    page: CursorPageDTO[int] = CursorPageDTO()

    assert page.items == []
    assert page.next_cursor is None


def test_cursor_page_dto_is_generic_and_serializable():
    page = CursorPageDTO[int](items=[1, 2, 3], next_cursor="abc")

    assert page.model_dump() == {"items": [1, 2, 3], "next_cursor": "abc"}


def test_query_response_dto_is_untouched():
    """F15 se añade, no sustituye."""
    response = QueryResponseDTO(items=[1], total=1, limit=50, offset=0)

    assert response.total == 1
    assert not hasattr(response, "next_cursor")


# ── F15: recorrido real sobre SQLite ───────────────────────────────────────────


sqlalchemy = pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

from sqlalchemy import DateTime, Integer, String  # noqa: E402
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from hexcore.infrastructure.repositories.orms.sqlalchemy import Base  # noqa: E402
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    dispose_engine,
    get_session_factory,
    init_engine,
)


# A nivel de módulo: SQLAlchemy resuelve las anotaciones `Mapped[...]` contra los
# globals del módulo, así que un modelo definido dentro de una función falla con
# `from __future__ import annotations`.
class Ticket(Base):
    __tablename__ = "f15_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


@pytest.fixture
def sqlite_cursor_setup():
    asyncio.run(dispose_engine())

    async def _prepare():
        engine = init_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all, tables=[Ticket.__table__])

        base = datetime(2026, 7, 1, tzinfo=timezone.utc)
        factory = get_session_factory()
        async with factory() as session:
            for i in range(10):
                session.add(
                    Ticket(id=i + 1, title=f"t{i + 1}", created_at=base + timedelta(days=i))
                )
            await session.commit()

    asyncio.run(_prepare())
    yield Ticket
    asyncio.run(dispose_engine())


@pytest.mark.anyio
async def test_cursor_pagination_walks_every_row_exactly_once(sqlite_cursor_setup):
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
        get_session_factory,
    )
    from hexcore.infrastructure.repositories.orms.sqlalchemy.utils import db_query_cursor

    Ticket = sqlite_cursor_setup
    seen: list[int] = []
    cursor: str | None = None
    factory = get_session_factory()

    for _ in range(10):  # tope de seguridad
        async with factory() as session:
            rows, cursor = await db_query_cursor(
                session,
                Ticket,
                CursorRequestDTO(limit=3, cursor=cursor, direction=SortDirection.ASC),
            )
        seen.extend(row.id for row in rows)
        if cursor is None:
            break

    assert seen == list(range(1, 11)), "se saltaron o repitieron filas"
    assert cursor is None


@pytest.mark.anyio
async def test_cursor_pagination_descending(sqlite_cursor_setup):
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
        get_session_factory,
    )
    from hexcore.infrastructure.repositories.orms.sqlalchemy.utils import db_query_cursor

    Ticket = sqlite_cursor_setup
    seen: list[int] = []
    cursor: str | None = None
    factory = get_session_factory()

    for _ in range(10):
        async with factory() as session:
            rows, cursor = await db_query_cursor(
                session,
                Ticket,
                CursorRequestDTO(limit=4, cursor=cursor, direction=SortDirection.DESC),
            )
        seen.extend(row.id for row in rows)
        if cursor is None:
            break

    assert seen == list(range(10, 0, -1))


@pytest.mark.anyio
async def test_last_page_returns_no_cursor(sqlite_cursor_setup):
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
        get_session_factory,
    )
    from hexcore.infrastructure.repositories.orms.sqlalchemy.utils import db_query_cursor

    Ticket = sqlite_cursor_setup
    factory = get_session_factory()

    async with factory() as session:
        rows, cursor = await db_query_cursor(
            session, Ticket, CursorRequestDTO(limit=50, direction=SortDirection.ASC)
        )

    assert len(rows) == 10
    assert cursor is None, "hay next_cursor sin más páginas"


@pytest.mark.anyio
async def test_ties_on_the_sort_field_are_broken_by_id(sqlite_cursor_setup):
    """Sin el desempate por id, las filas con el mismo created_at se pierden."""
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
        get_session_factory,
    )
    from hexcore.infrastructure.repositories.orms.sqlalchemy.utils import db_query_cursor

    Ticket = sqlite_cursor_setup
    factory = get_session_factory()
    same_moment = datetime(2026, 12, 1, tzinfo=timezone.utc)

    async with factory() as session:
        for i in range(5):
            session.add(Ticket(id=100 + i, title=f"tie{i}", created_at=same_moment))
        await session.commit()

    seen: list[int] = []
    cursor: str | None = None
    for _ in range(20):
        async with factory() as session:
            rows, cursor = await db_query_cursor(
                session,
                Ticket,
                CursorRequestDTO(limit=2, cursor=cursor, direction=SortDirection.ASC),
            )
        seen.extend(row.id for row in rows)
        if cursor is None:
            break

    assert sorted(seen) == sorted(list(range(1, 11)) + list(range(100, 105)))
    assert len(seen) == len(set(seen)), "se repitieron filas entre páginas"


@pytest.mark.anyio
async def test_cursor_respects_filters(sqlite_cursor_setup):
    from hexcore.application.dtos.query import FilterConditionDTO, FilterOperator
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
        get_session_factory,
    )
    from hexcore.infrastructure.repositories.orms.sqlalchemy.utils import db_query_cursor

    Ticket = sqlite_cursor_setup
    factory = get_session_factory()

    async with factory() as session:
        rows, _cursor = await db_query_cursor(
            session,
            Ticket,
            CursorRequestDTO(
                limit=50,
                direction=SortDirection.ASC,
                filters=[
                    FilterConditionDTO(
                        field="id", operator=FilterOperator.LTE, value=3
                    )
                ],
            ),
        )

    assert [row.id for row in rows] == [1, 2, 3]


@pytest.mark.anyio
async def test_cursor_rejects_an_unknown_sort_field(sqlite_cursor_setup):
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
        get_session_factory,
    )
    from hexcore.infrastructure.repositories.orms.sqlalchemy.utils import db_query_cursor

    Ticket = sqlite_cursor_setup
    factory = get_session_factory()

    async with factory() as session:
        with pytest.raises(UnsupportedQueryFieldError):
            await db_query_cursor(
                session, Ticket, CursorRequestDTO(sort_field="no_existe")
            )


# ── F10: errores de campo estructurados ────────────────────────────────────────


def test_unsupported_field_error_carries_field_and_allowed():
    error = UnsupportedQueryFieldError("nope", "orden", allowed=["id", "title"])

    assert isinstance(error, ValueError)
    assert error.field == "nope"
    assert error.allowed == ["id", "title"]
    assert str(error) == "Campo de orden no soportado: nope"


@pytest.mark.anyio
async def test_endpoint_returns_a_structured_422_with_field_and_allowed():
    class FailingUseCase:
        async def execute(self, _query: QueryRequestDTO) -> QueryResponseDTO:
            raise UnsupportedQueryFieldError("nope", "orden", allowed=["id", "title"])

    endpoint = build_query_endpoint(t.cast(t.Any, lambda: FailingUseCase()))

    with pytest.raises(HTTPException) as exc:
        await endpoint(
            limit=10, offset=0, search=None, search_fields=[], filters=[], sort=[]
        )

    assert exc.value.status_code == 422
    assert exc.value.detail == {
        "message": "Campo de orden no soportado: nope",
        "field": "nope",
        "allowed": ["id", "title"],
    }


# ── F10: dependencias, response_model y parámetros extra ───────────────────────


class StubUseCase:
    def __init__(self, **kwargs: t.Any) -> None:
        self.kwargs = kwargs

    async def execute(self, query: QueryRequestDTO) -> QueryResponseDTO:
        return QueryResponseDTO(
            items=[], total=0, limit=query.limit, offset=query.offset
        )


def test_register_query_endpoint_accepts_route_dependencies():
    """Sin esto, un endpoint que necesita auth no podía usar el helper."""
    calls: list[str] = []

    async def guard() -> None:
        calls.append("checked")

    router = APIRouter()
    register_query_endpoint(
        router,
        path="/entities",
        use_case_factory=t.cast(t.Any, lambda: StubUseCase()),
        dependencies=[Depends(guard)],
    )
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        assert client.get("/entities").status_code == 200

    assert calls == ["checked"]


def test_route_dependency_can_reject_the_request():
    async def deny() -> None:
        raise HTTPException(status_code=401, detail="sin token")

    router = APIRouter()
    register_query_endpoint(
        router,
        path="/entities",
        use_case_factory=t.cast(t.Any, lambda: StubUseCase()),
        dependencies=[Depends(deny)],
    )
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        assert client.get("/entities").status_code == 401


def test_register_query_endpoint_accepts_a_custom_response_model():
    class NarrowResponse(QueryResponseDTO):
        pass

    router = APIRouter()
    register_query_endpoint(
        router,
        path="/entities",
        use_case_factory=t.cast(t.Any, lambda: StubUseCase()),
        response_model=NarrowResponse,
    )

    route = next(r for r in router.routes if getattr(r, "path", None) == "/entities")
    assert t.cast(t.Any, route).response_model is NarrowResponse


def test_extra_params_appear_in_the_signature_and_reach_the_factory():
    received: list[t.Any] = []

    async def get_tenant() -> str:
        return "acme"

    def factory(**kwargs: t.Any) -> StubUseCase:
        received.append(kwargs)
        return StubUseCase(**kwargs)

    router = APIRouter()
    register_query_endpoint(
        router,
        path="/entities",
        use_case_factory=t.cast(t.Any, factory),
        extra_params={"tenant": Depends(get_tenant)},
    )
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        assert client.get("/entities").status_code == 200

    assert received == [{"tenant": "acme"}]


def test_a_factory_without_arguments_still_works_with_extra_params():
    """El caso habitual: `lambda: MiUseCase(...)` sin kwargs."""
    router = APIRouter()

    async def get_tenant() -> str:
        return "acme"

    register_query_endpoint(
        router,
        path="/entities",
        use_case_factory=t.cast(t.Any, lambda: StubUseCase()),
        extra_params={"tenant": Depends(get_tenant)},
    )
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        assert client.get("/entities").status_code == 200


def test_extra_params_are_documented_in_openapi():
    router = APIRouter()
    register_query_endpoint(
        router,
        path="/entities",
        use_case_factory=t.cast(t.Any, lambda: StubUseCase()),
        extra_params={"tenant": None},
    )
    app = FastAPI()
    app.include_router(router)

    parameters = app.openapi()["paths"]["/entities"]["get"]["parameters"]
    assert any(p["name"] == "tenant" for p in parameters)


# ── F10: los defaults mutables ─────────────────────────────────────────────────


def test_list_query_params_do_not_share_a_mutable_default():
    import inspect

    signature = inspect.signature(build_query_endpoint(t.cast(t.Any, lambda: StubUseCase())))
    for name in ("search_fields", "filters", "sort"):
        default = signature.parameters[name].default
        assert default.default_factory is list, f"{name} usa una lista mutable"


def test_list_query_params_still_work_over_http():
    router = APIRouter()
    captured: list[QueryRequestDTO] = []

    class Capturing:
        async def execute(self, query: QueryRequestDTO) -> QueryResponseDTO:
            captured.append(query)
            return QueryResponseDTO()

    register_query_endpoint(
        router, path="/entities", use_case_factory=t.cast(t.Any, lambda: Capturing())
    )
    app = FastAPI()
    app.include_router(router)

    with TestClient(app) as client:
        client.get("/entities?sort=id:asc&search_fields=title&search=abc")

    assert captured[0].search == "abc"
    assert captured[0].search_fields == ["title"]
    assert captured[0].sort[0].field == "id"
