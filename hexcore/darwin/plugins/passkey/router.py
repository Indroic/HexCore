"""
El router de `passkey`. Requiere `[api]`.

Seis rutas. La división:

- `POST /auth/passkey/authenticate/options` y `.../authenticate` son **públicas**: son el login, y
  quien las usa no tiene sesión. Su protección es el desafío de un solo uso más la firma.
- Registrar, listar y borrar exigen sesión: son operaciones sobre la propia cuenta.

⚠️ **Las opciones de registro se piden con sesión y el `user_id` sale del contexto**, nunca del
cuerpo. Aceptarlo del cliente dejaría registrar una credencial propia en la cuenta de otro — toma
de cuenta directa, en un endpoint que parece administrativo.
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

__all__ = [
    "FinishRegistrationBody",
    "AuthenticationOptionsBody",
    "FinishAuthenticationBody",
    "PasskeySummary",
    "build_passkey_router",
]


class FinishRegistrationBody(BaseModel):
    """
    El cuerpo de `POST /auth/passkey/register`.

    No lleva `user_id`: sale del desafío. Ver la advertencia del docstring del módulo.
    """

    credential: dict[str, t.Any]
    name: str | None = Field(default=None, max_length=128)


class AuthenticationOptionsBody(BaseModel):
    """
    El cuerpo de `POST /auth/passkey/authenticate/options`.

    `email` es opcional: sin él, el flujo es con credenciales descubribles y el navegador ofrece
    lo que tenga. Con él, se limita a las credenciales de esa cuenta — y **no se revela si la
    cuenta existe**: un mail desconocido devuelve opciones sin `allowCredentials`, exactamente
    igual que el flujo sin mail.
    """

    email: str | None = None


class FinishAuthenticationBody(BaseModel):
    credential: dict[str, t.Any]


class PasskeySummary(BaseModel):
    """Lo que se le muestra al usuario en la pantalla de ajustes."""

    id: str
    name: str | None
    aaguid: str | None
    backed_up: bool
    created_at: str | None
    last_used_at: str | None


def build_passkey_router(
    *,
    prefix: str = "/auth/passkey",
    tags: t.Sequence[str] = ("auth", "passkey"),
    rate_limit: tuple[int, int] | None = (20, 300),
) -> APIRouter:
    """
    Construye el router del plugin.

    Args:
        prefix: Prefijo de las rutas.
        tags: Tags de OpenAPI.
        rate_limit: `(intentos, ventana)` para las dos rutas públicas del login. Alto a propósito:
            un usuario que cancela el diálogo del navegador y reintenta consume intentos, y
            cortarlo en cinco sería frustrante. Lo que protege de verdad es la firma.

    Uso::

        from hexcore.darwin.plugins.passkey.router import build_passkey_router

        app = create_app(routers=[build_identity_router(), build_passkey_router()])
    """
    from hexcore.darwin.infrastructure.api.dependencies import provide_auth

    router = APIRouter(prefix=prefix, tags=list(tags))
    limite = _rate_limit(rate_limit)

    # ── Registro (con sesión) ─────────────────────────────────────────────────
    @router.post("/register/options")
    async def register_options(
        auth: t.Any = Depends(provide_auth),
    ) -> dict[str, t.Any]:
        """
        Las opciones para `navigator.credentials.create()`.

        Registra siempre al **actor** del contexto. Ver la advertencia del docstring del módulo.
        """
        from hexcore.darwin.plugins.passkey import get_passkey_service

        opciones = await get_passkey_service().start_registration(
            user_id=auth.actor_id, user_name=str(auth.actor_id)
        )
        return opciones.options

    @router.post("/register", status_code=201)
    async def register(
        payload: FinishRegistrationBody, auth: t.Any = Depends(provide_auth)
    ) -> PasskeySummary:
        """
        Guarda la credencial.

        **No se permite estando impersonado**: registrar una credencial propia en la cuenta de la
        persona que estás impersonando es tomarle la cuenta, y de forma permanente.
        """
        from hexcore.darwin.domain.exceptions import ImpersonationNotPermittedError
        from hexcore.darwin.plugins.passkey import get_passkey_service

        if auth.is_impersonating:
            raise ImpersonationNotPermittedError(
                "No se puede registrar una passkey en la cuenta de otra persona mientras la "
                "impersonás."
            )

        guardada = await get_passkey_service().finish_registration(
            credential=payload.credential, name=payload.name
        )
        return _resumen(guardada)

    # ── Login (público) ───────────────────────────────────────────────────────
    @router.post("/authenticate/options", dependencies=limite)
    async def authenticate_options(
        payload: AuthenticationOptionsBody,
    ) -> dict[str, t.Any]:
        """
        Las opciones para `navigator.credentials.get()`.

        **No revela si la cuenta existe.** Un mail desconocido devuelve opciones sin
        `allowCredentials`, que es exactamente la misma forma que el flujo sin mail: el cliente no
        puede distinguir "no existe" de "usá una credencial descubrible".
        """
        from hexcore.darwin.application.container import get_identity_container
        from hexcore.darwin.plugins.passkey import get_passkey_service

        user_id: UUID | None = None
        if payload.email:
            from hexcore.darwin.domain.value_objects import Email

            usuario = await get_identity_container().users().get_by_email(
                Email(value=payload.email).value
            )
            user_id = usuario.id if usuario else None

        opciones = await get_passkey_service().start_authentication(user_id=user_id)
        return opciones.options

    @router.post("/authenticate", dependencies=limite)
    async def authenticate(
        payload: FinishAuthenticationBody,
        request: Request,
        transport: t.Any = Depends(_resolve_transport),
    ) -> Response:
        """
        Verifica la aserción y abre la sesión.

        Reusa `emit_tokens` y `session_response_body` del router de identidad: una sesión abierta
        con passkey es una sesión normal, y armar su respuesta a mano sería la copia que se olvida
        de un atributo de cookie.
        """
        from hexcore.darwin.application.container import get_identity_container
        from hexcore.darwin.infrastructure.api.routers import (
            emit_tokens,
            session_response_body,
        )
        from hexcore.darwin.plugins.passkey import get_passkey_service

        entrada = await get_passkey_service().finish_authentication(
            credential=payload.credential,
            transport=transport.name,
            ip_address=_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )

        contenedor = get_identity_container()
        respuesta = JSONResponse(
            session_response_body(entrada.tokens, transport).model_dump(
                exclude_none=True
            )
        )
        emit_tokens(respuesta, entrada.tokens, transport, contenedor.config)
        return respuesta

    # ── Ciclo de vida (con sesión) ────────────────────────────────────────────
    @router.get("")
    async def listar(auth: t.Any = Depends(provide_auth)) -> list[PasskeySummary]:
        """Las credenciales del actor."""
        from hexcore.darwin.plugins.passkey import get_passkey_service

        credenciales = await get_passkey_service().list_for_user(auth.actor_id)
        return [_resumen(p) for p in credenciales]

    @router.delete("/{passkey_id}")
    async def borrar(
        passkey_id: UUID, auth: t.Any = Depends(provide_auth)
    ) -> dict[str, bool]:
        """
        Borra una credencial del actor.

        Se niega a dejar la cuenta sin ningún método de acceso — ver el docstring del servicio.
        """
        from hexcore.darwin.domain.exceptions import ImpersonationNotPermittedError
        from hexcore.darwin.plugins.passkey import get_passkey_service

        if auth.is_impersonating:
            raise ImpersonationNotPermittedError(
                "No se puede borrar una passkey de otra persona mientras la impersonás."
            )

        await get_passkey_service().delete(
            user_id=auth.actor_id, passkey_id=passkey_id
        )
        return {"deleted": True}

    return router


def _resumen(passkey: t.Any) -> PasskeySummary:
    """
    El resumen que sale al cliente.

    **No lleva `credential_id` ni `public_key`.** No son secretos, pero tampoco le sirven de nada
    a la interfaz, y una respuesta más chica es una superficie más chica.
    """
    return PasskeySummary(
        id=str(passkey.id),
        name=passkey.name,
        aaguid=passkey.aaguid,
        backed_up=passkey.backed_up,
        created_at=passkey.created_at.isoformat() if passkey.created_at else None,
        last_used_at=(
            passkey.last_used_at.isoformat() if passkey.last_used_at else None
        ),
    )


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
                namespace="hexcore:darwin:passkey",
            )
        )
    ]


def _ip(request: Request) -> str | None:
    cliente = request.client
    return cliente.host if cliente else None
