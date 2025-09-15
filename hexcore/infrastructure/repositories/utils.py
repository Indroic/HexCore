from __future__ import annotations

import typing as t


from hexcore.domain.base import BaseEntity
from hexcore.types import FieldResolversType
from hexcore.types import VisitedType, VisitedResultsType


from .orms.sqlalchemy import BaseModel
from .orms.beanie import BaseDocument
from .base import BaseSQLAlchemyRepository

# --- Función auxiliar para aplicar resolvers asíncronos en dicts ---

T = t.TypeVar("T", bound=t.Union[BaseModel[t.Any], BaseDocument, t.Any])
E = t.TypeVar("E", bound=BaseEntity)


async def _apply_async_field_resolvers(
    model_or_doc: T,
    model_dict: t.Dict[str, t.Any],
    field_resolvers: t.Optional[FieldResolversType[T]] = None,
    visited: t.Optional[VisitedType] = None,
    visited_results: t.Optional[VisitedResultsType] = None,
) -> t.Dict[str, t.Any]:
    """
    Aplica resolvers asíncronos sobre un dict, usando el modelo/documento como fuente.
    Cada resolver recibe el modelo/documento y debe devolver el valor para el campo.
    Mecanismo de protección de ciclo:
    - visited: set de ids de entidades ya visitadas en la cadena de resolución actual.
    - visited_results: dict de resultados ya calculados por id de entidad.
    Si se detecta un ciclo (id ya en visited), se retorna el dict tal cual, evitando recursión infinita.
    """
    # Si no hay resolvedores, retorna el dict tal cual
    if not field_resolvers:
        return model_dict
    # Copia el dict para no modificar el original
    model_dict = model_dict.copy()
    # Inicializa el set de visitados si no se pasó
    if visited is None:
        visited = set()
    # Inicializa el diccionario de resultados si no se pasó
    if visited_results is None:
        visited_results = {}
    # Obtiene el id único de la entidad/modelo actual
    entity_id = getattr(model_or_doc, "id", None)
    if entity_id is not None:
        # Si ya fue visitada, retorna el dict (evita recursión infinita)
        if entity_id in visited:
            return model_dict  # Ya visitado, evita ciclo
        # Marca la entidad como visitada
        visited = set(visited)
        visited.add(entity_id)
    # Itera sobre los campos y sus resolvedores
    for field, (data_field, resolver) in field_resolvers.items():
        # Si el campo existe en el dict
        if data_field in model_dict:
            # Llama al resolvedor pasando solo el modelo
            model_dict[field] = await resolver(model_or_doc)
    # Devuelve el dict con los campos resueltos
    return model_dict


async def to_entity_from_model_or_document(
    model_instance: T,
    entity_class: t.Type[E],
    field_resolvers: t.Optional[FieldResolversType[T]] = None,
    is_nosql: bool = False,
) -> E:
    """
    Convierte un modelo SQLAlchemy o un documento Beanie a una entidad de dominio,
    permitiendo reconstruir campos complejos con resolvers asíncronos.
    Si is_nosql=True, renombra 'entity_id' a 'id'.
    
    SOLO FUNCIONA CON LOS ORMS/ODMS SOPORTADOS (SQLAlchemy Y BEANIE).
    """
    model_dict = (
        model_instance.model_dump()
        if is_nosql and isinstance(model_instance, BaseDocument)
        else model_instance.__dict__.copy()
    )
    if is_nosql and "entity_id" in model_dict:
        model_dict["id"] = model_dict.pop("entity_id")
    model_dict = await _apply_async_field_resolvers(
        model_instance, model_dict, field_resolvers
    )
    if is_nosql:
        return entity_class.model_validate(model_dict)
    return entity_class.model_validate(model_dict, from_attributes=True)


def get_all_concrete_subclasses(cls: type) -> set[type]:
    subclasses: set[type] = set()
    for subclass in cls.__subclasses__():
        if not getattr(subclass, "__abstractmethods__", set()):  # type: ignore[arg-type]
            subclasses.add(subclass)
        subclasses.update(get_all_concrete_subclasses(subclass))
    return subclasses


def discover_sql_repositories() -> t.Dict[
    str,
    t.Type[BaseSQLAlchemyRepository[t.Any]],
]:
    """
    Descubre todos los repositorios SQL disponibles.

    Retorna un diccionario que mapea nombres de repositorios a sus clases.
    El nombre del repositorio se deriva del nombre de la clase, convirtiéndolo a minúsculas.
    Ejemplo:
        Si existe una clase UserRepository, se mapeará como 'user': UserRepository
    """

    return {
        repo_cls.__name__.lower().replace("repository", ""): repo_cls
        for repo_cls in get_all_concrete_subclasses(BaseSQLAlchemyRepository)
    }
