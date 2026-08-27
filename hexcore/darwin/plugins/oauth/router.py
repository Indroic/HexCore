"""
El router de `oauth`. Requiere `[api]`.

Cinco rutas. La división importante:

- `GET /auth/oauth/{provider}/start` y `GET /auth/oauth/{provider}/callback` son **públicas**: la
  primera la usa quien todavía no tiene sesión, y la segunda la invoca el navegador siguiendo un
  redirect del proveedor. Su protección es el `state` de un solo uso más PKCE.
- `POST /auth/oauth/{provider}/link` y `DELETE /auth/oauth/{provider}` exigen sesión: son
  operaciones sobre la propia cuenta.

⚠️ **El callback devuelve JSON, no un redirect al frontend.** Un redirect con los tokens en el
fragmento o en la query los deja en el historial del navegador y en el `Referer` de la página
siguiente. Con cookie el flujo cierra solo —la cookie ya va en la respuesta— y quien necesite
volver a una SPA puede envolver esta ruta con su propio redirect sin tokens en la URL.
"""
# pyright: reportUnusedFunction=false
#
# En un módulo de router ninguna función se llama por nombre: a todas las registra su decorador.
from __future__ import annotations

import typing as t

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

__all__ = [
    "AuthorizationResponse",
    "LinkedProviders",
    "build_oauth_router",
]


class AuthorizationResponse(BaseModel):
    """
    A dónde mandar al usuario.

    Se devuelve la URL en JSON en vez de responder un 302 porque el llamador suele ser una SPA
    con `fetch`, y un 302 seguido automáticamente por `fetch` termina con la página de
    consentimiento del proveedor cargada en un contexto que no puede navegar. Quien quiera el
    redirect pasa `?redirect=1`.
    """

    url: str
    state: str


class LinkedProviders(BaseModel):
    providers: list[str]


def build_oauth_router(
    *,
    prefix: str = "/auth/oauth",
    tags: t.Sequence[str] = ("auth", "oauth"),
    rate_limit: tuple[int, int] | None = (20, 300),
) -> APIRouter:
    """
    Construye el router del plugin.

    Args:
        prefix: Prefijo de las rutas.
        tags: Tags de OpenAPI.
        rate_limit: `(intentos, ventana)` para `start` y `callback`. Alto a propósito: son rutas
            de navegación normal, y un límite bajo cortaría a los usuarios detrás de un NAT
            compartido. Lo que las protege de verdad es el `state`.

    Uso::

        from hexcore.darwin.plugins.oauth.router import build_oauth_router

        app = create_app(routers=[build_identity_router(), build_oauth_router()])
    """
    from hexcore.darwin.infrastructure.api.dependencies import provide_auth

    router = APIRouter(prefix=prefix, tags=list(tags))
    limite = _rate_limit(rate_limit)

    @router.get("/providers")
    async def providers() -> LinkedProviders:
        """Los proveedores configurados. Es lo que la interfaz necesita para dibujar botones."""
        from hexcore.darwin.plugins.oauth import get_oauth_service

        return LinkedProviders(providers=list(get_oauth_service().provider_ids))

    @router.get("/{provider}/start", dependencies=limite)
    async def start(
        provider: str,
        redirect_uri: str = Query(..., description="A dónde vuelve el usuario."),
        redirect: bool = Query(False, description="Responder 302 en vez de JSON."),
    ) -> Response:
        """
        Inicia el flujo: emite el `state`, guarda el verificador de PKCE y arma la URL.

        **Pública.** Quien la usa todavía no tiene sesión — es el login.
        """
        from hexcore.darwin.plugins.oauth import get_oauth_service

        autorizacion = await get_oauth_service().start(
            provider, redirect_uri=redirect_uri
        )
        if redirect:
            from fastapi.responses import RedirectResponse

            return RedirectResponse(autorizacion.url, status_code=302)
        return JSONResponse(
            AuthorizationResponse(
                url=autorizacion.url, state=autorizacion.state
            ).model_dump()
        )

    @router.get("/{provider}/callback", dependencies=limite)
    async def callback(
        provider: str,
        request: Request,
        code: str = Query(...),
        state: str = Query(...),
        redirect_uri: str = Query(...),
        transport: t.Any = Depends(_resolve_transport),
    ) -> Response:
        """
        El callback: canjea el código y abre la sesión.

        Reusa `emit_tokens` y `session_response_body` del router de identidad, así que el
        transporte dual, los atributos de la cookie y el valor anti-CSRF salen idénticos a un
        sign-in normal. Ver el docstring del módulo sobre por qué no redirige.
        """
        from hexcore.darwin.application.container import get_identity_container
        from hexcore.darwin.infrastructure.api.routers import (
            emit_tokens,
            session_response_body,
        )
        from hexcore.darwin.plugins.oauth import get_oauth_service

        entrada = await get_oauth_service().callback(
            provider,
            code=code,
            state=state,
            redirect_uri=redirect_uri,
            transport=transport.name,
            ip_address=_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )

        cuerpo = session_response_body(entrada.tokens, transport).model_dump(
            exclude_none=True
        )
        cuerpo["created"] = entrada.created

        contenedor = get_identity_container()
        respuesta = JSONResponse(cuerpo)
        emit_tokens(respuesta, entrada.tokens, transport, contenedor.config)
        return respuesta

    @router.get("/{provider}/link", dependencies=limite)
    async def link(
        provider: str,
        redirect_uri: str = Query(...),
        auth: t.Any = Depends(provide_auth),
    ) -> AuthorizationResponse:
        """
        Inicia una **vinculación**: el mismo flujo, atado al actor de la sesión.

        A qué usuario se vincula se fija acá, en el `state`, y no se lee del callback: el
        callback lo controla en parte quien maneja el navegador, y aceptarlo de ahí dejaría
        vincular una identidad propia a la cuenta de otro.

        **No se permite estando impersonado**: vincular un proveedor propio a la cuenta de la
        persona que estás impersonando es tomarle la cuenta.
        """
        from hexcore.darwin.domain.exceptions import ImpersonationNotPermittedError
        from hexcore.darwin.plugins.oauth import get_oauth_service

        if auth.is_impersonating:
            raise ImpersonationNotPermittedError(
                "No se puede vincular un proveedor a la cuenta de otra persona mientras la "
                "impersonás."
            )

        autorizacion = await get_oauth_service().start(
            provider, redirect_uri=redirect_uri, link_user_id=auth.actor_id
        )
        return AuthorizationResponse(
            url=autorizacion.url, state=autorizacion.state
        )

    @router.get("/linked")
    async def linked(auth: t.Any = Depends(provide_auth)) -> LinkedProviders:
        """Los proveedores vinculados al actor."""
        from hexcore.darwin.plugins.oauth import get_oauth_service

        return LinkedProviders(
            providers=await get_oauth_service().list_linked(auth.actor_id)
        )

    @router.delete("/{provider}")
    async def unlink(
        provider: str, auth: t.Any = Depends(provide_auth)
    ) -> dict[str, bool]:
        """
        Desvincula un proveedor del actor.

        Se niega a dejar la cuenta sin ningún método de acceso — ver el docstring del servicio.
        """
        from hexcore.darwin.domain.exceptions import ImpersonationNotPermittedError
        from hexcore.darwin.plugins.oauth import get_oauth_service

        if auth.is_impersonating:
            raise ImpersonationNotPermittedError(
                "No se puede desvincular un proveedor de la cuenta de otra persona mientras la "
                "impersonás."
            )

        await get_oauth_service().unlink(user_id=auth.actor_id, provider_id=provider)
        return {"unlinked": True}

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
                namespace="hexcore:darwin:oauth",
            )
        )
    ]


def _ip(request: Request) -> str | None:
    cliente = request.client
    return cliente.host if cliente else None
