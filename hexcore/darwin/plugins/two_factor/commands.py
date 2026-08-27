"""
Comandos y handlers de `two_factor`.

Las acciones se declaran con `@identity_action` y no se dejan derivar del nombre de la clase:
son el contrato al que se enganchan los hooks de otros plugins, y derivarlas las ataría al nombre
de la clase — renombrar el comando rompería en silencio los hooks de todo el ecosistema.
"""
from __future__ import annotations

import typing as t
from uuid import UUID

from hexcore.darwin.domain.plugins import identity_action
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.handlers import AbstractCommandHandler

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.entities import IdentitySession, User
    from hexcore.darwin.domain.value_objects import TokenPair
    from hexcore.darwin.plugins.two_factor.domain import TwoFactor
    from hexcore.darwin.plugins.two_factor.service import Enrollment

__all__ = [
    "EnrollTwoFactor",
    "ConfirmTwoFactor",
    "DisableTwoFactor",
    "CompleteTwoFactorSignIn",
    "TwoFactorSignIn",
    "EnrollTwoFactorHandler",
    "ConfirmTwoFactorHandler",
    "DisableTwoFactorHandler",
    "CompleteTwoFactorSignInHandler",
]


# ── Comandos ──────────────────────────────────────────────────────────────────
@identity_action("two_factor.enroll")
class EnrollTwoFactor(Command):
    """Genera un secreto TOTP nuevo, **sin activarlo**."""

    user_id: UUID
    account: str


@identity_action("two_factor.confirm")
class ConfirmTwoFactor(Command):
    """Activa el factor, probando que la app del usuario genera el código correcto."""

    user_id: UUID
    code: str


@identity_action("two_factor.disable")
class DisableTwoFactor(Command):
    """Desactiva el factor. Exige un código válido: ver el docstring del servicio."""

    user_id: UUID
    code: str


@identity_action("two_factor.complete_sign_in")
class CompleteTwoFactorSignIn(Command):
    """El segundo paso del login: canjea el desafío con el código y abre la sesión."""

    challenge: str
    code: str
    transport: str = "cookie"
    ip_address: str | None = None
    user_agent: str | None = None


# ── Resultado ─────────────────────────────────────────────────────────────────
class TwoFactorSignIn(t.NamedTuple):
    user: "User"
    session: "IdentitySession"
    tokens: "TokenPair"


# ── Handlers ──────────────────────────────────────────────────────────────────
class EnrollTwoFactorHandler(AbstractCommandHandler[EnrollTwoFactor, "Enrollment"]):
    async def handle(self, command: EnrollTwoFactor) -> "Enrollment":
        from hexcore.darwin.plugins.two_factor import get_two_factor_service

        return await get_two_factor_service().enroll(
            user_id=command.user_id, account=command.account
        )


class ConfirmTwoFactorHandler(AbstractCommandHandler[ConfirmTwoFactor, "TwoFactor"]):
    async def handle(self, command: ConfirmTwoFactor) -> "TwoFactor":
        from hexcore.darwin.plugins.two_factor import get_two_factor_service

        return await get_two_factor_service().confirm(
            user_id=command.user_id, code=command.code
        )


class DisableTwoFactorHandler(AbstractCommandHandler[DisableTwoFactor, None]):
    async def handle(self, command: DisableTwoFactor) -> None:
        from hexcore.darwin.plugins.two_factor import get_two_factor_service

        await get_two_factor_service().disable(
            user_id=command.user_id, code=command.code
        )
        return None


class CompleteTwoFactorSignInHandler(
    AbstractCommandHandler[CompleteTwoFactorSignIn, TwoFactorSignIn]
):
    async def handle(self, command: CompleteTwoFactorSignIn) -> TwoFactorSignIn:
        from hexcore.darwin.plugins.two_factor import get_two_factor_service

        usuario, sesion, par = await get_two_factor_service().complete_sign_in(
            challenge=command.challenge,
            code=command.code,
            transport=t.cast(t.Any, command.transport),
            ip_address=command.ip_address,
            user_agent=command.user_agent,
        )
        return TwoFactorSignIn(user=usuario, session=sesion, tokens=par)
