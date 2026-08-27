"""
Dependencias de FastAPI para exigir autenticación y autorización en una ruta.

La decisión de exigir credencial es **de la ruta**, no del middleware. El middleware publica
el contexto y deja pasar; acá es donde una ruta dice "esto necesita un actor" y otra dice "y
además estos permisos". Al revés —un middleware que rechaza— habría que mantener la lista de
rutas públicas en dos lugares, y esa lista se desincroniza siempre.

Los 401 que salen de acá llevan `WWW-Authenticate`, que RFC 6750 §3 exige y que el
`headers_for` de `register_exception_handlers` hace posible (Fase 0).

Requiere el extra `[api]`.
"""
from __future__ import annotations

import typing as t

from fastapi import Request

from hexcore.darwin.domain.context import AuthContext
from hexcore.darwin.domain.exceptions import (
    InsufficientScopeError,
    UnauthenticatedError,
)
from hexcore.darwin.infrastructure.api.middlewares import auth_from_request

__all__ = [
    "WWW_AUTHENTICATE",
    "provide_auth",
    "provide_optional_auth",
    "require_authenticated",
    "require_scopes",
    "require_roles",
    "require_not_impersonated",
    "identity_exception_headers",
]

#: El valor de `WWW-Authenticate` de un 401. RFC 6750 §3.
#:
#: `Bearer` incluso cuando la credencial vino por cookie: el header describe **cómo
#: autenticarse**, y el esquema estándar que el cliente puede usar en los dos transportes es
#: Bearer. No existe un esquema "Cookie" registrado.
WWW_AUTHENTICATE = 'Bearer realm="hexcore", error="invalid_token"'


def provide_optional_auth(request: Request) -> "AuthContext[t.Any] | None":
    """
    El contexto del request, o `None` si es anónimo. **No lanza.**

    Para rutas que sirven a los dos: una home que muestra el nombre si hay sesión y un
    botón de login si no.
    """
    return auth_from_request(request)


def provide_auth(request: Request) -> "AuthContext[t.Any]":
    """
    El contexto del request. Lanza si es anónimo.

    Raises:
        UnauthenticatedError: mapeada a 401 con `WWW-Authenticate`.

    Uso::

        @router.get("/me")
        async def me(auth: AuthContext = Depends(provide_auth)):
            return {"user_id": str(auth.subject_id)}
    """
    contexto = auth_from_request(request)
    if contexto is None:
        raise UnauthenticatedError(
            "Esta ruta necesita autenticación y la petición no trae una credencial válida."
        )
    return contexto


def require_authenticated() -> t.Any:
    """
    Dependencia sin valor, para poner en `dependencies=[...]`.

    Uso::

        @router.post("/facturas", dependencies=[Depends(require_authenticated())])
        async def crear(): ...
    """

    async def dependencia(request: Request) -> None:
        provide_auth(request)

    return dependencia


def require_scopes(*scopes: str) -> t.Any:
    """
    Exige que el **actor** tenga todos los permisos.

    El actor y no el sujeto: en una impersonación, lo que se puede hacer lo determina quien
    ejecuta. Al revés, impersonar a un admin sería una escalación de privilegios en un solo
    paso.

    Raises:
        UnauthenticatedError: 401, si no hay credencial.
        InsufficientScopeError: 403, con **todos** los permisos que faltan, no el primero.

    Uso::

        @router.post(
            "/transferencias",
            dependencies=[Depends(require_scopes("dinero.mover"))],
        )
        async def transferir(): ...
    """

    async def dependencia(request: Request) -> None:
        provide_auth(request).require_scopes(*scopes)

    return dependencia


def require_roles(*roles: str) -> t.Any:
    """
    Exige que el actor tenga **alguno** de los roles.

    `alguno` y no `todos`, al revés que `require_scopes`, y la asimetría es a propósito: los
    roles son alternativas ("admin **o** soporte pueden ver esto") y los permisos son
    requisitos acumulativos. Exigir todos los roles no expresa ningún caso real.

    Raises:
        UnauthenticatedError: 401.
        InsufficientScopeError: 403.
    """

    async def dependencia(request: Request) -> None:
        contexto = provide_auth(request)
        if not any(contexto.has_role(rol) for rol in roles):
            raise InsufficientScopeError(
                roles,
                f"Esta ruta necesita alguno de estos roles: {', '.join(sorted(roles))}.",
            )

    return dependencia


def require_not_impersonated(operation: str) -> t.Any:
    """
    Rechaza si la sesión es impersonada.

    Para lo que sólo el dueño real puede hacer: cambiar la contraseña, refrescar la sesión,
    dar de alta un segundo factor, o impersonar a un tercero — que sería impersonación en
    cadena y rompería la cadena de custodia de la auditoría.

    Raises:
        ImpersonationNotPermittedError: 403.
    """

    async def dependencia(request: Request) -> None:
        provide_auth(request).assert_not_impersonating(operation)

    return dependencia


def identity_exception_headers(exc: Exception) -> t.Mapping[str, str]:
    """
    Los headers que las excepciones de identidad exigen por especificación.

    Se pasa a `create_app(exception_headers=...)`, y `create_app` lo hace solo cuando
    `AppFeatures.auth_context` está prendido.

    Un 401 sin `WWW-Authenticate` viola RFC 6750 §3, y hasta la Fase 0 el `_build_handler` de
    HexCore no podía emitir headers en absoluto.
    """
    from hexcore.darwin.domain.exceptions import AuthenticationError

    if isinstance(exc, AuthenticationError):
        return {"WWW-Authenticate": WWW_AUTHENTICATE}
    return {}
