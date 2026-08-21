"""
El router de `magic_link`. Requiere `[api]`.

Dos rutas, y las dos son públicas — no pueden no serlo: quien pide un magic link es justamente
quien no puede autenticarse. Eso hace que el rate limit y la respuesta uniforme no sean
opcionales.
"""
# pyright: reportUnusedFunction=false
#
# En un módulo de router ninguna función se llama por nombre: a todas las registra su
# decorador. Misma razón que en `infrastructure/api/routers.py`.
from __future__ import annotations

import typing as t
from datetime import timedelta

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

__all__ = ["RequestMagicLinkBody", "ConsumeMagicLinkBody", "build_magic_link_router"]


class RequestMagicLinkBody(BaseModel):
    email: str


class ConsumeMagicLinkBody(BaseModel):
    email: str
    token: str


def build_magic_link_router(
    *,
    prefix: str = "/auth/magic-link",
    ttl: timedelta | None = None,
    rate_limit: tuple[int, int] | None = (3, 900),
    tags: t.Sequence[str] = ("auth", "magic-link"),
) -> APIRouter:
    """
    Construye el router del plugin.

    Args:
        prefix: Prefijo de las rutas.
        ttl: Vida del link. Por defecto, `DEFAULT_TTL` (15 min).
        rate_limit: `(intentos, ventana)` para `POST /request`. **No lo apagues sin pensarlo**:
            la ruta es pública y manda mails, así que sin límite es un amplificador de mail
            gratuito contra terceros — el destinatario ni pidió estar en tu sistema.
        tags: Tags de OpenAPI.

    Uso::

        app = create_app(routers=[build_identity_router(), build_magic_link_router()])
    """
    from hexcore.darwin.plugins.magic_link import DEFAULT_TTL

    router = APIRouter(prefix=prefix, tags=list(tags))
    vida = ttl or DEFAULT_TTL
    limite = _rate_limit(rate_limit)

    @router.post("/request", dependencies=limite)
    async def request(payload: RequestMagicLinkBody) -> dict[str, t.Any]:
        """
        Pide un magic link. **Responde igual exista o no la cuenta.**

        Devuelve el token en el cuerpo cuando existe, por la misma razón que
        `/auth/sign-up` devuelve el código de verificación: el framework no manda mails.
        ⚠️ **En producción, no devuelvas este valor**: mandalo por mail y respondé sólo
        `{"sent": true}`. Suscribite al evento o envolvé esta ruta.
        """
        from hexcore.darwin.plugins.magic_link.commands import request_magic_link

        emitido = await request_magic_link(email=payload.email, ttl=vida)

        # La forma de la respuesta es idéntica en los dos casos. Que el token venga o no es lo
        # único que cambia, y en producción no se devuelve.
        cuerpo: dict[str, t.Any] = {"sent": True}
        if emitido.token is not None:
            cuerpo["token"] = emitido.token
        return cuerpo

    @router.post("/consume")
    async def consume(
        payload: ConsumeMagicLinkBody,
        request: Request,
        transport: t.Any = Depends(_resolve_transport),
    ) -> Response:
        """
        Canjea el link y abre la sesión, con el mismo transporte dual que `/auth/sign-in`.

        Reusa `emit_tokens` del router de identidad: si el plugin escribiera las cookies por su
        cuenta, tendría que replicar los atributos seguros y el valor anti-CSRF — y la copia
        que se olvida de uno es la que se explota.
        """
        from hexcore.darwin.application.container import get_identity_container
        from hexcore.darwin.infrastructure.api.routers import (
            emit_tokens,
            session_response_body,
        )
        from hexcore.darwin.plugins.magic_link.commands import consume_magic_link

        resultado = await consume_magic_link(
            email=payload.email,
            token=payload.token,
            transport=transport.name,
            ip_address=_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )

        contenedor = get_identity_container()
        respuesta = JSONResponse(
            session_response_body(resultado.tokens, transport).model_dump(
                exclude_none=True
            )
        )
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
                # `deny` como en el router de identidad: un backend caído no debería
                # convertirse en un relay de mail abierto.
                on_backend_error="deny",
                namespace="hexcore:darwin:magic-link",
            )
        )
    ]


def _ip(request: Request) -> str | None:
    cliente = request.client
    return cliente.host if cliente else None
