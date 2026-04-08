import typing as t
import types
import re
from uuid import UUID

from beanie import init_beanie  # type: ignore
from pymongo import AsyncMongoClient

from hexcore.application.dtos.query import (
    FilterOperator,
    QueryRequestDTO,
    SortDirection,
)
from hexcore.domain.base import BaseEntity
from hexcore.infrastructure.repositories.utils import get_all_concrete_subclasses
from hexcore.types import FieldSerializersType
from hexcore.config import LazyConfig

from . import BaseDocument

E = t.TypeVar("E", bound=BaseEntity)
D = t.TypeVar("D", bound=BaseDocument)


def to_document(
    entity_data: E,
    document_class: t.Type[D],
    field_serializers: t.Optional[FieldSerializersType[E]] = None,
    update: bool = False,  # Indica si se desea realizar una actualización, en ese caso No se renombrará el 'id', solo se excluirá
) -> D:
    """
    Función de ayuda para convertir una entidad de dominio a un modelo NoSQL, permitiendo serializar campos complejos.
    Args:
        entity_data: Entidad de dominio.
        document_class: Clase del documento NoSQL.
        field_serializers: Diccionario opcional {campo: (destino, serializer(entidad de dominio original))} para transformar campos complejos.
        update: Si es True, no renombra el id, solo lo excluye.
    """
    entity_data_dict: dict[str, t.Any] = entity_data.model_dump()

    # Renombramos el 'id' de la entidad a 'entity_id' para el documento.
    if "id" in entity_data_dict and not update:
        entity_data_dict["entity_id"] = entity_data_dict.pop("id")

    # Excluimos el 'id' de la entidad en caso de actualización
    if "id" in entity_data_dict and update:
        entity_data_dict.pop("id")

    # Serializamos campos complejos y eliminamos el campo original si se especifica
    if field_serializers:
        for field, (dest_field, serializer) in field_serializers.items():
            if hasattr(entity_data, field):
                entity_data_dict[dest_field] = serializer(entity_data)
                if field in entity_data_dict:
                    del entity_data_dict[field]

    return document_class(**entity_data_dict)


def discover_beanie_documents() -> t.List[t.Type[BaseDocument]]:
    """
    Descubre todos los documentos Beanie disponibles.

    Retorna una lista de clases de documentos Beanie.
    """
    return [doc_cls for doc_cls in get_all_concrete_subclasses(BaseDocument)]


async def init_beanie_documents() -> None:
    """
    Inicializa los documentos Beanie descubiertos.
    """
    client = AsyncMongoClient(LazyConfig.get_config().mongo_uri)  # type: ignore

    documents = discover_beanie_documents()

    await init_beanie(database=client.get_default_database(), document_models=documents)


async def db_get(document_class: t.Type[D], entity_id: UUID) -> t.Optional[D]:
    """
    Obtiene un documento por su ID.
    Args:
        document_class: Clase del documento a buscar.
        id: ID del documento.
    Returns:
        El documento encontrado o None si no existe.
        :param entity_id: id de la entidad
    """
    return await document_class.find_one({"entity_id": entity_id})


async def db_list(
    document_class: t.Type[D], limit: t.Optional[int] = None, offset: int = 0
) -> t.List[D]:
    """
    Lista todos los documentos de una clase específica.
    Args:
        document_class: Clase del documento a listar.
    Returns:
        Lista de documentos encontrados.
    """
    query = document_class.find_all()
    if offset > 0:
        query = query.skip(offset)
    if limit is not None:
        query = query.limit(limit)
    return await query.to_list()


def _build_filter_query(
    query: QueryRequestDTO,
    document_class: t.Type[D],
) -> dict[str, t.Any]:
    and_clauses: list[dict[str, t.Any]] = []

    if query.search:
        search_value = query.search.strip()
        if search_value:
            if query.search_fields:
                for field in query.search_fields:
                    _require_document_field(document_class, field, "busqueda")
                search_fields = query.search_fields
            else:
                search_fields = _infer_search_fields_from_document(document_class)
            if search_fields:
                escaped_search = re.escape(search_value)
                and_clauses.append(
                    {
                        "$or": [
                            {
                                field: {
                                    "$regex": escaped_search,
                                    "$options": "i",
                                }
                            }
                            for field in search_fields
                        ]
                    }
                )

    for condition in query.filters:
        field = condition.field
        _require_document_field(document_class, field, "filtro")
        operator = condition.operator
        value = condition.value

        if operator == FilterOperator.IS_NULL:
            and_clauses.append({field: None})
        elif operator == FilterOperator.EQ:
            and_clauses.append({field: value})
        elif operator == FilterOperator.NE:
            and_clauses.append({field: {"$ne": value}})
        elif operator == FilterOperator.GT:
            and_clauses.append({field: {"$gt": value}})
        elif operator == FilterOperator.GTE:
            and_clauses.append({field: {"$gte": value}})
        elif operator == FilterOperator.LT:
            and_clauses.append({field: {"$lt": value}})
        elif operator == FilterOperator.LTE:
            and_clauses.append({field: {"$lte": value}})
        elif operator == FilterOperator.IN and isinstance(value, list):
            and_clauses.append({field: {"$in": value}})
        elif operator == FilterOperator.NOT_IN and isinstance(value, list):
            and_clauses.append({field: {"$nin": value}})
        elif operator == FilterOperator.CONTAINS:
            and_clauses.append(
                {field: {"$regex": re.escape(str(value)), "$options": "i"}}
            )
        elif operator == FilterOperator.STARTSWITH:
            and_clauses.append(
                {field: {"$regex": f"^{re.escape(str(value))}", "$options": "i"}}
            )
        elif operator == FilterOperator.ENDSWITH:
            and_clauses.append(
                {field: {"$regex": f"{re.escape(str(value))}$", "$options": "i"}}
            )

    if not and_clauses:
        return {}
    return {"$and": and_clauses}


def _strip_optional(annotation: t.Any) -> t.Any:
    origin = t.get_origin(annotation)
    if origin in (t.Union, types.UnionType):
        args = [arg for arg in t.get_args(annotation) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def _infer_search_fields_from_document(document_class: t.Type[D]) -> list[str]:
    model_fields = getattr(document_class, "model_fields", {})
    fields: list[str] = []
    for field_name, field_info in model_fields.items():
        annotation = _strip_optional(getattr(field_info, "annotation", None))
        if annotation is str:
            fields.append(field_name)
    return fields


def _require_document_field(
    document_class: t.Type[D],
    field_name: str,
    context: str,
) -> None:
    model_fields = getattr(document_class, "model_fields", {})
    if field_name not in model_fields:
        raise ValueError(f"Campo de {context} no soportado: {field_name}")


def _build_sort_query(
    query: QueryRequestDTO,
    document_class: t.Type[D],
) -> list[tuple[str, int]]:
    sort_items: list[tuple[str, int]] = []
    for condition in query.sort:
        _require_document_field(document_class, condition.field, "orden")
        direction = -1 if condition.direction == SortDirection.DESC else 1
        sort_items.append((condition.field, direction))
    return sort_items


async def db_query(
    document_class: t.Type[D],
    query: QueryRequestDTO,
) -> tuple[list[D], int]:
    filter_query = _build_filter_query(query, document_class)
    beanie_query = document_class.find(filter_query)

    total = await beanie_query.count()

    sort_items = _build_sort_query(query, document_class)
    if sort_items:
        beanie_query = beanie_query.sort(t.cast(t.Any, sort_items))

    items = await beanie_query.skip(query.offset).limit(query.limit).to_list()
    return items, total


async def save_entity(
    entity: E,
    document_cls: t.Type[D],
    fields_serializers: t.Optional[FieldSerializersType[E]] = None,
) -> D:
    """
    Guarda o actualiza un documento en la base de datos.
    Args:
        document: Documento a guardar o actualizar.
    Returns:
        El documento guardado o actualizado.
        :param fields_resolvers: resolvers para campos complejos
        :param document_cls: Clase del Documento
        :param entity: Entidad a guardar o actualizar
    """
    document = await db_get(document_cls, entity.id)

    if document:
        # Actualización
        document = to_document(entity, document_cls, fields_serializers, update=True)
        await document.save()

        return document

    # Creación
    document = to_document(entity, document_cls, fields_serializers, update=False)
    await document.save()

    return document


async def logical_delete(entity_id: UUID, document_cls: t.Type[D]) -> None:
    """
    Realiza una eliminación lógica de un documento estableciendo is_active a False.
    Args:
        document: Documento a eliminar lógicamente.
    """
    document = await db_get(document_cls, entity_id)
    if document:
        document_any = t.cast(t.Any, document)
        document_any.is_active = False
        await document.save()
