"""
`authorize_command` y `AuthorizationMiddleware`: el mismo contrato de autorización, para el
bus de comandos.

`require_permission` (borde HTTP) protege una ruta; esto protege el **caso de uso**, así que
un comando despachado desde un worker, un cron o un test de handler queda cubierto igual que
uno que llegó por HTTP. Es la misma razón por la que Darwin tiene `HookMiddleware` además del
router que llama a los servicios directo: un solo punto de enganche no alcanza cuando hay dos
caminos de entrada.

`authorize_command` estampa metadata en la clase, igual que `identity_action` — un decorador
que degradara el tipo del comando decorado no sería aceptable en una API pública, así que
devuelve la misma clase.
"""
from __future__ import annotations

import typing as t

from hexcore.darwin.domain.authorization import AccessRequest, ResourceRef
from hexcore.darwin.domain.context import require_auth
from hexcore.darwin.domain.exceptions import AccessDeniedError
from hexcore.domain.cqrs.middleware import AbstractMiddleware, NextHandler

if t.TYPE_CHECKING:
    from hexcore.darwin.application.authorization import AuthorizationEngine

__all__ = ["AuthorizationMiddleware", "authorize_command"]

#: Dónde `authorize_command` estampa la acción. Privado: se lee por `getattr`, nunca a mano.
_ACTION_ATTR = "__darwin_authz_action__"
#: Dónde estampa el `resource_from`, si lo hay.
_RESOURCE_FROM_ATTR = "__darwin_authz_resource_from__"

ResourceFrom = t.Callable[[t.Any], "ResourceRef | None"]

_TClase = t.TypeVar("_TClase", bound=type)


def authorize_command(
    action: str, *, resource_from: ResourceFrom | None = None
) -> t.Callable[[_TClase], _TClase]:
    """
    Marca un comando o query con la acción que `AuthorizationMiddleware` exige antes del
    handler.

    Sin este decorador, un mensaje no pasa por el motor de autorización — `AuthorizationMiddleware`
    deja pasar todo lo que no está marcado, igual que `HookMiddleware` no hace nada con una
    acción sin hooks. Así un comando interno (el reaper de sesiones, un seed) no necesita
    declarar nada para seguir funcionando.

    Args:
        resource_from: Si la decisión depende del recurso, un callable **síncrono** que arma
            el `ResourceRef` a partir del propio mensaje —los datos que el comando ya trae en
            sus campos—, no de una consulta a la base. Para completar atributos que el mensaje
            no tiene, la decisión se apoya en el PIP de DRBAC al evaluar, no acá.

    Uso::

        @authorize_command(
            "invoice.approve",
            resource_from=lambda c: ResourceRef(type="invoice", id=str(c.invoice_id)),
        )
        class AprobarFactura(Command):
            invoice_id: UUID
    """

    def decorador(clase: _TClase) -> _TClase:
        setattr(clase, _ACTION_ATTR, action)
        setattr(clase, _RESOURCE_FROM_ATTR, resource_from)
        return clase

    return decorador


def _tipo_de(message: t.Any) -> type:
    """Mismo criterio que `action_of`: acepta una instancia o la clase misma."""
    return message if isinstance(message, type) else type(message)


def _accion_de(message: t.Any) -> str | None:
    return getattr(_tipo_de(message), _ACTION_ATTR, None)


def _resource_from_de(message: t.Any) -> ResourceFrom | None:
    return getattr(_tipo_de(message), _RESOURCE_FROM_ATTR, None)


class AuthorizationMiddleware(AbstractMiddleware):
    """
    Exige la acción de `authorize_command` antes de correr el handler.

    Un mensaje sin `@authorize_command` pasa de largo, así que este middleware se puede poner
    en el pipeline de toda la app sin que cada comando existente tenga que declararse
    explícitamente autorizado. Nada nuevo: es la misma forma que `HookMiddleware`.

    Uso::

        pipeline = MiddlewarePipeline([AuthorizationMiddleware(contenedor.authorizer())])
        bus = InMemoryCommandBus(registry=registry, pipeline=pipeline)
    """

    def __init__(self, engine: "AuthorizationEngine") -> None:
        self._engine = engine

    async def handle(self, message: t.Any, next_handler: NextHandler) -> t.Any:
        action = _accion_de(message)
        if action is None:
            return await next_handler(message)

        resource_from = _resource_from_de(message)
        resource = resource_from(message) if resource_from is not None else None

        context = require_auth()
        decision = await self._engine.decide(
            AccessRequest(context=context, action=action, resource=resource)
        )
        if not decision.allowed:
            raise AccessDeniedError(action)

        return await next_handler(message)
