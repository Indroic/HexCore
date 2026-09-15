"""
El router de `impersonate`. Requiere `[api]`.

Tres rutas, las tres autenticadas — no hay ninguna pública acá: impersonar se define por tener un
actor identificado.

⚠️ **`POST /auth/impersonate/{user_id}` emite una sesión nueva por el transporte actual.** Con
cookie, eso **reemplaza la cookie del operador en el navegador**, y es lo correcto: es lo que hace
que las pestañas siguientes vean lo que ve el sujeto. Terminar la impersonación exige volver a
iniciar sesión con cookie, o usar Bearer si querés conservar las dos sesiones en paralelo — que es
lo que conviene para una herramienta de soporte.
"""
# pyright: reportUnusedFunction=false
#
# En un módulo de router ninguna función se llama por nombre: a todas las registra su decorador.
from __future__ import annotations

import typing as t
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

__all__ = ["StartImpersonationBody", "ImpersonationStatus", "build_impersonate_router"]


class StartImpersonationBody(BaseModel):
    """
    El cuerpo de `POST /auth/impersonate/{user_id}`.

    `reason` con `min_length=1` para que el 422 salga del borde y no del modelo de dominio: el
    error de validación de FastAPI dice qué campo falta, y el `ValueError` del servicio no.
    """

    reason: str = Field(min_length=1, max_length=500)


class ImpersonationStatus(BaseModel):
    active: bool
    actor_id: str | None = None
    subject_id: str | None = None
    reason: str | None = None
    expires_at: str | None = None


def build_impersonate_router(
    *,
    prefix: str = "/auth/impersonate",
    tags: t.Sequence[str] = ("auth", "impersonate"),
    rate_limit: tuple[int, int] | None = (10, 300),
) -> APIRouter:
    """
    Construye el router del plugin.

    Args:
        prefix: Prefijo de las rutas.
        tags: Tags de OpenAPI.
        rate_limit: `(intentos, ventana)` para iniciar. Existe aunque la ruta esté autenticada y
            requiera un scope: si un operador legítimo queda comprometido, el límite convierte
            "impersonar a toda la base de usuarios" en algo que tarda y se nota en las métricas.

    Uso::

        from hexcore.darwin.plugins.impersonate.router import build_impersonate_router

        app = create_app(routers=[build_identity_router(), build_impersonate_router()])
    """
    from hexcore.darwin.infrastructure.api.dependencies import provide_auth

    router = APIRouter(prefix=prefix, tags=list(tags))
    limite = _rate_limit(rate_limit)

    @router.get("")
    async def status(auth: t.Any = Depends(provide_auth)) -> ImpersonationStatus:
        """
        El estado de impersonación de la sesión actual.

        Es lo que alimenta la barra de "estás viendo como…", y esa barra no es cosmética: sin
        ella, un operador olvida que está impersonando y toma decisiones creyendo que actúa como
        él mismo.
        """
        from hexcore.darwin.plugins.impersonate import get_impersonation_service

        info = await get_impersonation_service().describe(auth)
        return ImpersonationStatus(
            active=info.active,
            actor_id=str(info.actor_id) if info.actor_id else None,
            subject_id=str(info.subject_id) if info.subject_id else None,
            reason=info.reason,
            expires_at=info.expires_at.isoformat() if info.expires_at else None,
        )

    # ⚠️ `/stop` va declarado **antes** de `/{user_id}`: FastAPI resuelve las rutas en orden de
    # registro, así que con `/{user_id}` primero, un `POST /auth/impersonate/stop` matchea la
    # ruta paramétrica e intenta parsear `"stop"` como UUID — 422 en vez de terminar la
    # impersonación. Lo encontró un test, no la revisión.
    @router.post("/stop")
    async def stop(
        auth: t.Any = Depends(provide_auth),
        transport: t.Any = Depends(_resolve_transport),
    ) -> Response:
        """
        Termina la impersonación: revoca la sesión impersonada y borra sus cookies.

        No devuelve una sesión: la del operador nunca se tocó. Con cookie hay que volver a
        iniciar sesión, y esa asimetría es deliberada — ver el docstring del módulo.
        """
        from hexcore.darwin.plugins.impersonate import get_impersonation_service

        await get_impersonation_service().stop(context=auth)

        respuesta = JSONResponse({"stopped": True})
        transport.clear(respuesta)
        return respuesta

    @router.post("/{user_id}", dependencies=limite)
    async def start(
        user_id: UUID,
        payload: StartImpersonationBody,
        request: Request,
        auth: t.Any = Depends(provide_auth),
        transport: t.Any = Depends(_resolve_transport),
    ) -> Response:
        """
        Empieza una impersonación. Ver la advertencia del docstring del módulo sobre cookies.

        Reusa `emit_tokens` y `session_response_body` del router de identidad: la sesión
        impersonada es una sesión normal en todo lo demás, y armar su respuesta a mano sería la
        copia que se olvida de un atributo de cookie.
        """
        from hexcore.darwin.application.container import get_identity_container
        from hexcore.darwin.infrastructure.api.routers import (
            emit_tokens,
            session_response_body,
        )
        from hexcore.darwin.plugins.impersonate import get_impersonation_service

        resultado = await get_impersonation_service().start(
            context=auth,
            subject_id=user_id,
            reason=payload.reason,
            transport=transport.name,
            ip_address=_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )

        cuerpo = session_response_body(resultado.tokens, transport).model_dump(
            exclude_none=True
        )
        cuerpo["impersonating"] = str(resultado.subject.id)
        cuerpo["expires_at"] = (
            resultado.session.impersonation_expires_at.isoformat()
            if resultado.session.impersonation_expires_at
            else None
        )

        contenedor = get_identity_container()
        respuesta = JSONResponse(cuerpo)
        emit_tokens(respuesta, resultado.tokens, transport, contenedor.config)
        return respuesta

    return router


def _resolve_transport(request: Request) -> t.Any:
    from hexcore.darwin.infrastructure.api.routers import resolve_transport

    return resolve_transport(request)


def _rate_limit(spec: tuple[int, int] | None) -> list[t.Any]:
    if spec is None:
        return []

    from hexcore.infrastructure.api.rate_limit import client_ip_key, rate_limit

    intentos, ventana = spec
    return [
        Depends(
            rate_limit(
                intentos,
                ventana,
                key=client_ip_key,
                on_backend_error="deny",
                namespace="hexcore:darwin:impersonate",
            )
        )
    ]


def _ip(request: Request) -> str | None:
    cliente = request.client
    return cliente.host if cliente else None
