"""
Mapeo de excepciones de dominio a respuestas HTTP.

Cada app escribe los mismos cuatro `@app.exception_handler` para traducir sus
excepciones a 404/409/422/500. HexCore ya define `hexcore.domain.exceptions` y las de
CQRS, así que tiene la información para hacerlo por defecto.
"""
from __future__ import annotations

import logging
import typing as t

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from hexcore.domain.cqrs.exceptions import (
    DeserializationError,
    DuplicateHandlerError,
    HandlerNotFoundError,
)
from hexcore.domain.exceptions import InactiveEntityException

logger = logging.getLogger("hexcore.api.exceptions")

__all__ = [
    "DEFAULT_EXCEPTION_STATUS_MAP",
    "register_exception_handlers",
]


# `ValueError` → 422 porque es lo que lanza la validación de campos de las queries.
# Hasta ahora sólo se capturaba **dentro** de `build_query_endpoint`, así que una query
# construida a mano devolvía 500.
DEFAULT_EXCEPTION_STATUS_MAP: dict[type[Exception], int] = {
    InactiveEntityException: 409,
    DeserializationError: 400,
    HandlerNotFoundError: 501,
    DuplicateHandlerError: 500,
    ValueError: 422,
}

DetailFactory = t.Callable[[Exception], str]


def register_exception_handlers(
    app: FastAPI,
    *,
    mapping: dict[type[Exception], int] | None = None,
    include_detail: bool | DetailFactory = True,
) -> None:
    """
    Registra handlers que traducen excepciones a respuestas JSON.

    Args:
        app: La aplicación FastAPI.
        mapping: Excepciones extra o overrides. Se **fusiona** con
            `DEFAULT_EXCEPTION_STATUS_MAP`; lo que pases gana.
        include_detail: Qué poner en el campo ``detail`` de la respuesta.
            ``True`` → ``str(exc)``. ``False`` → un mensaje genérico por status, para no
            filtrar internals en una API pública. Un callable → lo que devuelva.

    El cuerpo es siempre ``{"detail": ..., "error": "<NombreDeLaExcepción>"}``, para que
    el cliente pueda distinguir el caso sin parsear el texto.

    Uso::

        register_exception_handlers(app, mapping={MiNotFound: 404})
    """
    resolved = {**DEFAULT_EXCEPTION_STATUS_MAP, **(mapping or {})}

    # De más específico a más genérico. Starlette resuelve por MRO de la excepción
    # lanzada, pero registrar en este orden mantiene el comportamiento estable si dos
    # entradas están emparentadas (p. ej. `ValueError` y una subclase propia).
    for exc_type in sorted(resolved, key=_specificity, reverse=True):
        app.add_exception_handler(
            exc_type,
            _build_handler(exc_type, resolved[exc_type], include_detail),
        )


def _specificity(exc_type: type[Exception]) -> int:
    """Cuántos niveles de herencia tiene: más profundo = más específico."""
    return len(exc_type.__mro__)


def _build_handler(
    exc_type: type[Exception],
    status_code: int,
    include_detail: bool | DetailFactory,
) -> t.Callable[[Request, Exception], t.Awaitable[JSONResponse]]:
    async def handler(request: Request, exc: Exception) -> JSONResponse:
        if status_code >= 500:
            logger.exception(
                "%s %s → %s (%s)",
                request.method,
                request.url.path,
                status_code,
                type(exc).__name__,
            )
        else:
            logger.info(
                "%s %s → %s (%s: %s)",
                request.method,
                request.url.path,
                status_code,
                type(exc).__name__,
                exc,
            )

        return JSONResponse(
            status_code=status_code,
            content={
                "detail": _detail_for(exc, status_code, include_detail),
                "error": type(exc).__name__,
            },
        )

    handler.__name__ = f"handle_{exc_type.__name__}"
    return handler


def _detail_for(
    exc: Exception,
    status_code: int,
    include_detail: bool | DetailFactory,
) -> str:
    if callable(include_detail):
        return include_detail(exc)
    if include_detail:
        return str(exc)
    return "Internal server error" if status_code >= 500 else "Request rejected"
