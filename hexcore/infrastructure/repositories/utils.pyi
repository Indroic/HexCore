import typing as t
from .base import BaseSQLRepository as BaseSQLRepository, BaseSQLTenantAwareRepository as BaseSQLTenantAwareRepository
from .orms.nosql.beanie import BaseDocument as BaseDocument
from .orms.sql.alchemy import BaseModel as BaseModel
from hexcore.domain.base import BaseEntity as BaseEntity
from hexcore.types import FieldResolversType as FieldResolversType, VisitedResultsType as VisitedResultsType, VisitedType as VisitedType

T = t.TypeVar('T', bound=BaseModel[t.Any] | BaseDocument)
E = t.TypeVar('E', bound=BaseEntity)

async def to_entity_from_model_or_document(model_instance: T, entity_class: type[E], field_resolvers: FieldResolversType[T] | None = ..., is_nosql: bool = ...) -> E: ...
def get_all_concrete_subclasses(cls) -> set[type]: ...
def discover_sql_repositories() -> dict[str, type[BaseSQLRepository[t.Any]] | type[BaseSQLTenantAwareRepository[t.Any]]]: ...
