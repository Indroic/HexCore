"""
`require_permission`: la dependencia de FastAPI que pasa por `AuthorizationEngine`.

`require_scopes`/`require_roles` (`dependencies.py`) sólo miran `Principal.scopes`/`roles` del
token. Esto es lo que hace falta cuando la decisión depende del **recurso** —"aprobá esta
factura, salvo que sea la tuya"— y no sólo de qué trae el actor encima: corre todos los
`AuthorizationProvider` cableados (el `ScopeAuthorizationProvider` retrocompatible, y los que
aporten RBAC/DRBAC) y aplica deny-overrides.

Requiere el extra `[api]`.
"""
from __future__ import annotations

import typing as t

from fastapi import Request

from hexcore.darwin.domain.authorization import AccessRequest, ResourceRef
from hexcore.darwin.domain.exceptions import AccessDeniedError
from hexcore.darwin.infrastructure.api.dependencies import provide_auth

__all__ = ["require_permission"]

#: Arma el `ResourceRef` a partir del request — típicamente cargando la fila del path y
#: devolviendo su `id`/`owner_id`/`scope_path`. `None` para acciones sin recurso.
ResourceLoader = t.Callable[[Request], t.Awaitable["ResourceRef | None"]]


def require_permission(action: str, *, resource: ResourceLoader | None = None) -> t.Any:
    """
    Exige que `AuthorizationEngine` autorice `action` para el actor del request.

    Args:
        resource: Un callable async que recibe el `Request` y arma el `ResourceRef` — para
            protecciones que dependen del dueño, el tenant o atributos del recurso.  `None`
            para una acción sobre la colección (`invoice.create`), que no tiene uno.

    Raises:
        UnauthenticatedError: 401, si no hay credencial.
        AccessDeniedError: 403, con `required` = `action`. Sin la razón de la política: un
            403 que la explicara le regalaría a quien está sondeando el sistema un mapa de lo
            que existe adentro.

    Uso::

        @router.post(
            "/invoices/{invoice_id}/approve",
            dependencies=[Depends(require_permission("invoice.approve", resource=cargar_factura))],
        )
        async def aprobar(invoice_id: UUID): ...
    """

    async def dependencia(request: Request) -> None:
        contexto = provide_auth(request)
        resource_ref = await resource(request) if resource is not None else None

        from hexcore.darwin.application.container import get_identity_container

        engine = get_identity_container().authorizer()
        decision = await engine.decide(
            AccessRequest(context=contexto, action=action, resource=resource_ref)
        )
        if not decision.allowed:
            raise AccessDeniedError(action)

    return dependencia
