import typing as t
from pymongo import AsyncMongoClient
from beanie import init_beanie  # type: ignore
from uuid import UUID

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
    entity_data_dict = entity_data.model_dump()

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


async def init_beanie_documents():
    """
    Inicializa los documentos Beanie descubiertos.
    """
    client = AsyncMongoClient(LazyConfig().get_config().mongo_uri)  # type: ignore

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


async def db_list(document_class: t.Type[D]) -> t.List[D]:
    """
    Lista todos los documentos de una clase específica.
    Args:
        document_class: Clase del documento a listar.
    Returns:
        Lista de documentos encontrados.
    """
    return await document_class.find_all().to_list()


async def save_entity(
    entity: E, document_cls: t.Type[D], fields_serializers: FieldSerializersType[E]
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
        document.is_active = False
        await document.save()
