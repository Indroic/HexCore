"""
El router de `two_factor`. Requiere `[api]`.

Seis rutas, y la división entre públicas y protegidas es la parte que importa:

- `POST /auth/2fa/challenge` es **pública** — no puede no serlo: quien la usa está a mitad de un
  login y todavía no tiene sesión. Su protección es el desafío de un solo uso más el rate limit.
- Todo el resto exige sesión: inscribir, confirmar, desactivar y consultar el estado son
  operaciones sobre la propia cuenta.

`GET /auth/2fa` devuelve el estado sin exigir código: es lo que la interfaz necesita para saber
qué mostrar, y no revela nada que el dueño de la sesión no pueda ver de todas formas.
"""
# pyright: reportUnusedFunction=false
#
# En un módulo de router ninguna función se llama por nombre: a todas las registra su decorador.
from __future__ import annotations

import typing as t

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

__all__ = [
    "ConfirmBody",
    "DisableBody",
    "ChallengeBody",
    "EnrollmentResponse",
    "TwoFactorStatus",
    "build_two_factor_router",
]


class ConfirmBody(BaseModel):
    code: str


class DisableBody(BaseModel):
    code: str


class ChallengeBody(BaseModel):
    challenge: str
    code: str


class EnrollmentResponse(BaseModel):
    """
    La respuesta de inscribir.

    ⚠️ Lleva el secreto en claro: es la única vez que sale de la aplicación, porque el usuario
    tiene que escanearlo. No lo loguees ni lo guardes del lado del cliente más allá de mostrar
    el QR.
    """

    secret: str
    uri: str
    confirmed: bool


class TwoFactorStatus(BaseModel):
    enrolled: bool
    confirmed: bool


def build_two_factor_router(
    *,
    prefix: str = "/auth/2fa",
    tags: t.Sequence[str] = ("auth", "2fa"),
    rate_limit: tuple[int, int] | None = (10, 300),
) -> APIRouter:
    """
    Construye el router del plugin.

    Args:
        prefix: Prefijo de las rutas.
        tags: Tags de OpenAPI.
        rate_limit: `(intentos, ventana)` para el canje del desafío. **No lo apagues sin
            pensarlo**: es la ruta donde se prueban códigos de 6 dígitos, y el techo por fila
            sólo protege a un usuario ya inscripto — el límite por IP es lo que corta a quien
            rota entre cuentas.

    Uso::

        from hexcore.darwin.plugins.two_factor.router import build_two_factor_router

        app = create_app(routers=[build_identity_router(), build_two_factor_router()])
    """
    from hexcore.darwin.infrastructure.api.dependencies import provide_auth

    router = APIRouter(prefix=prefix, tags=list(tags))
    limite = _rate_limit(rate_limit)

    @router.get("")
    async def estado(auth: t.Any = Depends(provide_auth)) -> TwoFactorStatus:
        """El estado del segundo factor del actor de la sesión."""
        from hexcore.darwin.plugins.two_factor import get_two_factor_service

        inscripto, confirmado = await get_two_factor_service().describe(auth.actor_id)
        return TwoFactorStatus(enrolled=inscripto, confirmed=confirmado)

    @router.post("/enroll", status_code=201)
    async def enroll(auth: t.Any = Depends(provide_auth)) -> EnrollmentResponse:
        """
        Inscribe un factor nuevo, **sin activarlo**.

        Inscribe siempre al **actor**, nunca a un `user_id` del cuerpo: aceptarlo del cliente
        dejaría que cualquiera con sesión le inscriba un segundo factor a otro y le tome la
        cuenta. Es la misma razón por la que `/auth/me` mira el contexto y no un parámetro.
        """
        from hexcore.darwin.plugins.two_factor import get_two_factor_service

        inscripcion = await get_two_factor_service().enroll(
            user_id=auth.actor_id, account=_cuenta(auth)
        )
        return EnrollmentResponse(
            secret=inscripcion.secret,
            uri=inscripcion.uri,
            confirmed=inscripcion.confirmed,
        )

    @router.post("/confirm")
    async def confirm(
        payload: ConfirmBody, auth: t.Any = Depends(provide_auth)
    ) -> dict[str, bool]:
        """Activa el factor del actor."""
        from hexcore.darwin.plugins.two_factor import get_two_factor_service

        await get_two_factor_service().confirm(
            user_id=auth.actor_id, code=payload.code
        )
        return {"confirmed": True}

    @router.post("/disable")
    async def disable(
        payload: DisableBody, auth: t.Any = Depends(provide_auth)
    ) -> dict[str, bool]:
        """
        Desactiva el factor del actor, exigiendo un código válido.

        **No se permite estando impersonado.** Un administrador que entra como el usuario no
        tiene por qué poder bajarle el segundo factor: sería la escalada más barata del sistema.
        """
        from hexcore.darwin.domain.exceptions import ImpersonationNotPermittedError
        from hexcore.darwin.plugins.two_factor import get_two_factor_service

        if auth.is_impersonating:
            raise ImpersonationNotPermittedError(
                "No se puede desactivar el segundo factor de otra persona mientras la "
                "impersonás."
            )

        await get_two_factor_service().disable(
            user_id=auth.actor_id, code=payload.code
        )
        return {"disabled": True}

    @router.post("/challenge", dependencies=limite)
    async def challenge(
        payload: ChallengeBody,
        request: Request,
        transport: t.Any = Depends(_resolve_transport),
    ) -> Response:
        """
        El segundo paso del login: canjea el desafío con el código y abre la sesión.

        **Pública**, y no puede no serlo: quien llega acá está a mitad de un login y todavía no
        tiene sesión. Reusa `emit_tokens` y `session_response_body` del router de identidad, así
        que el transporte dual, los atributos de la cookie y el valor anti-CSRF salen idénticos a
        un sign-in normal — replicarlos sería la copia que se olvida de uno.
        """
        from hexcore.darwin.application.container import get_identity_container
        from hexcore.darwin.infrastructure.api.routers import (
            emit_tokens,
            session_response_body,
        )
        from hexcore.darwin.plugins.two_factor import get_two_factor_service

        _, _, par = await get_two_factor_service().complete_sign_in(
            challenge=payload.challenge,
            code=payload.code,
            transport=transport.name,
            ip_address=_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )

        contenedor = get_identity_container()
        respuesta = JSONResponse(
            session_response_body(par, transport).model_dump(exclude_none=True)
        )
        emit_tokens(respuesta, par, transport, contenedor.config)
        return respuesta

    return router


def _cuenta(auth: t.Any) -> str:
    """
    Qué se muestra como cuenta en la app autenticadora.

    El `sub` y no el mail: el mail no viaja en el token —es PII— y traerlo de la base para armar
    un QR sería una consulta por inscripción para un dato cosmético. Quien quiera el mail ahí
    puede envolver esta ruta.
    """
    return str(auth.actor_id)


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
                # `deny` como en el resto de las rutas de auth: un backend de cache caído no
                # debería convertirse en fuerza bruta ilimitada sobre códigos de 6 dígitos.
                on_backend_error="deny",
                namespace="hexcore:darwin:2fa",
            )
        )
    ]


def _ip(request: Request) -> str | None:
    cliente = request.client
    return cliente.host if cliente else None
