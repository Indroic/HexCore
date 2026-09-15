"""
El borde HTTP de HexCore.

⚠️ **Importar este paquete no puede exigir `[sql]`**, y por eso los dos nombres de `utils` entran
por `__getattr__` en vez de eagerly.

El motivo no es cosmético. `utils.py` construye endpoints de query sobre SQLAlchemy, así que su
import de `sqlalchemy.ext.asyncio` es legítimo. Pero importarlo desde acá hacía que **cualquier**
submódulo de este paquete arrastrara sqlalchemy —importar `api.rate_limit` ejecuta el `__init__`
del paquete, que es semántica de Python y no algo que se pueda esquivar— y eso alcanzaba a todo el
borde HTTP de Darwin: los routers de los plugins usan `rate_limit`, así que un despliegue en Mongo
no podía montar un magic link.

El `if t.TYPE_CHECKING` es lo que mantiene el tipado: pyright resuelve los dos nombres contra la
definición real, y en runtime el módulo sigue siendo perezoso.
"""
from __future__ import annotations

import typing as t

from .cqrs import (
    CQRSContainer,
    configure_cqrs,
    get_cqrs_container,
    provide_command_bus,
    provide_event_bus,
    provide_query_bus,
    provide_registry,
    reset_cqrs,
)
from .exception_handlers import (
    DEFAULT_EXCEPTION_STATUS_MAP,
    HeadersFactory,
    register_exception_handlers,
)
from .health import (
    DependencyReport,
    HealthReport,
    Probe,
    check_health,
    default_probes,
    register_health_routes,
)
from .middlewares import (
    REQUEST_ID,
    RequestIDLogFilter,
    RequestIDMiddleware,
    TimingMiddleware,
    get_request_id,
    install_request_id_logging,
)
from .rate_limit import client_ip_key, rate_limit
from .routing import build_root_router, mount_routers
from .streaming import connection_slot, format_sse_event, sse_stream, ws_heartbeat

__all__ = [
    "build_query_endpoint",
    "register_query_endpoint",
    # Middlewares (F11)
    "REQUEST_ID",
    "RequestIDMiddleware",
    "TimingMiddleware",
    "RequestIDLogFilter",
    "get_request_id",
    "install_request_id_logging",
    # Routing (F13)
    "build_root_router",
    "mount_routers",
    # Handlers de excepción (F5)
    "DEFAULT_EXCEPTION_STATUS_MAP",
    "HeadersFactory",
    "register_exception_handlers",
    # Providers CQRS (F6)
    "CQRSContainer",
    "configure_cqrs",
    "get_cqrs_container",
    "reset_cqrs",
    "provide_command_bus",
    "provide_query_bus",
    "provide_event_bus",
    "provide_registry",
    # Rate limiting (F12)
    "rate_limit",
    "client_ip_key",
    # Streaming (F14)
    "sse_stream",
    "format_sse_event",
    "ws_heartbeat",
    "connection_slot",
    # Health checks (F16)
    "check_health",
    "register_health_routes",
    "default_probes",
    "HealthReport",
    "DependencyReport",
    "Probe",
]


# ── Los dos nombres que exigen `[sql]` ────────────────────────────────────────
if t.TYPE_CHECKING:
    from .utils import build_query_endpoint as build_query_endpoint
    from .utils import register_query_endpoint as register_query_endpoint

#: Nombre público -> atributo en `.utils`.
_DIFERIDOS: dict[str, str] = {
    "build_query_endpoint": "build_query_endpoint",
    "register_query_endpoint": "register_query_endpoint",
}


def __getattr__(name: str) -> t.Any:
    """
    Resuelve los nombres de `utils` recién cuando se los pide (PEP 562).

    Un nombre que no está acá levanta `AttributeError` y no `ImportError`, que es lo que
    corresponde: un typo tiene que seguir dando el error de siempre.
    """
    atributo = _DIFERIDOS.get(name)
    if atributo is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    from importlib import import_module

    valor = getattr(import_module(".utils", __name__), atributo)
    globals()[name] = valor  # se memoiza: el segundo acceso no vuelve a importar
    return valor


def __dir__() -> list[str]:
    return sorted(__all__)
