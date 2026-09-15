"""
Comandos y servicio de `magic_link`.

Las acciones se declaran con `@identity_action` y no se dejan derivar del nombre de la clase:
son el contrato al que se enganchan los hooks, y derivarlas las ataría al nombre de la clase —
renombrar el comando rompería en silencio los hooks de otros plugins.
"""
from __future__ import annotations

import typing as t
from datetime import timedelta

from hexcore.darwin.domain.entities import Verification
from hexcore.darwin.domain.exceptions import (
    AccountLockedError,
    InvalidCredentialsError,
)
from hexcore.darwin.domain.plugins import identity_action
from hexcore.darwin.domain.value_objects import Email
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.handlers import AbstractCommandHandler

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.context import Transport
    from hexcore.darwin.domain.entities import IdentitySession, User
    from hexcore.darwin.domain.value_objects import TokenPair

__all__ = [
    "RequestMagicLink",
    "ConsumeMagicLink",
    "MagicLinkIssued",
    "MagicLinkSession",
    "RequestMagicLinkHandler",
    "ConsumeMagicLinkHandler",
    "request_magic_link",
    "consume_magic_link",
    "registrar_uso",
]


# ── Comandos ──────────────────────────────────────────────────────────────────
@identity_action("magic_link.request")
class RequestMagicLink(Command):
    """Pide un magic link para un mail."""

    email: str


@identity_action("magic_link.consume")
class ConsumeMagicLink(Command):
    """Canjea un magic link y abre la sesión."""

    email: str
    token: str
    transport: str = "cookie"
    ip_address: str | None = None
    user_agent: str | None = None


# ── Resultados ────────────────────────────────────────────────────────────────
class MagicLinkIssued(t.NamedTuple):
    """
    El resultado de pedir un link.

    `token` es `None` cuando la cuenta **no existe**, y el llamador tiene que responder lo
    mismo en los dos casos: la diferencia va en si manda el mail. Devolver un error cuando no
    existe convertiría la ruta en un oráculo de enumeración sin autenticación.
    """

    email: str
    token: str | None


class MagicLinkSession(t.NamedTuple):
    user: "User"
    session: "IdentitySession"
    tokens: "TokenPair"


# ── El servicio ───────────────────────────────────────────────────────────────
async def request_magic_link(
    *, email: str, ttl: timedelta
) -> MagicLinkIssued:
    """
    Emite un magic link, invalidando los pendientes del mismo mail.

    Invalidar los anteriores no es limpieza: sin eso, cinco clicks en "reenviar" dejan cinco
    links válidos y el espacio a adivinar se multiplica por cinco.

    Devuelve `token=None` si la cuenta no existe, **sin** lanzar. Ver `MagicLinkIssued`.
    """
    from hexcore.darwin.application.container import get_identity_container
    from hexcore.darwin.infrastructure.hashing import generate_token, hash_token
    from hexcore.darwin.plugins.magic_link import MAGIC_LINK_PURPOSE

    contenedor = get_identity_container()
    normalizado = Email(value=email).value

    usuario = await contenedor.users().get_by_email(normalizado)
    if usuario is None:
        return MagicLinkIssued(email=normalizado, token=None)

    ahora = contenedor.clock().now()
    verificaciones = contenedor.verifications()
    await verificaciones.invalidate_for(normalizado, MAGIC_LINK_PURPOSE, at=ahora)

    # 32 bytes de aleatoriedad, no un código de 6 dígitos: el link viaja por mail y puede
    # quedar en logs, así que el espacio tiene que ser inatacable por fuerza bruta incluso sin
    # techo de intentos.
    token = generate_token()
    await verificaciones.add(
        Verification(
            identifier=normalizado,
            value_hash=hash_token(token),
            purpose=MAGIC_LINK_PURPOSE,
            expires_at=ahora + ttl,
        )
    )
    return MagicLinkIssued(email=normalizado, token=token)


async def consume_magic_link(
    *,
    email: str,
    token: str,
    transport: "Transport" = "cookie",
    ip_address: str | None = None,
    user_agent: str | None = None,
) -> MagicLinkSession:
    """
    Canjea el link y crea la sesión.

    El canje es **atómico** (`consume` hace `UPDATE ... WHERE consumed_at IS NULL RETURNING`),
    así que de dos clicks simultáneos sobre el mismo link exactamente uno gana. Sin eso, "de un
    solo uso" sería una afirmación falsa bajo concurrencia — que es justamente cuando importa.

    **Verifica el mail como efecto**: quien prueba que controla la casilla ya demostró lo que
    la verificación de mail prueba, y dejarlo sin verificar obligaría a un segundo mail para
    algo que acaba de ocurrir.

    Raises:
        InvalidCredentialsError: token inválido, vencido o ya usado. Un solo error para los
            tres: distinguirlos diría si el mail tiene un link pendiente.
        AccountLockedError: la cuenta está bloqueada.
    """
    from hexcore.darwin.application.container import get_identity_container
    from hexcore.darwin.infrastructure.hashing import hash_token
    from hexcore.darwin.plugins.magic_link import MAGIC_LINK_PURPOSE

    contenedor = get_identity_container()
    normalizado = Email(value=email).value
    ahora = contenedor.clock().now()

    consumido = await contenedor.verifications().consume(
        normalizado, MAGIC_LINK_PURPOSE, hash_token(token), at=ahora
    )
    if consumido is None:
        raise InvalidCredentialsError("El link no es válido o ya se usó.")

    usuarios = contenedor.users()
    usuario = await usuarios.get_by_email(normalizado)
    if usuario is None:
        # La cuenta se borró entre la emisión y el canje. Mismo error, por lo mismo.
        raise InvalidCredentialsError("El link no es válido o ya se usó.")

    if usuario.is_locked_at(ahora):
        raise AccountLockedError(
            "La cuenta está bloqueada temporalmente. Intentá más tarde."
        )

    if not usuario.email_verified:
        usuario = await usuarios.update(
            usuario.model_copy(update={"email_verified": True})
        )

    sesion, par = await contenedor.session_service().create(
        actor=usuario,
        transport=transport,
        ip_address=ip_address,
        user_agent=user_agent,
    )
    return MagicLinkSession(user=usuario, session=sesion, tokens=par)


# ── Handlers ──────────────────────────────────────────────────────────────────
class RequestMagicLinkHandler(
    AbstractCommandHandler[RequestMagicLink, MagicLinkIssued]
):
    def __init__(self, ttl: timedelta | None = None) -> None:
        from hexcore.darwin.plugins.magic_link import DEFAULT_TTL

        self._ttl = ttl or DEFAULT_TTL

    async def handle(self, command: RequestMagicLink) -> MagicLinkIssued:
        return await request_magic_link(email=command.email, ttl=self._ttl)


class ConsumeMagicLinkHandler(
    AbstractCommandHandler[ConsumeMagicLink, MagicLinkSession]
):
    async def handle(self, command: ConsumeMagicLink) -> MagicLinkSession:
        return await consume_magic_link(
            email=command.email,
            token=command.token,
            transport=t.cast(t.Any, command.transport),
            ip_address=command.ip_address,
            user_agent=command.user_agent,
        )


# ── El hook de ejemplo ────────────────────────────────────────────────────────
async def registrar_uso(resultado: t.Any) -> None:
    """
    Hook `after` de ejemplo: registra el uso en la auditoría.

    Devuelve `None` —o sea, no cambia el resultado— que es lo que hace un hook que sólo
    observa. Es también la forma mínima de un hook real: recibe el valor, hace algo, y no
    toca nada.
    """
    from hexcore.darwin.application.container import get_identity_container

    contenedor = get_identity_container()
    sink = getattr(contenedor, "_audit", None)
    if sink is None or not isinstance(resultado, MagicLinkSession):
        return None

    await sink.record(
        action="magic_link.consumed",
        actor_id=resultado.user.id,
        subject_id=resultado.user.id,
        metadata={"session_id": str(resultado.session.id)},
    )
    return None
