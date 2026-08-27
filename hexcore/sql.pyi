# ⚠️  ARCHIVO GENERADO — NO EDITAR A MANO.
#
# Generado por `scripts/gen_stubs.py` desde el `_EXPORTS` de `hexcore/sql.py`.
# Si editás esto a mano, el job `stubs-drift` de CI te lo va a revertir.
#
# Para regenerar:
#
#     uv run python scripts/gen_stubs.py --write
#
# Existe porque la fachada resuelve sus exports con `__getattr__` y declara
# `__all__ = sorted(_EXPORTS)`: las dos son expresiones de runtime, así que sin este stub
# los 27 símbolos de `hexcore.sql` tipan `Any`. El runtime no cambia — Python usa
# el `.py` y el checker usa el `.pyi`, así que la carga perezosa se mantiene.


from hexcore.application.dtos.cursor import CursorPageDTO as CursorPageDTO
from hexcore.application.dtos.cursor import CursorRequestDTO as CursorRequestDTO
from hexcore.application.dtos.errors import UnsupportedQueryFieldError as UnsupportedQueryFieldError
from hexcore.application.dtos.query import FilterConditionDTO as FilterConditionDTO
from hexcore.application.dtos.query import FilterOperator as FilterOperator
from hexcore.application.dtos.query import QueryRequestDTO as QueryRequestDTO
from hexcore.application.dtos.query import QueryResponseDTO as QueryResponseDTO
from hexcore.application.dtos.query import SortConditionDTO as SortConditionDTO
from hexcore.application.dtos.query import SortDirection as SortDirection
from hexcore.infrastructure.repositories.base import BaseSQLAlchemyRepository as BaseSQLAlchemyRepository
from hexcore.infrastructure.repositories.implementations import SqlAlchemyRepository as SqlAlchemyRepository
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base as Base
from hexcore.infrastructure.repositories.orms.sqlalchemy import BaseModel as BaseModel
from hexcore.infrastructure.repositories.orms.sqlalchemy import NAMING_CONVENTION as NAMING_CONVENTION
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import PoolSettings as PoolSettings
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import dispose_engine as dispose_engine
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import get_engine as get_engine
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import get_session_factory as get_session_factory
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import init_engine as init_engine
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import normalize_async_dsn as normalize_async_dsn
from hexcore.infrastructure.repositories.orms.sqlalchemy.utils import ensure_framework_models_loaded as ensure_framework_models_loaded
from hexcore.infrastructure.repositories.orms.sqlalchemy.utils import import_all_models as import_all_models
from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork as SqlAlchemyUnitOfWork
from hexcore.infrastructure.uow.scopes import nosql_uow_scope as nosql_uow_scope
from hexcore.infrastructure.uow.scopes import open_uow_scope as open_uow_scope
from hexcore.infrastructure.uow.scopes import session_scope as session_scope
from hexcore.infrastructure.uow.scopes import uow_scope as uow_scope

__all__ = [
    "Base",
    "BaseModel",
    "BaseSQLAlchemyRepository",
    "CursorPageDTO",
    "CursorRequestDTO",
    "FilterConditionDTO",
    "FilterOperator",
    "NAMING_CONVENTION",
    "PoolSettings",
    "QueryRequestDTO",
    "QueryResponseDTO",
    "SortConditionDTO",
    "SortDirection",
    "SqlAlchemyRepository",
    "SqlAlchemyUnitOfWork",
    "UnsupportedQueryFieldError",
    "dispose_engine",
    "ensure_framework_models_loaded",
    "get_engine",
    "get_session_factory",
    "import_all_models",
    "init_engine",
    "normalize_async_dsn",
    "nosql_uow_scope",
    "open_uow_scope",
    "session_scope",
    "uow_scope",
]
