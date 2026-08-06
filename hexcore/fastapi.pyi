# ⚠️  ARCHIVO GENERADO — NO EDITAR A MANO.
#
# Generado por `scripts/gen_stubs.py` desde el `_EXPORTS` de `hexcore/fastapi.py`.
# Si editás esto a mano, el job `stubs-drift` de CI te lo va a revertir.
#
# Para regenerar:
#
#     uv run python scripts/gen_stubs.py --write
#
# Existe porque la fachada resuelve sus exports con `__getattr__` y declara
# `__all__ = sorted(_EXPORTS)`: las dos son expresiones de runtime, así que sin este stub
# los 46 símbolos de `hexcore.fastapi` tipan `Any`. El runtime no cambia — Python usa
# el `.py` y el checker usa el `.pyi`, así que la carga perezosa se mantiene.


from hexcore.infrastructure.api.app import AppFeatures as AppFeatures
from hexcore.infrastructure.api.app import create_app as create_app
from hexcore.infrastructure.api.cqrs import CQRSContainer as CQRSContainer
from hexcore.infrastructure.api.cqrs import configure_cqrs as configure_cqrs
from hexcore.infrastructure.api.cqrs import get_cqrs_container as get_cqrs_container
from hexcore.infrastructure.api.cqrs import provide_command_bus as provide_command_bus
from hexcore.infrastructure.api.cqrs import provide_event_bus as provide_event_bus
from hexcore.infrastructure.api.cqrs import provide_query_bus as provide_query_bus
from hexcore.infrastructure.api.cqrs import provide_registry as provide_registry
from hexcore.infrastructure.api.cqrs import reset_cqrs as reset_cqrs
from hexcore.infrastructure.api.exception_handlers import DEFAULT_EXCEPTION_STATUS_MAP as DEFAULT_EXCEPTION_STATUS_MAP
from hexcore.infrastructure.api.exception_handlers import register_exception_handlers as register_exception_handlers
from hexcore.infrastructure.api.health import DependencyReport as DependencyReport
from hexcore.infrastructure.api.health import HealthReport as HealthReport
from hexcore.infrastructure.api.health import Probe as Probe
from hexcore.infrastructure.api.health import check_health as check_health
from hexcore.infrastructure.api.health import default_probes as default_probes
from hexcore.infrastructure.api.health import register_health_routes as register_health_routes
from hexcore.infrastructure.api.lifespan import BeanieStep as BeanieStep
from hexcore.infrastructure.api.lifespan import CacheStep as CacheStep
from hexcore.infrastructure.api.lifespan import CallableStep as CallableStep
from hexcore.infrastructure.api.lifespan import CronSeedStep as CronSeedStep
from hexcore.infrastructure.api.lifespan import EventBusStep as EventBusStep
from hexcore.infrastructure.api.lifespan import ProcrastinateStep as ProcrastinateStep
from hexcore.infrastructure.api.lifespan import SqlEngineStep as SqlEngineStep
from hexcore.infrastructure.api.lifespan import StartupStep as StartupStep
from hexcore.infrastructure.api.lifespan import build_lifespan as build_lifespan
from hexcore.infrastructure.api.middlewares import RequestIDLogFilter as RequestIDLogFilter
from hexcore.infrastructure.api.middlewares import RequestIDMiddleware as RequestIDMiddleware
from hexcore.infrastructure.api.middlewares import TimingMiddleware as TimingMiddleware
from hexcore.infrastructure.api.middlewares import get_request_id as get_request_id
from hexcore.infrastructure.api.middlewares import install_request_id_logging as install_request_id_logging
from hexcore.infrastructure.api.rate_limit import client_ip_key as client_ip_key
from hexcore.infrastructure.api.rate_limit import rate_limit as rate_limit
from hexcore.infrastructure.api.routing import build_root_router as build_root_router
from hexcore.infrastructure.api.routing import mount_routers as mount_routers
from hexcore.infrastructure.api.streaming import connection_slot as connection_slot
from hexcore.infrastructure.api.streaming import format_sse_event as format_sse_event
from hexcore.infrastructure.api.streaming import sse_stream as sse_stream
from hexcore.infrastructure.api.streaming import ws_heartbeat as ws_heartbeat
from hexcore.infrastructure.api.utils import build_query_endpoint as build_query_endpoint
from hexcore.infrastructure.api.utils import get_nosql_uow as get_nosql_uow
from hexcore.infrastructure.api.utils import get_session as get_session
from hexcore.infrastructure.api.utils import get_sql_uow as get_sql_uow
from hexcore.infrastructure.api.utils import get_sql_uow_open as get_sql_uow_open
from hexcore.infrastructure.api.utils import register_query_endpoint as register_query_endpoint

__all__ = [
    "AppFeatures",
    "BeanieStep",
    "CQRSContainer",
    "CacheStep",
    "CallableStep",
    "CronSeedStep",
    "DEFAULT_EXCEPTION_STATUS_MAP",
    "DependencyReport",
    "EventBusStep",
    "HealthReport",
    "Probe",
    "ProcrastinateStep",
    "RequestIDLogFilter",
    "RequestIDMiddleware",
    "SqlEngineStep",
    "StartupStep",
    "TimingMiddleware",
    "build_lifespan",
    "build_query_endpoint",
    "build_root_router",
    "check_health",
    "client_ip_key",
    "configure_cqrs",
    "connection_slot",
    "create_app",
    "default_probes",
    "format_sse_event",
    "get_cqrs_container",
    "get_nosql_uow",
    "get_request_id",
    "get_session",
    "get_sql_uow",
    "get_sql_uow_open",
    "install_request_id_logging",
    "mount_routers",
    "provide_command_bus",
    "provide_event_bus",
    "provide_query_bus",
    "provide_registry",
    "rate_limit",
    "register_exception_handlers",
    "register_health_routes",
    "register_query_endpoint",
    "reset_cqrs",
    "sse_stream",
    "ws_heartbeat",
]
