from __future__ import annotations
import typing as t
from uuid import UUID

from hexcore.infrastructure.uow.decorators import register_entity_on_uow
from hexcore.types import FieldResolversType, FieldSerializersType

from .base import T, BaseSQLAlchemyRepository, IBaseRepository
from .utils import to_entity_from_model_or_document

# Utilidades para operaciones comunes con SQLAlchemy
from .orms.sqlalchemy.utils import (
    db_get as sql_db_get,
    db_list as sql_db_list,
    save_entity as sql_save_entity,
    logical_delete as sql_logical_delete,
)
from .orms.sqlalchemy import BaseModel


# Utilidades para operaciones comunes con Beanie
from .orms.beanie import BaseDocument
from .orms.beanie.utils import (
    db_get as nosql_db_get,
    db_list as nosql_db_list,
    save_entity as nosql_save_entity,
    logical_delete as nosql_logical_delete,
)

M = t.TypeVar("M", bound=BaseModel[t.Any])
D = t.TypeVar("D", bound=BaseDocument)

A = t.TypeVar("A")


class HasBasicArgs(t.Generic[T, A]):
    @property
    def entity_cls(self) -> t.Type[T]:
        raise NotImplementedError("Debe implementar la propiedad entity_cls")

    @property
    def not_found_exception(self) -> t.Type[Exception]:
        raise NotImplementedError("Debe implementar la propiedad not_found_exception")

    @property
    def fields_serializers(self) -> FieldSerializersType[T]:
        """
        Serializadores para campos complejos en la conversión entre Entidad -> Documento/Modelo.
        """
        return {}

    @property
    def fields_resolvers(self) -> FieldResolversType[A]:
        """
        Resolvedores para campos complejos en la conversión entre Documento/Modelo -> Entidad.
        Debe ser implementado por cada repositorio específico.
        """
        return {}


class SQLAlchemyCommonImplementationsRepo(
    BaseSQLAlchemyRepository[T], HasBasicArgs[T, M], t.Generic[T, M]
):
    """
    Implementaciones comunes para repositorios SQL usando SQLAlchemy.
    Proporciona métodos CRUD genéricos que pueden ser reutilizados por repositorios específicos.
    Requiere que se especifiquen las clases de entidad y modelo, así como la excepción a lanzar cuando no se encuentra una entidad.
    - entity_cls: La clase de la entidad de dominio.
    - model_cls: La clase del modelo SQLAlchemy.
    - not_found_exception: La excepción a lanzar cuando no se encuentra una entidad.
    - uow: La unidad de trabajo para manejar transacciones y sesiones.
    Ejemplo de uso:
        class UserRepository(IUserRepo, SqlCommonImplementationsRepo[UserEntity, UserModel]):
            def __init__(self, uow: SqlAlchemyUnitOfWork):
                IUserRepo.__init__(self, uow)
                SqlCommonImplementationsRepo.__init__(self, UserEntity, UserModel, UserNotFoundException, uow)

            async def get_by_email(self, email: str) -> UserEntity:
                # Implementación específica del repositorio


    """

    @property
    def model_cls(self) -> t.Type[M]:
        raise NotImplementedError("Debe implementar la propiedad document_cls")

    async def get_by_id(self, entity_id: UUID) -> T:

        model = await sql_db_get(
            self.session,
            self.model_cls,
            entity_id,
            self.not_found_exception(entity_id),
        )
        return await to_entity_from_model_or_document(
            model, self.entity_cls, self.fields_resolvers
        )

    async def list_all(self) -> t.List[T]:

        models = await sql_db_list(self.session, self.model_cls)
        return [
            await to_entity_from_model_or_document(
                model, self.entity_cls, self.fields_resolvers
            )
            for model in models
        ]

    async def save(self, entity: T) -> T:

        saved = await sql_save_entity(
            self.session,
            entity,
            self.model_cls,
            fields_serializers=self.fields_serializers,
        )
        return await to_entity_from_model_or_document(
            saved, self.entity_cls, self.fields_resolvers
        )

    async def delete(self, entity: T) -> None:

        await sql_logical_delete(self.session, entity, self.model_cls)


class BeanieODMCommonImplementationsRepo(
    IBaseRepository[T], HasBasicArgs[T, D], t.Generic[T, D]
):
    """
    Implementaciones comunes para repositorios usando Beanie.
    Proporciona métodos CRUD genéricos que pueden ser reutilizados por repositorios específicos.
    Requiere que se especifiquen las clases de entidad y documento, así como la excepción a lanzar cuando no se encuentra una entidad.
    - entity_cls: La clase de la entidad de dominio.
    - document_cls: La clase del documento Beanie.
    - not_found_exception: La excepción a lanzar cuando no se encuentra una entidad.
    - fields_resolvers: Resolvedores para campos complejos en la conversión entre entidad y documento.
    Ejemplo de uso:
        class UserRepository(IUserRepo, NoSQLCommonImplementationsRepo[UserEntity]):
            def __init__(self, uow: NoSqlUnitOfWork):
                IUserRepo.__init__(self, uow)
                NoSQLCommonImplementationsRepo.__init__(self, UserEntity, UserDocument, UserNotFoundException, uow)

            async def get_by_email(self, email: str) -> UserEntity:
                # Implementación específica del repositorio
    """

    @property
    def document_cls(self) -> t.Type[D]:
        raise NotImplementedError("Debe implementar la propiedad document_cls")

    async def get_by_id(self, entity_id: UUID) -> T:
        document = await nosql_db_get(self.document_cls, entity_id)
        if not document:
            raise self.not_found_exception(entity_id)
        return await to_entity_from_model_or_document(
            document, self.entity_cls, self.fields_resolvers, is_nosql=True
        )

    async def list_all(self) -> t.List[T]:
        documents = await nosql_db_list(self.document_cls)
        return [
            await to_entity_from_model_or_document(
                doc, self.entity_cls, self.fields_resolvers, is_nosql=True
            )
            for doc in documents
        ]

    @register_entity_on_uow
    async def save(self, entity: T) -> T:  # type: ignore
        saved = await nosql_save_entity(
            entity, self.document_cls, self.fields_serializers
        )
        return await to_entity_from_model_or_document(
            saved, self.entity_cls, self.fields_resolvers, is_nosql=True
        )

    async def delete(self, entity: T) -> None:
        return await nosql_logical_delete(entity.id, self.document_cls)
