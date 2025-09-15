import typing as t
import types
import importlib
import pkgutil
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload, RelationshipProperty

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


async def db_list(session: AsyncSession, model: t.Type[T]) -> t.List[T]:
    stmt = select(model).options(*load_relations(model))
    result = await session.execute(stmt)
    entities = list(result.scalars().all())
    if not entities:
        return []
    return entities


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


def import_all_models(package: types.ModuleType)  -> t.Any:
    for _, module_name, _ in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package.__name__}.{module_name}")
