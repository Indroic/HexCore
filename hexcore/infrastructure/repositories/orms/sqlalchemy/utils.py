import typing as t
import types
import importlib
import pkgutil
from uuid import UUID

from sqlalchemy import and_, cast, func, or_, select, String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, RelationshipProperty

from hexcore.application.dtos.query import (
    FilterOperator,
    QueryRequestDTO,
    SortDirection,
)
from hexcore.types import FieldResolversType, RelationsType

from . import BaseModel

from hexcore.domain.base import BaseEntity

T = t.TypeVar("T", bound=BaseModel[t.Any])
E = t.TypeVar("E", bound=BaseEntity)


def to_model(
    entity: E,
    model_cls: type[T],
    exclude: t.Optional[set[str]] = None,
    field_serializers: t.Optional[FieldResolversType[E]] = None,
    set_domain: bool = False,
) -> T:
    """
    Convierte una entidad de dominio a un modelo SQLAlchemy, permitiendo serializar campos complejos.
    - Si se especifican field_serializers, serializa campos complejos.
    - Si set_domain es True, llama a set_domain_entity en el modelo.
    """
    entity_data = entity.model_dump(exclude=exclude or set())
    if field_serializers:
        for field, (dest_field, serializer) in field_serializers.items():
            if hasattr(entity, field):
                entity_data[dest_field] = serializer(entity)
                if field in entity_data:
                    del entity_data[field]
    model = model_cls(**entity_data)
    if set_domain and hasattr(model, "set_domain_entity"):
        model.set_domain_entity(entity)
    return model


def _get_relationship_names(model: t.Type[BaseModel[t.Any]]) -> list[str]:
    return [
        key
        for key, attr in model.__mapper__.all_orm_descriptors.items()  # type: ignore
        if isinstance(getattr(attr, "property", None), RelationshipProperty)  # type: ignore
    ]


def load_relations(model: t.Type[T]) -> t.Any:
    """
    Crea una lista de opciones selectinload para las relaciones del modelo especificado.

    Args:
        model: Clase del modelo a cargar.

    Returns:
        Lista de opciones selectinload.
    """
    return [selectinload(getattr(model, rel)) for rel in _get_relationship_names(model)]


async def db_get(
    session: AsyncSession, model: t.Type[T], id: UUID, exc_none: Exception
) -> T:
    stmt = select(model).where(model.id == id)
    result = await session.execute(stmt)
    get_entity = result.scalar_one_or_none()
    if not get_entity:
        raise exc_none

    return get_entity


async def db_list(
    session: AsyncSession,
    model: t.Type[T],
    limit: t.Optional[int] = None,
    offset: int = 0,
) -> t.List[T]:
    stmt = select(model).options(*load_relations(model))
    if offset > 0:
        stmt = stmt.offset(offset)
    if limit is not None:
        stmt = stmt.limit(limit)
    result = await session.execute(stmt)
    entities = list(result.scalars().all())
    if not entities:
        return []
    return entities


def _resolve_model_column(model: t.Type[T], field_name: str) -> t.Any:
    if field_name in model.__table__.columns:
        return getattr(model, field_name)
    return None


def _require_model_column(
    model: t.Type[T],
    field_name: str,
    context: str,
) -> t.Any:
    column = _resolve_model_column(model, field_name)
    if column is None:
        raise ValueError(f"Campo de {context} no soportado: {field_name}")
    return column


def _build_filter_expression(model: t.Type[T], query: QueryRequestDTO) -> list[t.Any]:
    expressions: list[t.Any] = []

    if query.search:
        search_text = query.search.strip()
        if search_text:
            if query.search_fields:
                search_columns = [
                    _require_model_column(model, field, "busqueda")
                    for field in query.search_fields
                ]
            else:
                search_columns = [
                    getattr(model, column.name)
                    for column in model.__table__.columns
                    if isinstance(column.type, String)
                ]
            if search_columns:
                expressions.append(
                    or_(
                        *[
                            cast(column, String).ilike(f"%{search_text}%")
                            for column in search_columns
                        ]
                    )
                )

    for condition in query.filters:
        column = _require_model_column(model, condition.field, "filtro")

        operator = condition.operator
        value = condition.value

        if operator == FilterOperator.IS_NULL:
            expressions.append(column.is_(None))
        elif operator == FilterOperator.EQ:
            expressions.append(column == value)
        elif operator == FilterOperator.NE:
            expressions.append(column != value)
        elif operator == FilterOperator.GT:
            expressions.append(column > value)
        elif operator == FilterOperator.GTE:
            expressions.append(column >= value)
        elif operator == FilterOperator.LT:
            expressions.append(column < value)
        elif operator == FilterOperator.LTE:
            expressions.append(column <= value)
        elif operator == FilterOperator.IN and isinstance(value, list):
            expressions.append(column.in_(value))
        elif operator == FilterOperator.NOT_IN and isinstance(value, list):
            expressions.append(~column.in_(value))
        elif operator == FilterOperator.CONTAINS:
            expressions.append(cast(column, String).ilike(f"%{value}%"))
        elif operator == FilterOperator.STARTSWITH:
            expressions.append(cast(column, String).ilike(f"{value}%"))
        elif operator == FilterOperator.ENDSWITH:
            expressions.append(cast(column, String).ilike(f"%{value}"))

    return expressions


def _apply_sorting(stmt: t.Any, model: t.Type[T], query: QueryRequestDTO) -> t.Any:
    for condition in query.sort:
        column = _require_model_column(model, condition.field, "orden")
        stmt = stmt.order_by(
            column.desc() if condition.direction == SortDirection.DESC else column.asc()
        )
    return stmt


async def db_query(
    session: AsyncSession,
    model: t.Type[T],
    query: QueryRequestDTO,
) -> tuple[list[T], int]:
    where_expressions = _build_filter_expression(model, query)

    count_stmt = select(func.count()).select_from(model)
    if where_expressions:
        count_stmt = count_stmt.where(and_(*where_expressions))
    count_result = await session.execute(count_stmt)
    total = int(count_result.scalar_one() or 0)

    data_stmt = select(model).options(*load_relations(model))
    if where_expressions:
        data_stmt = data_stmt.where(and_(*where_expressions))
    data_stmt = _apply_sorting(data_stmt, model, query)
    data_stmt = data_stmt.offset(query.offset).limit(query.limit)

    result = await session.execute(data_stmt)
    return list(result.scalars().all()), total


async def db_save(session: AsyncSession, entity: T) -> T:
    """
    Guarda una entidad en la base de datos usando merge (actualiza o inserta),
    realiza commit y refresh, y retorna la instancia gestionada.
    """
    merged = await session.merge(entity)
    await session.flush()
    await session.refresh(merged)
    return merged


def select_in_load_options(*relationships: str, model: t.Type[T]) -> t.Any:
    """
    Crea una lista de opciones selectinload para las relaciones especificadas.

    Args:
        *relationships: Nombres de las relaciones a cargar.

    Returns:
        Lista de opciones selectinload.
    """
    return [selectinload(getattr(model, rel)) for rel in relationships]


async def assign_relations(
    session: AsyncSession, model_instance: BaseModel[t.Any], relations: RelationsType
) -> None:
    for attr, (Model, ids) in relations.items():
        if ids:
            stmt = select(Model).where(Model.id.in_(ids))
            result = await session.execute(stmt)
            models = [m for m in result.scalars().all()]
            if len(models) != len(ids):
                raise ValueError(f"Uno o más {attr} especificados no existen.")
            # Asegura que todos los modelos estén en la sesión
            for m in models:
                # Verifica si el objeto está en la sesión usando get (async)
                obj_in_session = await session.get(Model, m.id)
                if not session.is_modified(m) and obj_in_session is None:
                    session.add(m)
            # Detecta si la relación es lista o única
            rel_prop = getattr(type(model_instance), attr)
            if hasattr(rel_prop, "property") and hasattr(rel_prop.property, "uselist"):
                if rel_prop.property.uselist:
                    setattr(model_instance, attr, models)
                else:
                    setattr(model_instance, attr, models[0] if models else None)
            else:
                setattr(model_instance, attr, models)


async def save_entity(
    session: AsyncSession,
    entity: E,
    model_cls: type[T],
    relations: t.Optional[RelationsType] = None,
    exclude: t.Optional[set[str]] = None,
    fields_serializers: t.Optional[FieldResolversType[E]] = None,
) -> T:
    model_instance = to_model(
        entity, model_cls, exclude, fields_serializers, set_domain=True
    )
    if relations:
        await assign_relations(session, model_instance, relations)
    saved = await db_save(session, model_instance)
    return saved


async def logical_delete(
    session: AsyncSession, entity: BaseEntity, model_cls: type[T]
) -> None:
    model = await session.get(model_cls, entity.id)
    if model:
        await entity.deactivate()
        await save_entity(session, entity, model_cls)


def import_all_models(package: types.ModuleType) -> t.Any:
    for _, module_name, _ in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package.__name__}.{module_name}")
