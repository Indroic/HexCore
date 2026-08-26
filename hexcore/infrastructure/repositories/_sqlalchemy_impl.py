"""
`SqlAlchemyRepository`, con SQLAlchemy importado sin guardas.

Este módulo **exige `[sql]`**: importarlo sin SQLAlchemy levanta `ImportError`, y así tiene que
ser. Quien decide si el extra está es `implementations.py`, que lo importa perezosamente y
traduce la falta en un error que dice qué instalar.

La razón de que la clase viva acá y no allá es de tipado, no de organización. Antes estaba
adentro de un `try/except ImportError` con una clase vacía en el `except`::

    except ImportError:
        M = t.TypeVar("M")
        class SqlAlchemyRepository(t.Generic[T, M]): ...

Pyright analiza **las dos ramas** y se queda con la última definición del nombre, así que
`SqlAlchemyRepository` resolvía a la clase vacía: sin `save`, sin `get_by_id`, sin `model_cls`,
sin `query_cursor`. El hover y el autocompletado del consumidor no mostraban nada, y los tipos
de todo lo que la tocaba se propagaban como `Unknown` — 64 de los errores de Pyright del
paquete salían de esas tres líneas.

Un stub no lo arregla: el problema no es que falte información de tipos, es que hay dos
definiciones del mismo nombre y la mala gana. Lo que lo arregla es que haya **una sola**, y para
que haya una sola el `except ImportError` tiene que desaparecer — de ahí este archivo.
"""
from __future__ import annotations

import typing as t
from uuid import UUID

from hexcore.application.dtos.query import QueryRequestDTO

from .base import BaseSQLAlchemyRepository, T
from .implementations import HasBasicArgs
from .orms.sqlalchemy import BaseModel
from .orms.sqlalchemy.utils import (
    db_get as sql_db_get,
    db_list as sql_db_list,
    db_query as sql_db_query,
    logical_delete as sql_logical_delete,
    save_entity as sql_save_entity,
)
from .utils import to_entity_from_model_or_document

if t.TYPE_CHECKING:
    from hexcore.application.dtos.cursor import CursorPageDTO, CursorRequestDTO

__all__ = ["M", "SqlAlchemyRepository"]

M = t.TypeVar("M", bound=BaseModel[t.Any])


class SqlAlchemyRepository(
    BaseSQLAlchemyRepository[T], HasBasicArgs[T, M], t.Generic[T, M]
):
    """
    Implementaciones comunes para repositorios SQL usando SQLAlchemy.
    Proporciona métodos CRUD genéricos que pueden ser reutilizados por repositorios específicos.
    """

    @property
    def model_cls(self) -> t.Type[M]:
        raise NotImplementedError("Debe implementar la propiedad model_cls")

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

    async def list_all(
        self, limit: t.Optional[int] = None, offset: int = 0
    ) -> t.List[T]:
        if self.limit_offset_pagination:
            models = await sql_db_list(
                self.session, self.model_cls, limit=limit, offset=offset
            )
        else:
            models = await sql_db_list(self.session, self.model_cls)
        return [
            await to_entity_from_model_or_document(
                model, self.entity_cls, self.fields_resolvers
            )
            for model in models
        ]

    async def query_all(self, query: QueryRequestDTO) -> tuple[t.List[T], int]:
        models, total = await sql_db_query(self.session, self.model_cls, query)
        entities = [
            await to_entity_from_model_or_document(
                model, self.entity_cls, self.fields_resolvers
            )
            for model in models
        ]
        return entities, total

    async def query_cursor(self, query: "CursorRequestDTO") -> "CursorPageDTO[T]":
        """
        Página por cursor (F15). Alternativa a `query_all` para listados grandes,
        donde `OFFSET` degrada. No devuelve `total`: contar es lo que evita.
        """
        from hexcore.application.dtos.cursor import CursorPageDTO

        from .orms.sqlalchemy.utils import db_query_cursor

        models, next_cursor = await db_query_cursor(
            self.session, self.model_cls, query
        )
        entities = [
            await to_entity_from_model_or_document(
                model, self.entity_cls, self.fields_resolvers
            )
            for model in models
        ]
        return CursorPageDTO[T](items=entities, next_cursor=next_cursor)

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
