from collections.abc import AsyncGenerator
import typing as t

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from hexcore.application.dtos.query import (
    FilterConditionDTO,
    FilterOperator,
    QueryRequestDTO,
    QueryResponseDTO,
    SortConditionDTO,
    SortDirection,
)
from hexcore.application.use_cases.query import QueryEntitiesUseCase
from hexcore.domain.uow import IUnitOfWork
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
    AsyncSessionLocal,
)
from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork, NoSqlUnitOfWork


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSessionLocal() as session:
        yield session


async def get_sql_uow(
    session: AsyncSession = Depends(get_session),
) -> AsyncGenerator[IUnitOfWork, None]:
    async with SqlAlchemyUnitOfWork(session=session) as uow:
        yield uow


async def get_nosql_uow() -> AsyncGenerator[IUnitOfWork, None]:
    uow: IUnitOfWork = NoSqlUnitOfWork()
    async with uow:
        yield uow


def build_query_endpoint(
    use_case_factory: t.Callable[[], QueryEntitiesUseCase[t.Any]],
) -> t.Callable[..., t.Awaitable[QueryResponseDTO]]:
    async def endpoint(
        limit: int = Query(50, ge=1),
        offset: int = Query(0, ge=0),
        search: str | None = Query(default=None),
        search_fields: list[str] = Query(default=[]),
        filters: list[str] = Query(default=[]),
        sort: list[str] = Query(default=[]),
    ) -> QueryResponseDTO:
        filter_conditions = _parse_filter_conditions(filters)
        sort_conditions = _parse_sort_conditions(sort)
        query = QueryRequestDTO(
            limit=limit,
            offset=offset,
            search=search,
            search_fields=search_fields,
            filters=filter_conditions,
            sort=sort_conditions,
        )
        use_case = use_case_factory()
        try:
            return await use_case.execute(query)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return endpoint


def register_query_endpoint(
    router: APIRouter,
    *,
    path: str,
    use_case_factory: t.Callable[[], QueryEntitiesUseCase[t.Any]],
    name: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
) -> t.Callable[..., t.Awaitable[QueryResponseDTO]]:
    endpoint = build_query_endpoint(use_case_factory)
    router.add_api_route(
        path,
        endpoint,
        methods=["GET"],
        response_model=QueryResponseDTO,
        name=name,
        summary=summary,
        tags=t.cast(t.Any, tags),
    )
    return endpoint


def _parse_filter_conditions(filters: list[str]) -> list[FilterConditionDTO]:
    conditions: list[FilterConditionDTO] = []
    for filter_item in filters:
        parts = filter_item.split(":", 2)
        if len(parts) != 3:
            raise HTTPException(
                status_code=422,
                detail="Formato de filtro invalido. Usa 'campo:operador:valor'.",
            )

        field, operator, raw_value = parts
        try:
            parsed_operator = FilterOperator(operator)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Operador de filtro no soportado: {operator}",
            ) from exc

        conditions.append(
            FilterConditionDTO(
                field=field,
                operator=parsed_operator,
                value=_parse_filter_value(raw_value, parsed_operator),
            )
        )

    return conditions


def _parse_sort_conditions(sort: list[str]) -> list[SortConditionDTO]:
    sort_conditions: list[SortConditionDTO] = []
    for sort_item in sort:
        parts = sort_item.split(":", 1)
        if len(parts) != 2:
            raise HTTPException(
                status_code=422,
                detail="Formato de sort invalido. Usa 'campo:asc|desc'.",
            )

        field, direction = parts
        try:
            parsed_direction = SortDirection(direction)
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Direccion de orden no soportada: {direction}",
            ) from exc

        sort_conditions.append(
            SortConditionDTO(field=field, direction=parsed_direction)
        )

    return sort_conditions


def _parse_filter_value(raw_value: str, operator: FilterOperator) -> t.Any:
    if operator in {FilterOperator.IN, FilterOperator.NOT_IN}:
        return [_parse_scalar(piece.strip()) for piece in raw_value.split(",")]
    return _parse_scalar(raw_value)


def _parse_scalar(raw_value: str) -> t.Any:
    lowered = raw_value.lower()
    if lowered == "null":
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False

    try:
        return int(raw_value)
    except ValueError:
        pass

    try:
        return float(raw_value)
    except ValueError:
        return raw_value
