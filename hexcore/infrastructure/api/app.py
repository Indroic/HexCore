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

from .exception_handlers import HeadersFactory, register_exception_handlers
from .health import Probe, ResponseFactory, register_health_routes
from .middlewares import RequestIDMiddleware, TimingMiddleware
from .routing import MountableRouter, mount_routers

__all__ = ["AppFeatures", "HealthRoutes", "create_app"]


class HealthRoutes(BaseModel):
    """
    Qué rutas de health cablea `create_app()`, y con qué forma.

    Existe para que una app **ya publicada** pueda adoptar la readiness sin renunciar a
    su `/health` ni al cliente tipado generado desde su OpenAPI. Los argumentos son los
    de `register_health_routes`, agrupados para no engordar la firma de `create_app`.
    """

    model_config = {"arbitrary_types_allowed": True}

    path: str = "/health"
    liveness: bool = True
    readiness: bool = True
    readiness_path: str | None = None
    response_factory: ResponseFactory | None = None


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

    health: bool | HealthRoutes = True
    """
    Rutas `/health` y `/health/ready` (F16).

    Acepta también un `HealthRoutes` para adoptarlas **por partes**: una app que ya
    publica su propio `/health` puede quedarse con la readiness, que es la que no se
    escribe a mano, sin apagar la feature entera::

        AppFeatures(health=HealthRoutes(liveness=False))
    """

    auth_context: bool = False
    """
    Middleware que resuelve la credencial y publica el `AuthContext` (Darwin, Fase 7).

    **Apagado por defecto**, al revés que el resto. Prenderlo en silencio cambiaría el
    comportamiento de toda app existente: `create_app()` empezaría a resolver credenciales y
    a exigir que Darwin esté configurado. Es una decisión que se toma, no que se hereda.

    Prenderlo también hace que `create_app` mergee `IDENTITY_EXCEPTION_STATUS_MAP` y ponga el
    `WWW-Authenticate` de los 401 (RFC 6750 §3).
    """

    csrf: bool = False
    """
    Chequeo anti-CSRF del transporte por cookie (Darwin, Fase 7).

    Apagado por defecto y **separado de `auth_context`** a propósito: una API que sólo sirve
    clientes Bearer no necesita CSRF —el cliente adjunta el token a propósito— y prenderlo
    ahí sólo agregaría un chequeo que nunca se dispara. Si servís sesiones por cookie,
    prendelo.
    """


def create_app(
    lifespan: t.Any | None = None,
    *,
    features: AppFeatures | None = None,
    routers: t.Sequence[MountableRouter] | None = None,
    health_probes: t.Sequence[Probe] | None = None,
    exception_mapping: dict[type[Exception], int] | None = None,
    exception_headers: HeadersFactory | None = None,
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
            configuración (F16). Qué rutas se registran y con qué forma se controla con
            `AppFeatures(health=HealthRoutes(...))`.
        exception_mapping: Excepciones extra a mapear, fusionadas con el default (F5).
        exception_headers: Headers extra por excepción. Necesario para los status que
            los exigen por especificación: un 401 tiene que llevar `WWW-Authenticate`
            (RFC 6750 §3).
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

    # Los de Darwin van **antes** de `RequestIDMiddleware` en el orden de registro, o sea
    # que corren **adentro** de él: cuando el middleware de auth loguea o el sink de
    # auditoría lee el request-id, ya está poblado. Y el CSRF va antes que el de contexto
    # para correr por fuera: rechazar una petición cross-origin no debería costar el trabajo
    # de verificar su token.
    if resolved_features.csrf:
        from hexcore.darwin.infrastructure.api.middlewares import CsrfMiddleware

        app.add_middleware(CsrfMiddleware)
    if resolved_features.auth_context:
        from hexcore.darwin.infrastructure.api.middlewares import AuthContextMiddleware

        app.add_middleware(AuthContextMiddleware)

    if resolved_features.request_id:
        app.add_middleware(RequestIDMiddleware)

    if resolved_features.exception_handlers:
        mapping, headers_for = _con_darwin(
            resolved_features, exception_mapping, exception_headers
        )
        register_exception_handlers(app, mapping=mapping, headers_for=headers_for)

    if resolved_features.health:
        health_routes = (
            resolved_features.health
            if isinstance(resolved_features.health, HealthRoutes)
            else HealthRoutes()
        )
        register_health_routes(
            app,
            probes=health_probes,
            path=health_routes.path,
            liveness=health_routes.liveness,
            readiness=health_routes.readiness,
            readiness_path=health_routes.readiness_path,
            response_factory=health_routes.response_factory,
        )

    if routers:
        mount_routers(app, list(routers))

    return app


def _con_darwin(
    features: AppFeatures,
    mapping: dict[type[Exception], int] | None,
    headers_for: HeadersFactory | None,
) -> tuple[dict[type[Exception], int] | None, HeadersFactory | None]:
    """
    Mergea el mapa de excepciones de Darwin y su fábrica de headers, si `auth_context`.

    **`IDENTITY_EXCEPTION_STATUS_MAP` no se agrega a `DEFAULT_EXCEPTION_STATUS_MAP`.**
    Importar las excepciones de Darwin en tiempo de import de esta capa la acoplaría al
    módulo de identidad y rompería el contrato de dependencias opcionales que
    `tests/test_optional_dependencies.py` verifica. Se importa acá, adentro de la función y
    sólo cuando el feature está prendido.

    Lo que pase el consumidor **gana**: se mergea con el de Darwin debajo, no encima.
    """
    if not features.auth_context:
        return mapping, headers_for

    from hexcore.darwin.domain.exceptions import IDENTITY_EXCEPTION_STATUS_MAP
    from hexcore.darwin.infrastructure.api.dependencies import (
        identity_exception_headers,
    )

    combinado = {**IDENTITY_EXCEPTION_STATUS_MAP, **(mapping or {})}

    if headers_for is None:
        return combinado, identity_exception_headers

    def combinar(exc: Exception) -> t.Mapping[str, str]:
        # El del consumidor gana sobre el de Darwin, igual que con el mapa.
        return {**identity_exception_headers(exc), **headers_for(exc)}

    return combinado, combinar


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
