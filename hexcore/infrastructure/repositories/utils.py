from __future__ import annotations

import importlib
import inspect
import pkgutil
import sys
import typing as t
import warnings
from collections.abc import Mapping

from sqlalchemy import Row


from hexcore.domain.base import BaseEntity
from hexcore.types import FieldResolversType
from hexcore.types import VisitedType, VisitedResultsType


from .orms.sqlalchemy import BaseModel
from .orms.beanie import BaseDocument
from .base import BaseSQLAlchemyRepository, BaseBeanieRepository

# --- Función auxiliar para aplicar resolvers asíncronos en dicts ---

T = t.TypeVar("T", bound=t.Union[BaseModel[t.Any], BaseDocument, Row[t.Any], t.Any])
E = t.TypeVar("E", bound=BaseEntity)

_AUTOLOADED_REPOSITORY_PACKAGES: set[str] = set()
_EMPTY_ABSTRACT_MEMBERS: frozenset[str] = frozenset()


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
    Convierte un modelo SQLAlchemy o un documento Beanie a una entidad de dominio.
    """
    model_dict: dict[str, t.Any]

    if is_nosql and isinstance(model_instance, BaseDocument):
        model_dict = model_instance.model_dump()
    # Prioriza _mapping para soportar Row de SQLAlchemy y row-like compatibles.
    elif hasattr(model_instance, "_mapping"):
        mapping_obj = t.cast(Mapping[str, t.Any], getattr(model_instance, "_mapping"))
        model_dict = dict(mapping_obj)
    # Compatibilidad con objetos tipo namedtuple/Row legacy que exponen _asdict().
    elif hasattr(model_instance, "_asdict") and callable(
        getattr(model_instance, "_asdict")
    ):
        asdict_obj = t.cast(Mapping[str, t.Any], getattr(model_instance, "_asdict")())
        model_dict = dict(asdict_obj)
    # Soporte para objetos que son Mapping pero no tienen __dict__.
    elif isinstance(model_instance, Mapping):
        mapping_instance = t.cast(Mapping[str, t.Any], model_instance)
        model_dict = dict(mapping_instance)
    else:
        model_dict = vars(model_instance).copy()

    if is_nosql and "entity_id" in model_dict:
        model_dict["id"] = model_dict.pop("entity_id")

    resolver_source = t.cast(T, model_instance)
    model_dict = await _apply_async_field_resolvers(
        resolver_source, model_dict, field_resolvers
    )

    if is_nosql:
        return entity_class.model_construct(**model_dict)

    return entity_class.model_construct(**model_dict)


def get_all_concrete_subclasses(cls: type) -> set[type]:
    subclasses: set[type] = set()
    for subclass in cls.__subclasses__():
        if not getattr(subclass, "__abstractmethods__", set()):  # type: ignore[arg-type]
            subclasses.add(subclass)
        subclasses.update(get_all_concrete_subclasses(subclass))
    return subclasses


def _get_all_subclasses(cls: type) -> set[type]:
    subclasses: set[type] = set()
    for subclass in cls.__subclasses__():
        subclasses.add(subclass)
        subclasses.update(_get_all_subclasses(subclass))
    return subclasses


def _warn_for_abstract_repository(repo_cls: type) -> None:
    abstract_members_raw = t.cast(
        t.Iterable[str],
        getattr(repo_cls, "__abstractmethods__", _EMPTY_ABSTRACT_MEMBERS),
    )
    abstract_members = sorted(abstract_members_raw)
    if not abstract_members:
        return

    missing_members = ", ".join(abstract_members)
    warnings.warn(
        f"Repositorio abstracto detectado y omitido: {repo_cls.__module__}.{repo_cls.__name__}. "
        f"Miembros pendientes por implementar: {missing_members}. Este repositorio puede fallar si se intenta usar.",
        UserWarning,
        stacklevel=2,
    )


def _get_configured_repository_packages() -> set[str]:
    try:
        from hexcore.config import LazyConfig

        config = LazyConfig.get_config()
    except Exception:
        return set()

    configured = getattr(config, "repository_discovery_packages", ())
    if not isinstance(configured, (list, tuple, set)):
        return set()

    normalized_packages: set[str] = set()
    configured_packages = t.cast(t.Iterable[object], configured)
    for package in configured_packages:
        package_name = str(package).strip()
        if package_name:
            normalized_packages.add(package_name)

    return normalized_packages


def _iter_candidate_repository_packages() -> set[str]:
    packages = {
        "hexcore.infrastructure.repositories",
        "infrastructure.repositories",
        "src.infrastructure.repositories",
        "src.repositories",
        "repositories",
    }

    for module_name in tuple(sys.modules):
        if not module_name:
            continue
        if (
            ".interfaces" not in module_name
            and ".domain" not in module_name
            and ".infrastructure" not in module_name
        ):
            continue

        root_module = module_name.split(".", 1)[0]
        if not root_module:
            continue

        packages.add(f"{root_module}.infrastructure.repositories")
        packages.add(f"{root_module}.repositories")

    packages.update(_get_configured_repository_packages())

    return packages


def _import_package_and_submodules(package_name: str, strict: bool = False) -> None:
    if package_name in _AUTOLOADED_REPOSITORY_PACKAGES:
        return

    try:
        package = importlib.import_module(package_name)
    except ModuleNotFoundError:
        if strict:
            raise
        return

    _AUTOLOADED_REPOSITORY_PACKAGES.add(package_name)

    package_paths = getattr(package, "__path__", None)
    if not package_paths:
        return

    for module_info in pkgutil.walk_packages(package_paths, prefix=f"{package_name}."):
        try:
            importlib.import_module(module_info.name)
        except ModuleNotFoundError:
            if strict:
                raise
        except Exception:
            if strict:
                raise


def _autoload_repository_modules() -> None:
    configured_packages = _get_configured_repository_packages()
    for package_name in _iter_candidate_repository_packages():
        _import_package_and_submodules(
            package_name,
            strict=package_name in configured_packages,
        )


def _normalize_repository_module(module_name: str) -> str:
    if module_name.startswith("src."):
        return module_name[4:]
    return module_name


def _get_preferred_repository_prefixes() -> list[str]:
    try:
        from hexcore.config import LazyConfig

        config = LazyConfig.get_config()
    except Exception:
        return [
            "src.infrastructure.repositories",
            "infrastructure.repositories",
            "hexcore.infrastructure.repositories",
        ]

    configured = getattr(config, "repository_discovery_preferred_prefixes", ())
    if isinstance(configured, (list, tuple, set)):
        normalized: list[str] = []
        configured_iter = t.cast(t.Iterable[object], configured)
        for item in configured_iter:
            value = str(item).strip()
            if value:
                normalized.append(value)
        if normalized:
            return normalized

    return [
        "src.infrastructure.repositories",
        "infrastructure.repositories",
        "hexcore.infrastructure.repositories",
    ]


def _module_priority(module_name: str, prefixes: list[str]) -> int:
    normalized_module = module_name.strip()
    for index, prefix in enumerate(prefixes):
        normalized_prefix = prefix.strip()
        if normalized_module == normalized_prefix or normalized_module.startswith(
            f"{normalized_prefix}."
        ):
            return index
    return len(prefixes)


def _get_repository_class_source_path(repo_cls: type) -> str | None:
    try:
        source_file = inspect.getsourcefile(repo_cls)
    except (TypeError, OSError):
        source_file = None

    if source_file is None:
        return None

    return source_file.replace("\\", "/").lower()


def _repository_key_from_class_name(class_name: str) -> str:
    normalized_name = class_name.strip()
    lowered_name = normalized_name.lower()

    for suffix in ("repository", "repo"):
        if lowered_name.endswith(suffix):
            normalized_name = normalized_name[: -len(suffix)]
            break

    repo_key = "".join(char for char in normalized_name if char.isalnum()).lower()
    if not repo_key:
        raise ValueError(
            f"No se pudo derivar la clave del repositorio para la clase '{class_name}'."
        )

    return repo_key


def _discover_repositories(
    base_repository_class: type,
) -> t.Dict[str, type]:
    _autoload_repository_modules()

    preferred_prefixes = _get_preferred_repository_prefixes()
    repositories: t.Dict[str, type] = {}
    all_subclasses = _get_all_subclasses(base_repository_class)
    sorted_classes = sorted(
        all_subclasses,
        key=lambda cls: f"{cls.__module__}.{cls.__qualname__}",
    )

    for repo_cls in sorted_classes:
        abstract_methods = t.cast(
            t.Iterable[str],
            getattr(repo_cls, "__abstractmethods__", _EMPTY_ABSTRACT_MEMBERS),
        )
        if any(True for _ in abstract_methods):
            _warn_for_abstract_repository(repo_cls)
            continue

        repo_key = _repository_key_from_class_name(repo_cls.__name__)
        existing_repo_cls = repositories.get(repo_key)

        if existing_repo_cls is not None and existing_repo_cls is not repo_cls:
            existing_module = _normalize_repository_module(existing_repo_cls.__module__)
            current_module = _normalize_repository_module(repo_cls.__module__)
            existing_source = _get_repository_class_source_path(existing_repo_cls)
            current_source = _get_repository_class_source_path(repo_cls)

            if (
                existing_module == current_module
                and existing_repo_cls.__name__ == repo_cls.__name__
                and existing_repo_cls.__qualname__ == repo_cls.__qualname__
                and existing_source is not None
                and current_source is not None
                and existing_source == current_source
            ):
                warnings.warn(
                    "Repositorio duplicado detectado por alias de import y omitido: "
                    f"'{repo_key}' -> {repo_cls.__module__}.{repo_cls.__name__}. "
                    f"Se mantiene {existing_repo_cls.__module__}.{existing_repo_cls.__name__}.",
                    UserWarning,
                    stacklevel=2,
                )
                continue

            existing_priority = _module_priority(
                existing_repo_cls.__module__, preferred_prefixes
            )
            current_priority = _module_priority(repo_cls.__module__, preferred_prefixes)

            if current_priority < existing_priority:
                warnings.warn(
                    "Colision de repositorio resuelta por prioridad de modulo para "
                    f"'{repo_key}': se reemplaza "
                    f"{existing_repo_cls.__module__}.{existing_repo_cls.__name__} por "
                    f"{repo_cls.__module__}.{repo_cls.__name__}.",
                    UserWarning,
                    stacklevel=2,
                )
            elif current_priority > existing_priority:
                warnings.warn(
                    "Colision de repositorio resuelta por prioridad de modulo para "
                    f"'{repo_key}': se omite "
                    f"{repo_cls.__module__}.{repo_cls.__name__} y se mantiene "
                    f"{existing_repo_cls.__module__}.{existing_repo_cls.__name__}.",
                    UserWarning,
                    stacklevel=2,
                )
                continue
            else:
                raise ValueError(
                    "Se detecto una colision de nombres de repositorio para "
                    f"'{repo_key}': {existing_repo_cls.__module__}.{existing_repo_cls.__name__} "
                    f"y {repo_cls.__module__}.{repo_cls.__name__}."
                )

        repositories[repo_key] = repo_cls

    return repositories


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

    return t.cast(
        t.Dict[str, t.Type[BaseSQLAlchemyRepository[t.Any]]],
        _discover_repositories(BaseSQLAlchemyRepository),
    )


def discover_nosql_repositories() -> t.Dict[
    str,
    t.Type[BaseBeanieRepository[t.Any]],
]:
    """
    Descubre todos los repositorios NoSQL disponibles.

    Retorna un diccionario que mapea nombres de repositorios a sus clases.
    El nombre del repositorio se deriva del nombre de la clase, convirtiéndolo a minúsculas.
    Ejemplo:
        Si existe una clase UserRepository, se mapeará como 'user': UserRepository
    """

    return t.cast(
        t.Dict[str, t.Type[BaseBeanieRepository[t.Any]]],
        _discover_repositories(BaseBeanieRepository),
    )
