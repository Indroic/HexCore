"""
Comandos y handlers de `impersonate`.

Los comandos **no llevan el actor**: lo toman del `AuthContext` ambiental. Es la diferencia
deliberada con un `AuthenticatedCommand` que llevara un `actor_id` en el payload — un campo que el
llamador rellena es un campo que el llamador puede mentir, y acá mentirlo sería impersonar en
nombre de otro.

Que el contexto llegue al worker cuando el comando se encola lo resuelve el sobre firmado de la
Fase 6: viaja en `__meta__`, atado al `command_id` y al tipo de mensaje, y el worker **re-valida la
fila de `session`** en vez de confiar en el `exp`.
"""
from __future__ import annotations

import typing as t
from uuid import UUID

from hexcore.darwin.domain.plugins import identity_action
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.handlers import AbstractCommandHandler

if t.TYPE_CHECKING:
    from hexcore.darwin.plugins.impersonate.service import Impersonated

__all__ = [
    "StartImpersonation",
    "StopImpersonation",
    "StartImpersonationHandler",
    "StopImpersonationHandler",
]


@identity_action("impersonation.start")
class StartImpersonation(Command):
    """
    Empieza una impersonación.

    No hay `actor_id`: sale del contexto ambiental. Ver el docstring del módulo.
    """

    subject_id: UUID
    reason: str
    transport: str = "cookie"
    ip_address: str | None = None
    user_agent: str | None = None


@identity_action("impersonation.stop")
class StopImpersonation(Command):
    """Termina la impersonación de la sesión actual."""


class StartImpersonationHandler(
    AbstractCommandHandler[StartImpersonation, "Impersonated"]
):
    async def handle(self, command: StartImpersonation) -> "Impersonated":
        from hexcore.darwin.domain.context import current_auth
        from hexcore.darwin.plugins.impersonate import get_impersonation_service

        return await get_impersonation_service().start(
            context=current_auth(),
            subject_id=command.subject_id,
            reason=command.reason,
            transport=t.cast(t.Any, command.transport),
            ip_address=command.ip_address,
            user_agent=command.user_agent,
        )


class StopImpersonationHandler(AbstractCommandHandler[StopImpersonation, None]):
    async def handle(self, command: StopImpersonation) -> None:
        from hexcore.darwin.domain.context import current_auth
        from hexcore.darwin.plugins.impersonate import get_impersonation_service

        await get_impersonation_service().stop(context=current_auth())
        return None
