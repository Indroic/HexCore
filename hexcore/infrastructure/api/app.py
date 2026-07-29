"""
Factory de la aplicación FastAPI.

Cada app repite CORS, middleware de request-id, handlers de excepción y health checks.
Aplicando las reglas de simplicidad del plan: el camino feliz es cero configuración
(`create_app()` a secas produce una app usable), y los interruptores van agrupados en un
solo objeto en vez de ocho keywords.
"""
from __future__ import annotations

import typing as t

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from .exception_handlers import register_exception_handlers
from .health import Probe, register_health_routes
from .middlewares import RequestIDMiddleware, TimingMiddleware
from .routing import MountableRouter, mount_routers

__all__ = ["AppFeatures", "create_app"]


class AppFeatures(BaseModel):
    """
    Qué cablea `create_app()`. Todo activado por defecto.

    Desactivar es explícito y localizado: ``create_app(features=AppFeatures(cors=False))``.
    """

    cors: bool = True
    """CORS con `config.allow_origins` / `allow_methods` / `allow_headers`."""

    request_id: bool = True
    """Middleware `X-Request-ID` (F11)."""

    timing: bool = True
    """Middleware `X-Response-Time-ms` (F11)."""

    exception_handlers: bool = True
    """Mapeo de excepciones de dominio a HTTP (F5)."""

    health: bool = True
    """Rutas `/health` y `/health/ready` (F16)."""


def create_app(
    lifespan: t.Any | None = None,
    *,
    features: AppFeatures | None = None,
    routers: t.Sequence[MountableRouter] | None = None,
    health_probes: t.Sequence[Probe] | None = None,
    exception_mapping: dict[type[Exception], int] | None = None,
    **fastapi_kwargs: t.Any,
) -> FastAPI:
    """
    Construye una `FastAPI` con lo que toda app necesita ya cableado.

    `create_app()` sin argumentos ya da una app usable: `title` y `version` salen de
    `ServerConfig`, CORS de `config.allow_origins`, y `/health` existe.

    Args:
        lifespan: El lifespan. Típicamente de `build_lifespan(...)` (F4).
        features: Qué cablear. Ver `AppFeatures`.
        routers: Routers a montar. Acepta `APIRouter` o `(APIRouter, kwargs)` (F13).
        health_probes: Sondas para `/health/ready`. Por defecto, las deducidas de la
            configuración (F16).
        exception_mapping: Excepciones extra a mapear, fusionadas con el default (F5).
        **fastapi_kwargs: Se pasan tal cual a `FastAPI` (`title`, `version`,
            `docs_url`, `openapi_tags`…). Lo que pases gana sobre los defaults derivados
            de la configuración.

    Returns:
        La aplicación, lista para servir.
    """
    resolved_features = features or AppFeatures()
    config = _config()

    app = FastAPI(lifespan=lifespan, **{**_fastapi_defaults(config), **fastapi_kwargs})

    # Los middlewares de Starlette se ejecutan en orden inverso al de registro, así que
    # el request-id se añade último para que envuelva a todo lo demás y su ContextVar
    # esté disponible en el resto de la pila.
    if resolved_features.cors:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(getattr(config, "allow_origins", ["*"])),
            allow_credentials=bool(getattr(config, "allow_credentials", True)),
            allow_methods=list(getattr(config, "allow_methods", ["*"])),
            allow_headers=list(getattr(config, "allow_headers", ["*"])),
        )
    if resolved_features.timing:
        app.add_middleware(TimingMiddleware)
    if resolved_features.request_id:
        app.add_middleware(RequestIDMiddleware)

    if resolved_features.exception_handlers:
        register_exception_handlers(app, mapping=exception_mapping)

    if resolved_features.health:
        register_health_routes(app, probes=health_probes)

    if routers:
        mount_routers(app, list(routers))

    return app


def _fastapi_defaults(config: t.Any) -> dict[str, t.Any]:
    """
    Defaults derivados de `ServerConfig`.

    `debug` gobierna la documentación interactiva: en producción, `/docs` abierto es una
    decisión que hay que tomar a propósito, no heredar.
    """
    debug = bool(getattr(config, "debug", False))
    return {
        "title": getattr(config, "app_title", None) or "HexCore API",
        "version": getattr(config, "app_version", None) or "0.1.0",
        "debug": debug,
        "docs_url": "/docs" if debug else None,
        "redoc_url": "/redoc" if debug else None,
        "openapi_url": "/openapi.json" if debug else None,
    }


def _config() -> t.Any:
    from hexcore.config import LazyConfig

    return LazyConfig.get_config()
