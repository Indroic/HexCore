"""
Fachada de la capa FastAPI: un import obvio por tarea.

El arranque completo de una app HexCore::

    from hexcore.fastapi import create_app, build_lifespan, SqlEngineStep, BeanieStep

    app = create_app(
        lifespan=build_lifespan(SqlEngineStep(), BeanieStep(documents=DOCS)),
        routers=[usuarios_router, tickets_router],
    )

Requiere el extra ``[api]``. La resolución es perezosa, así que importar este módulo no
falla sin FastAPI; falla al pedir el primer nombre.
"""
from __future__ import annotations

import typing as t

_EXPORTS: dict[str, tuple[str, str]] = {
    # ── App y lifespan ────────────────────────────────────────────────────────
    "create_app": ("hexcore.infrastructure.api.app", "create_app"),
    "AppFeatures": ("hexcore.infrastructure.api.app", "AppFeatures"),
    "HealthRoutes": ("hexcore.infrastructure.api.app", "HealthRoutes"),
    "build_lifespan": ("hexcore.infrastructure.api.lifespan", "build_lifespan"),
    "StartupStep": ("hexcore.infrastructure.api.lifespan", "StartupStep"),
    "CallableStep": ("hexcore.infrastructure.api.lifespan", "CallableStep"),
    "SqlEngineStep": ("hexcore.infrastructure.api.lifespan", "SqlEngineStep"),
    "BeanieStep": ("hexcore.infrastructure.api.lifespan", "BeanieStep"),
    "EventBusStep": ("hexcore.infrastructure.api.lifespan", "EventBusStep"),
    "CacheStep": ("hexcore.infrastructure.api.lifespan", "CacheStep"),
    "ProcrastinateStep": ("hexcore.infrastructure.api.lifespan", "ProcrastinateStep"),
    "CronSeedStep": ("hexcore.infrastructure.api.lifespan", "CronSeedStep"),
    # ── Middlewares ───────────────────────────────────────────────────────────
    "RequestIDMiddleware": (
        "hexcore.infrastructure.api.middlewares",
        "RequestIDMiddleware",
    ),
    "TimingMiddleware": ("hexcore.infrastructure.api.middlewares", "TimingMiddleware"),
    "RequestIDLogFilter": (
        "hexcore.infrastructure.api.middlewares",
        "RequestIDLogFilter",
    ),
    "get_request_id": ("hexcore.infrastructure.api.middlewares", "get_request_id"),
    "install_request_id_logging": (
        "hexcore.infrastructure.api.middlewares",
        "install_request_id_logging",
    ),
    # ── Routing ───────────────────────────────────────────────────────────────
    "build_root_router": ("hexcore.infrastructure.api.routing", "build_root_router"),
    "mount_routers": ("hexcore.infrastructure.api.routing", "mount_routers"),
    # ── Providers CQRS ────────────────────────────────────────────────────────
    "configure_cqrs": ("hexcore.infrastructure.api.cqrs", "configure_cqrs"),
    "get_cqrs_container": ("hexcore.infrastructure.api.cqrs", "get_cqrs_container"),
    "reset_cqrs": ("hexcore.infrastructure.api.cqrs", "reset_cqrs"),
    "CQRSContainer": ("hexcore.infrastructure.api.cqrs", "CQRSContainer"),
    "provide_command_bus": ("hexcore.infrastructure.api.cqrs", "provide_command_bus"),
    "provide_query_bus": ("hexcore.infrastructure.api.cqrs", "provide_query_bus"),
    "provide_event_bus": ("hexcore.infrastructure.api.cqrs", "provide_event_bus"),
    "provide_registry": ("hexcore.infrastructure.api.cqrs", "provide_registry"),
    # ── Sesiones y UoW como dependencias ──────────────────────────────────────
    "get_session": ("hexcore.infrastructure.api.utils", "get_session"),
    "get_sql_uow": ("hexcore.infrastructure.api.utils", "get_sql_uow"),
    "get_sql_uow_open": ("hexcore.infrastructure.api.utils", "get_sql_uow_open"),
    "get_nosql_uow": ("hexcore.infrastructure.api.utils", "get_nosql_uow"),
    # ── Endpoints de query ────────────────────────────────────────────────────
    "build_query_endpoint": (
        "hexcore.infrastructure.api.utils",
        "build_query_endpoint",
    ),
    "register_query_endpoint": (
        "hexcore.infrastructure.api.utils",
        "register_query_endpoint",
    ),
    # ── Handlers de excepción ─────────────────────────────────────────────────
    "register_exception_handlers": (
        "hexcore.infrastructure.api.exception_handlers",
        "register_exception_handlers",
    ),
    "DEFAULT_EXCEPTION_STATUS_MAP": (
        "hexcore.infrastructure.api.exception_handlers",
        "DEFAULT_EXCEPTION_STATUS_MAP",
    ),
    # ── Rate limiting ─────────────────────────────────────────────────────────
    "rate_limit": ("hexcore.infrastructure.api.rate_limit", "rate_limit"),
    "client_ip_key": ("hexcore.infrastructure.api.rate_limit", "client_ip_key"),
    # ── Streaming ─────────────────────────────────────────────────────────────
    "sse_stream": ("hexcore.infrastructure.api.streaming", "sse_stream"),
    "format_sse_event": ("hexcore.infrastructure.api.streaming", "format_sse_event"),
    "ws_heartbeat": ("hexcore.infrastructure.api.streaming", "ws_heartbeat"),
    "connection_slot": ("hexcore.infrastructure.api.streaming", "connection_slot"),
    # ── Health checks ─────────────────────────────────────────────────────────
    "check_health": ("hexcore.infrastructure.api.health", "check_health"),
    "register_health_routes": (
        "hexcore.infrastructure.api.health",
        "register_health_routes",
    ),
    "default_probes": ("hexcore.infrastructure.api.health", "default_probes"),
    "HealthReport": ("hexcore.infrastructure.api.health", "HealthReport"),
    "DependencyReport": ("hexcore.infrastructure.api.health", "DependencyReport"),
    "Probe": ("hexcore.infrastructure.api.health", "Probe"),
    "ResponseFactory": ("hexcore.infrastructure.api.health", "ResponseFactory"),
}

__all__ = sorted(_EXPORTS)


def __getattr__(name: str) -> t.Any:
    try:
        module_path, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(
            f"module 'hexcore.fastapi' has no attribute {name!r}"
        ) from None

    import importlib

    value = getattr(importlib.import_module(module_path), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return __all__
