"""
`BeanieRepository`, con Beanie importado sin guardas.

Este módulo **exige `[mongo]`**. El porqué de que esté acá y no adentro de un
`try/except ImportError` está explicado en `_sqlalchemy_impl.py`: es el mismo defecto, con la
misma consecuencia — una clase vacía en el `except` que Pyright elegía por sobre la real.
"""
from __future__ import annotations

import typing as t
from uuid import UUID

from hexcore.application.dtos.query import QueryRequestDTO
from hexcore.infrastructure.uow.decorators import register_entity_on_uow

from .base import BaseBeanieRepository, T
from .implementations import HasBasicArgs
from .orms.beanie import BaseDocument
from .orms.beanie.utils import (
    db_get as nosql_db_get,
    db_list as nosql_db_list,
    db_query as nosql_db_query,
    logical_delete as nosql_logical_delete,
    save_entity as nosql_save_entity,
)
from .utils import to_entity_from_model_or_document

__all__ = ["BeanieRepository", "D"]

D = t.TypeVar("D", bound=BaseDocument)


class BeanieRepository(BaseBeanieRepository[T], HasBasicArgs[T, D], t.Generic[T, D]):
    """
    Implementaciones comunes para repositorios usando Beanie.
    Proporciona métodos CRUD genéricos que pueden ser reutilizados por repositorios específicos.
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

    async def list_all(
        self, limit: t.Optional[int] = None, offset: int = 0
    ) -> t.List[T]:
        if self.limit_offset_pagination:
            documents = await nosql_db_list(
                self.document_cls, limit=limit, offset=offset
            )
        else:
            documents = await nosql_db_list(self.document_cls)
        return [
            await to_entity_from_model_or_document(
                doc, self.entity_cls, self.fields_resolvers, is_nosql=True
            )
            for doc in documents
        ]

    async def query_all(self, query: QueryRequestDTO) -> tuple[t.List[T], int]:
        documents, total = await nosql_db_query(self.document_cls, query)
        entities = [
            await to_entity_from_model_or_document(
                doc, self.entity_cls, self.fields_resolvers, is_nosql=True
            )
            for doc in documents
        ]
        return entities, total

    @register_entity_on_uow
    async def save(self, entity: T) -> T:
        saved = await nosql_save_entity(
            entity, self.document_cls, self.fields_serializers
        )
        return await to_entity_from_model_or_document(
            saved, self.entity_cls, self.fields_resolvers, is_nosql=True
        )

    async def delete(self, entity: T) -> None:
        return await nosql_logical_delete(entity.id, self.document_cls)
