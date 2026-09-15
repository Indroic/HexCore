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
    get_session_factory,
)
from hexcore.infrastructure.uow import BeanieUnitOfWork, SqlAlchemyUnitOfWork


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_session_factory()
    async with factory() as session:
        yield session


async def get_sql_uow(
    session: AsyncSession = Depends(get_session),
) -> AsyncGenerator[IUnitOfWork, None]:
    """
    Cede un `SqlAlchemyUnitOfWork` **sin entrar** en él.

    Es la convención de los ejemplos de use case: el use case hace su propio
    ``async with self.uow:`` y controla el commit. Si esta dependencia entrara al UoW,
    ese ``async with`` del use case anidaría contextos.

    Si necesitás el UoW ya abierto en el endpoint, usá `get_sql_uow_open`.
    """
    yield SqlAlchemyUnitOfWork(session=session)


async def get_sql_uow_open(
    session: AsyncSession = Depends(get_session),
) -> AsyncGenerator[IUnitOfWork, None]:
    """
    Cede un `SqlAlchemyUnitOfWork` ya **abierto** (dentro de ``async with``).

    Para endpoints que operan sobre el UoW directamente, sin pasar por un use case.
    No comitea: el commit sigue siendo explícito.
    """
    async with SqlAlchemyUnitOfWork(session=session) as uow:
        yield uow


async def get_nosql_uow() -> AsyncGenerator[IUnitOfWork, None]:
    uow: IUnitOfWork = BeanieUnitOfWork()
    async with uow:
        yield uow


def build_query_endpoint(
    use_case_factory: t.Callable[[], QueryEntitiesUseCase[t.Any]],
    *,
    extra_params: t.Mapping[str, t.Any] | None = None,
) -> t.Callable[..., t.Awaitable[QueryResponseDTO]]:
    """
    Construye un endpoint GET de listado/búsqueda a partir de un use case.

    Args:
        use_case_factory: Cómo construir el use case por request. Normalmente un
            `lambda` que resuelve dependencias.
        extra_params: Parámetros adicionales a inyectar en la firma del endpoint, como
            ``{"tenant": Depends(get_tenant)}``. Aparecen en el OpenAPI y se le pasan al
            factory como kwargs si éste los acepta.
    """
    async def endpoint(
        limit: int = Query(50, ge=1),
        offset: int = Query(0, ge=0),
        search: str | None = Query(default=None),
        # `default_factory=list` y no `default=[]`: FastAPI tolera la lista mutable como
        # default, pero es una lista compartida entre todas las llamadas.
        search_fields: list[str] = Query(default_factory=list),
        filters: list[str] = Query(default_factory=list),
        sort: list[str] = Query(default_factory=list),
    ) -> QueryResponseDTO:
        return await _execute(
            use_case_factory,
            limit=limit,
            offset=offset,
            search=search,
            search_fields=search_fields,
            filters=filters,
            sort=sort,
        )

    if extra_params:
        return _with_extra_params(endpoint, use_case_factory, extra_params)
    return endpoint


async def _execute(
    use_case_factory: t.Callable[..., QueryEntitiesUseCase[t.Any]],
    *,
    limit: int,
    offset: int,
    search: str | None,
    search_fields: list[str],
    filters: list[str],
    sort: list[str],
    injected: t.Mapping[str, t.Any] | None = None,
) -> QueryResponseDTO:
    query = QueryRequestDTO(
        limit=limit,
        offset=offset,
        search=search,
        search_fields=search_fields,
        filters=_parse_filter_conditions(filters),
        sort=_parse_sort_conditions(sort),
    )
    use_case = _build_use_case(use_case_factory, injected or {})
    try:
        return await use_case.execute(query)
    except ValueError as exc:
        raise _field_error(exc) from exc


def register_query_endpoint(
    router: APIRouter,
    *,
    path: str,
    use_case_factory: t.Callable[[], QueryEntitiesUseCase[t.Any]],
    name: str | None = None,
    summary: str | None = None,
    tags: list[str] | None = None,
    dependencies: t.Sequence[t.Any] | None = None,
    response_model: t.Any = QueryResponseDTO,
    extra_params: t.Mapping[str, t.Any] | None = None,
    **route_kwargs: t.Any,
) -> t.Callable[..., t.Awaitable[QueryResponseDTO]]:
    """
    Registra el endpoint de query en un router.

    Args:
        dependencies: Dependencias de la ruta, típicamente la auth
            (``[Depends(get_current_user)]``). Sin esto, cualquier endpoint que necesite
            autenticación no podía usar este helper — que es el caso de casi toda app.
        response_model: Por si querés un modelo de respuesta más estrecho que
            `QueryResponseDTO`.
        extra_params: Ver `build_query_endpoint`.
        **route_kwargs: Se pasan tal cual a `add_api_route` (`status_code`,
            `responses`, `deprecated`…).
    """
    endpoint = build_query_endpoint(use_case_factory, extra_params=extra_params)
    router.add_api_route(
        path,
        endpoint,
        methods=["GET"],
        response_model=response_model,
        name=name,
        summary=summary,
        tags=t.cast(t.Any, tags),
        dependencies=list(dependencies) if dependencies else None,
        **route_kwargs,
    )
    return endpoint


def _build_use_case(
    factory: t.Callable[..., QueryEntitiesUseCase[t.Any]],
    injected: t.Mapping[str, t.Any],
) -> QueryEntitiesUseCase[t.Any]:
    """
    Llama al factory pasándole sólo los `extra_params` que acepte.

    Así un factory sin argumentos —el caso habitual— sigue funcionando aunque el endpoint
    declare parámetros extra para el OpenAPI.
    """
    if not injected:
        return factory()

    import inspect

    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):
        return factory()

    accepts_kwargs = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in signature.parameters.values()
    )
    if accepts_kwargs:
        return factory(**injected)

    accepted = {
        name: value
        for name, value in injected.items()
        if name in signature.parameters
    }
    return factory(**accepted)


def _with_extra_params(
    base_endpoint: t.Callable[..., t.Awaitable[QueryResponseDTO]],
    use_case_factory: t.Callable[..., QueryEntitiesUseCase[t.Any]],
    extra_params: t.Mapping[str, t.Any],
) -> t.Callable[..., t.Awaitable[QueryResponseDTO]]:
    """
    Envuelve el endpoint añadiendo parámetros a su firma para que FastAPI los resuelva.

    Se manipula `__signature__` porque es la única forma de declarar parámetros
    dinámicos que FastAPI vea: su introspección lee la firma, y un `**kwargs` sin firma
    declarada hace que FastAPI lo interprete como un parámetro requerido y devuelva 422.
    """
    import inspect

    base = inspect.signature(base_endpoint)
    query_param_names = set(base.parameters)

    async def wrapper(**kwargs: t.Any) -> QueryResponseDTO:
        query_args = {
            name: value for name, value in kwargs.items() if name in query_param_names
        }
        injected = {
            name: value
            for name, value in kwargs.items()
            if name not in query_param_names
        }
        return await _execute(use_case_factory, injected=injected, **query_args)

    extra = [
        inspect.Parameter(
            name,
            inspect.Parameter.KEYWORD_ONLY,
            default=default,
            annotation=t.Any,
        )
        for name, default in extra_params.items()
    ]
    wrapper.__signature__ = base.replace(  # type: ignore[attr-defined]
        parameters=[*base.parameters.values(), *extra]
    )
    wrapper.__name__ = getattr(base_endpoint, "__name__", "query_endpoint")
    return wrapper


def _field_error(exc: ValueError) -> HTTPException:
    """
    Traduce un error de campo inválido a un 422 con cuerpo estructurado.

    Antes salía como `str(ValueError)` crudo, que el cliente sólo podía mostrar. Con
    `field` y `allowed` puede señalar el input concreto.
    """
    detail: dict[str, t.Any] = {"message": str(exc)}
    field = getattr(exc, "field", None)
    allowed = getattr(exc, "allowed", None)
    if field is not None:
        detail["field"] = field
    if allowed is not None:
        detail["allowed"] = sorted(allowed)
    return HTTPException(status_code=422, detail=detail)


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
