"""
Los flujos de `two_factor`: inscribir, confirmar, exigir, completar y desactivar.

Cinco flujos y una idea: **el sign-in se parte en dos**. El primer paso valida la contraseña y
—si el usuario tiene segundo factor confirmado— no emite ningún token: lanza
`TwoFactorRequiredError` con un desafío. El segundo paso canjea el desafío junto con el código
y ahí sí se crea la sesión.

Que no se emita **nada** en el primer paso es la propiedad central. La alternativa que se ve
seguido —emitir una sesión "parcial" con un scope reducido— pone en manos del cliente un token
real que después hay que acordarse de restringir en cada endpoint, y el endpoint que se olvida es
el que convierte el 2FA en decorativo.

El desafío se guarda en `verification` con `purpose="two_factor"` y no en un JWT: la tabla ya
tiene el canje atómico (`UPDATE ... WHERE consumed_at IS NULL RETURNING`), así que el desafío es
de un solo uso y revocable sin escribir nada nuevo. Un desafío stateless sería replayeable
durante todo su TTL.
"""
from __future__ import annotations

import typing as t
from datetime import timedelta
from uuid import UUID

from hexcore.darwin.domain.entities import Verification
from hexcore.darwin.domain.exceptions import AccountLockedError, InvalidCredentialsError
from hexcore.darwin.plugins.two_factor.domain import (
    MAX_FAILED_ATTEMPTS,
    TwoFactor,
    TwoFactorAlreadyConfirmedError,
    TwoFactorInvalidCodeError,
    TwoFactorNotEnrolledError,
    TwoFactorRequiredError,
)

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.context import Transport
    from hexcore.darwin.domain.entities import IdentitySession, User
    from hexcore.darwin.domain.ports import (
        AbstractClock,
        AbstractUserRepository,
        AbstractVerificationRepository,
    )
    from hexcore.darwin.domain.value_objects import TokenPair
    from hexcore.darwin.plugins.two_factor.crypto import TotpSecretCipher
    from hexcore.darwin.plugins.two_factor.domain import AbstractTwoFactorRepository

__all__ = [
    "TWO_FACTOR_PURPOSE",
    "DEFAULT_CHALLENGE_TTL",
    "Enrollment",
    "TwoFactorService",
]

#: El `purpose` del desafío en `verification`. Parte de la clave de canje, así que un desafío de
#: 2FA no se puede usar para verificar un mail ni al revés.
TWO_FACTOR_PURPOSE: t.Final = "two_factor"

#: Cuánto vive un desafío.
#:
#: 5 minutos: lo que tarda alguien en buscar el teléfono, desbloquearlo y tipear. Más largo
#: sería una ventana en la que un desafío filtrado —de un log, del historial del navegador—
#: sigue sirviendo; más corto haría fallar al usuario que se distrajo, y volver a pedir la
#: contraseña es la clase de fricción que hace que se apague el 2FA.
DEFAULT_CHALLENGE_TTL = timedelta(minutes=5)

#: Separa el id del usuario del token dentro del desafío. Un punto y no `:` para que el valor
#: viaje sin escapar en un header o en una query string.
_SEPARADOR = "."


def _partir_desafio(challenge: str) -> tuple[UUID | None, str | None]:
    """
    Parte el desafío en `(user_id, token)`. `(None, None)` si no tiene la forma esperada.

    Devuelve `None` en vez de lanzar porque el valor lo manda el cliente: un `ValueError` de
    `UUID` acá saldría como un 500 y le diría a quien prueba formatos que encontró un camino no
    manejado.
    """
    identificador, _, token = challenge.partition(_SEPARADOR)
    if not identificador or not token:
        return None, None
    try:
        return UUID(identificador), token
    except ValueError:
        return None, None


class Enrollment(t.NamedTuple):
    """
    El resultado de inscribir un factor.

    Lleva el secreto **en claro**, porque el usuario tiene que poder escanearlo o tipearlo: es
    la única vez que sale de la aplicación. ⚠️ No lo loguees y no lo persistas del lado del
    cliente.
    """

    secret: str
    uri: str
    confirmed: bool


class TwoFactorService:
    """
    Los flujos del segundo factor.

    Uso::

        servicio = get_two_factor_service()
        inscripcion = await servicio.enroll(user_id=usuario.id, account=usuario.email)
        await servicio.confirm(user_id=usuario.id, code="123456")
    """

    def __init__(
        self,
        *,
        repository: "AbstractTwoFactorRepository",
        users: "AbstractUserRepository",
        verifications: "AbstractVerificationRepository",
        cipher: "TotpSecretCipher",
        clock: "AbstractClock",
        sessions: t.Any,
        issuer: str = "HexCore",
        challenge_ttl: timedelta = DEFAULT_CHALLENGE_TTL,
    ) -> None:
        self._repo = repository
        self._users = users
        self._verifications = verifications
        self._cipher = cipher
        self._clock = clock
        self._sessions = sessions
        self._issuer = issuer
        self._ttl = challenge_ttl

    # ── Inscripción ───────────────────────────────────────────────────────────
    async def enroll(self, *, user_id: UUID, account: str) -> Enrollment:
        """
        Genera un secreto nuevo y lo guarda **sin confirmar**.

        Sin confirmar es lo que evita el bloqueo autoinfligido: si inscribir activara el factor,
        el usuario que guardó mal el QR queda afuera en el siguiente login y sólo lo saca de ahí
        una intervención humana. Acá, hasta que no demuestre que su app genera el código
        correcto, el 2FA no exige nada.

        Raises:
            TwoFactorAlreadyConfirmedError: ya hay uno activo. Re-inscribir rotaría el secreto
                en silencio y el usuario quedaría con el QR viejo.
        """
        from hexcore.darwin.plugins.two_factor.totp import (
            generate_totp_secret,
            provisioning_uri,
        )

        existente = await self._repo.get_for_user(user_id)
        if existente is not None and existente.is_confirmed:
            raise TwoFactorAlreadyConfirmedError(
                "Ya tenés un segundo factor activo. Desactivalo antes de inscribir otro."
            )

        secreto = generate_totp_secret()
        await self._repo.upsert(
            TwoFactor(
                user_id=user_id,
                secret_encrypted=self._cipher.encrypt(secreto),
            )
        )
        return Enrollment(
            secret=secreto,
            uri=provisioning_uri(secreto, account=account, issuer=self._issuer),
            confirmed=False,
        )

    async def confirm(self, *, user_id: UUID, code: str) -> TwoFactor:
        """
        Activa el factor, verificando que la app del usuario genere el código correcto.

        Raises:
            TwoFactorNotEnrolledError: no hay factor para confirmar.
            TwoFactorAlreadyConfirmedError: ya estaba activo.
            TwoFactorInvalidCodeError: el código no valida.
        """
        factor = await self._exigir_factor(user_id)
        if factor.is_confirmed:
            raise TwoFactorAlreadyConfirmedError("Ese segundo factor ya está activo.")

        paso = self._verificar_codigo(factor, code)
        confirmado = await self._repo.confirm(
            user_id, at=self._clock.now(), step=paso
        )
        if confirmado is None:
            # Otra petición ganó la carrera. Se responde "ya está activo" y no un error de
            # código: el usuario hizo lo correcto dos veces.
            raise TwoFactorAlreadyConfirmedError("Ese segundo factor ya está activo.")
        return confirmado

    async def disable(self, *, user_id: UUID, code: str) -> None:
        """
        Desactiva el factor, **exigiendo un código válido**.

        Exigirlo no es burocracia: sin eso, quien roba una sesión con el 2FA ya pasado apaga el
        segundo factor y se queda con la cuenta. Es la operación que más protección necesita, no
        menos.

        Raises:
            TwoFactorNotEnrolledError, TwoFactorInvalidCodeError
        """
        factor = await self._exigir_factor(user_id)
        paso = self._verificar_codigo(factor, code)

        # Se consume el paso antes de borrar: si el borrado fallara, el código igual queda
        # gastado y no se puede reintentar con el mismo.
        await self._repo.consume_step(
            user_id, step=paso, after_step=factor.last_used_step
        )
        await self._repo.delete_for_user(user_id)

    # ── El sign-in en dos pasos ───────────────────────────────────────────────
    async def describe(self, user_id: UUID) -> tuple[bool, bool]:
        """
        `(inscripto, confirmado)` para el usuario.

        Devuelve las dos cosas y no una sola porque son estados distintos con interfaces
        distintas: inscripto-sin-confirmar es "terminá de configurar", y no-inscripto es
        "activá el 2FA".
        """
        factor = await self._repo.get_for_user(user_id)
        if factor is None:
            return False, False
        return True, factor.is_confirmed

    async def is_required_for(self, user_id: UUID) -> bool:
        """Si el usuario tiene un factor **confirmado**. Inscripto no cuenta."""
        factor = await self._repo.get_for_user(user_id)
        return factor is not None and factor.is_confirmed

    async def issue_challenge(self, *, user: "User") -> str:
        """
        Emite el desafío del segundo paso e invalida los pendientes del usuario.

        Invalidar los anteriores no es limpieza: sin eso, cinco intentos de login dejan cinco
        desafíos válidos, y cada uno es un vale para completar un login con un código robado.
        """
        from hexcore.darwin.infrastructure.hashing import generate_token, hash_token

        ahora = self._clock.now()
        identificador = str(user.id)
        await self._verifications.invalidate_for(
            identificador, TWO_FACTOR_PURPOSE, at=ahora
        )

        token = generate_token()
        await self._verifications.add(
            Verification(
                identifier=identificador,
                value_hash=hash_token(token),
                purpose=TWO_FACTOR_PURPOSE,
                expires_at=ahora + self._ttl,
            )
        )

        # El desafío lleva el id adelante, separado por un punto. Es lo que permite canjearlo
        # con `consume(identifier, purpose, hash)` —la operación atómica que ya existe— sin
        # agregarle al puerto un `consume_by_hash` que sacaría el identificador de la clave de
        # canje. La parte secreta sigue siendo sólo el token: el id no es un secreto, y
        # adivinarlo no ayuda porque el hash tiene que coincidir igual.
        return f"{identificador}{_SEPARADOR}{token}"

    async def require(self, user: "User") -> None:
        """
        El hook del sign-in. Lanza `TwoFactorRequiredError` si el usuario tiene 2FA activo.

        Se engancha a `SIGN_IN_AUTHENTICATED`, que corre con la contraseña ya validada y la
        sesión **todavía no creada**. Antes no se sabe quién es el usuario; después ya hay un
        par de tokens emitido que habría que revocar — y el que se olvida de revocarlo dejó el
        2FA en decorativo.
        """
        if not await self.is_required_for(user.id):
            return None

        raise TwoFactorRequiredError(challenge=await self.issue_challenge(user=user))

    async def complete_sign_in(
        self,
        *,
        challenge: str,
        code: str,
        transport: "Transport" = "cookie",
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> tuple["User", "IdentitySession", "TokenPair"]:
        """
        Canjea el desafío con el código y crea la sesión.

        El canje del desafío es atómico y de un solo uso, así que un desafío interceptado sirve
        una vez y sólo con un código válido — que a su vez es de un solo uso por
        `consume_step`.

        Raises:
            InvalidCredentialsError: el desafío no existe, venció o ya se usó. **El mismo error
                que un login con contraseña equivocada**: distinguirlos diría si hay un intento
                de login en curso para esa cuenta.
            TwoFactorInvalidCodeError: el desafío era válido pero el código no.
            AccountLockedError: la cuenta se bloqueó entre los dos pasos.
        """
        from hexcore.darwin.infrastructure.hashing import hash_token

        ahora = self._clock.now()

        identificador, token = _partir_desafio(challenge)
        if identificador is None or token is None:
            raise InvalidCredentialsError()

        # ⚠️ El desafío se consume **antes** de verificar el código, y eso es deliberado: si se
        # consumiera después, un atacante con el desafío podría probar códigos indefinidamente
        # sobre el mismo desafío. Consumiéndolo primero, cada intento cuesta un desafío nuevo —
        # o sea la contraseña.
        consumido = await self._verifications.consume(
            str(identificador), TWO_FACTOR_PURPOSE, hash_token(token), at=ahora
        )
        if consumido is None:
            raise InvalidCredentialsError()

        usuario = await self._users.get_by_id(identificador)
        if usuario is None:
            raise InvalidCredentialsError()
        if usuario.is_locked_at(ahora):
            raise AccountLockedError(
                "La cuenta está bloqueada temporalmente. Intentá más tarde."
            )

        factor = await self._exigir_factor(usuario.id)
        paso = self._verificar_codigo(factor, code)
        if not await self._repo.consume_step(
            usuario.id, step=paso, after_step=factor.last_used_step
        ):
            # Válido pero ya usado: otra petición ganó la carrera con el mismo código. Es
            # exactamente el replay contra el que existe `consume_step`.
            raise TwoFactorInvalidCodeError(
                "El código no es válido o ya se usó. Esperá el siguiente."
            )

        await self._repo.reset_failures(usuario.id)
        sesion, par = await self._sessions.create(
            actor=usuario,
            transport=transport,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        return usuario, sesion, par

    # ── Interno ───────────────────────────────────────────────────────────────
    async def _exigir_factor(self, user_id: UUID) -> TwoFactor:
        factor = await self._repo.get_for_user(user_id)
        if factor is None:
            raise TwoFactorNotEnrolledError(
                "No tenés un segundo factor inscripto. Inscribilo antes de usarlo."
            )
        return factor

    def _verificar_codigo(self, factor: TwoFactor, code: str) -> int:
        """
        Verifica el código y devuelve el paso con el que matcheó.

        Chequea el techo de intentos **primero** y sin calcular nada: seguir verificando después
        del límite le regala al atacante intentos gratis, y calcular el HMAC igual haría que el
        tiempo de respuesta diga si la fila existe.
        """
        from hexcore.darwin.plugins.two_factor.totp import verify_totp

        if factor.is_locked_out:
            raise TwoFactorInvalidCodeError(
                f"Demasiados intentos fallidos ({MAX_FAILED_ATTEMPTS}). Volvé a iniciar "
                f"sesión para reintentar."
            )

        secreto = self._cipher.decrypt(factor.secret_encrypted)
        paso = verify_totp(
            secreto,
            code,
            self._clock.now().timestamp(),
            after_step=factor.last_used_step,
        )
        if paso is None:
            raise TwoFactorInvalidCodeError(
                "El código no es válido o ya se usó. Esperá el siguiente."
            )
        return paso

    async def record_failed_attempt(self, user_id: UUID) -> int:
        """
        Registra un intento fallido. Lo llama el borde, no el servicio.

        Está afuera de `_verificar_codigo` a propósito: ese método es sincrónico y puro, y
        contar intentos es un efecto que el llamador tiene que poder hacer una sola vez —si lo
        hiciera el verificador, un flujo que verifica dos veces contaría dos.
        """
        return await self._repo.record_failure(user_id)
