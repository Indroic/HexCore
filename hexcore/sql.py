"""
Fachada de la capa SQL: un import obvio por tarea.

::

    import hexcore.sql as sql

    sql.init_engine()                       # arranque
    async with sql.session_scope() as s:    # fuera de un request
        ...
    await sql.dispose_engine()              # apagado

Requiere el extra ``[sql]``. La resolución es perezosa, así que importar este módulo no
falla sin SQLAlchemy; falla —con el error de SQLAlchemy— al pedir el primer nombre.
"""
from __future__ import annotations

import typing as t

_EXPORTS: dict[str, tuple[str, str]] = {
    # ── Engine y sesiones ─────────────────────────────────────────────────────
    "init_engine": (
        "hexcore.infrastructure.repositories.orms.sqlalchemy.session",
        "init_engine",
    ),
    "dispose_engine": (
        "hexcore.infrastructure.repositories.orms.sqlalchemy.session",
        "dispose_engine",
    ),
    "get_engine": (
        "hexcore.infrastructure.repositories.orms.sqlalchemy.session",
        "get_engine",
    ),
    "get_session_factory": (
        "hexcore.infrastructure.repositories.orms.sqlalchemy.session",
        "get_session_factory",
    ),
    "PoolSettings": (
        "hexcore.infrastructure.repositories.orms.sqlalchemy.session",
        "PoolSettings",
    ),
    "normalize_async_dsn": (
        "hexcore.infrastructure.repositories.orms.sqlalchemy.session",
        "normalize_async_dsn",
    ),
    # ── Scopes (fuera de FastAPI) ─────────────────────────────────────────────
    "session_scope": ("hexcore.infrastructure.uow.scopes", "session_scope"),
    "uow_scope": ("hexcore.infrastructure.uow.scopes", "uow_scope"),
    "open_uow_scope": ("hexcore.infrastructure.uow.scopes", "open_uow_scope"),
    "nosql_uow_scope": ("hexcore.infrastructure.uow.scopes", "nosql_uow_scope"),
    # ── Modelos y UoW ─────────────────────────────────────────────────────────
    "Base": ("hexcore.infrastructure.repositories.orms.sqlalchemy", "Base"),
    "NAMING_CONVENTION": (
        "hexcore.infrastructure.repositories.orms.sqlalchemy",
        "NAMING_CONVENTION",
    ),
    "BaseModel": ("hexcore.infrastructure.repositories.orms.sqlalchemy", "BaseModel"),
    "SqlAlchemyUnitOfWork": ("hexcore.infrastructure.uow", "SqlAlchemyUnitOfWork"),
    "SqlAlchemyRepository": (
        "hexcore.infrastructure.repositories.implementations",
        "SqlAlchemyRepository",
    ),
    "BaseSQLAlchemyRepository": (
        "hexcore.infrastructure.repositories.base",
        "BaseSQLAlchemyRepository",
    ),
    # ── Migraciones (para el env.py de Alembic) ───────────────────────────────
    "import_all_models": (
        "hexcore.infrastructure.repositories.orms.sqlalchemy.utils",
        "import_all_models",
    ),
    "ensure_framework_models_loaded": (
        "hexcore.infrastructure.repositories.orms.sqlalchemy.utils",
        "ensure_framework_models_loaded",
    ),
    # ── Consultas ─────────────────────────────────────────────────────────────
    "QueryRequestDTO": ("hexcore.application.dtos.query", "QueryRequestDTO"),
    "QueryResponseDTO": ("hexcore.application.dtos.query", "QueryResponseDTO"),
    "FilterConditionDTO": ("hexcore.application.dtos.query", "FilterConditionDTO"),
    "FilterOperator": ("hexcore.application.dtos.query", "FilterOperator"),
    "SortConditionDTO": ("hexcore.application.dtos.query", "SortConditionDTO"),
    "SortDirection": ("hexcore.application.dtos.query", "SortDirection"),
    "CursorPageDTO": ("hexcore.application.dtos.cursor", "CursorPageDTO"),
    "CursorRequestDTO": ("hexcore.application.dtos.cursor", "CursorRequestDTO"),
    "UnsupportedQueryFieldError": (
        "hexcore.application.dtos.errors",
        "UnsupportedQueryFieldError",
    ),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> t.Any:
    try:
        module_path, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module 'hexcore.sql' has no attribute {name!r}") from None

    import importlib

    value = getattr(importlib.import_module(module_path), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return __all__
