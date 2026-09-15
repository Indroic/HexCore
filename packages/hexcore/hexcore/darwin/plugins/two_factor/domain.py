"""
El dominio de `two_factor`: la entidad, el puerto y sus excepciones.

Las excepciones viven acá y **no** en `hexcore/darwin/domain/exceptions.py`: el núcleo no tiene
por qué conocer los modos de falla de un plugin. Lo que las conecta con el borde HTTP es
`DarwinPlugin.exception_status_map()`, que `create_app` mergea.

Heredan de `IdentityError` a propósito, y eso tiene una consecuencia concreta: `run_hooks` deja
pasar los `IdentityError` sin envolverlos, así que un hook puede cortar un sign-in lanzando
`TwoFactorRequiredError` y el borde lo mapea a su status en vez de a un 500.
"""
from __future__ import annotations

import abc
from datetime import datetime
from uuid import UUID

from hexcore.darwin.domain.exceptions import AuthenticationError, IdentityError
from hexcore.domain.base import BaseEntity

__all__ = [
    "TwoFactor",
    "AbstractTwoFactorRepository",
    "TwoFactorError",
    "TwoFactorRequiredError",
    "TwoFactorInvalidCodeError",
    "TwoFactorNotEnrolledError",
    "TwoFactorAlreadyConfirmedError",
    "TWO_FACTOR_EXCEPTION_STATUS_MAP",
    "MAX_FAILED_ATTEMPTS",
]

#: Intentos fallidos seguidos antes de rechazar sin siquiera calcular.
#:
#: 5 y no 3: un usuario con el reloj del teléfono desincronizado falla dos o tres veces
#: legítimamente, y bloquearlo ahí lo manda a soporte. 5 y no 20 porque un OTP de 6 dígitos son
#: 10⁶ combinaciones y la ventana deja válidos 3 códigos a la vez, o sea que cada intento tiene
#: una chance de 3 en 10⁶: con 20 intentos por ventana y reintentos indefinidos, el ataque
#: cierra en horas.
MAX_FAILED_ATTEMPTS = 5


# ── La entidad ────────────────────────────────────────────────────────────────
class TwoFactor(BaseEntity):
    """
    El segundo factor TOTP de un usuario.

    `secret_encrypted` viaja cifrado **también acá dentro**, y no se descifra al hidratar: la
    entidad se loguea, se serializa en un error de pydantic y aparece en el `repr` de un
    traceback de pytest. El descifrado ocurre en el servicio, en la línea que lo necesita.
    """

    user_id: UUID
    secret_encrypted: str
    confirmed_at: datetime | None = None
    last_used_step: int | None = None
    failed_attempts: int = 0

    @property
    def is_confirmed(self) -> bool:
        """
        Si el factor está **activo**.

        Inscripto no es activo: una fila sin confirmar existe pero no exige nada. Activar en el
        mismo paso que se inscribe deja afuera para siempre al usuario que guardó mal el
        secreto, y sacarlo de ahí requiere intervención humana.
        """
        return self.confirmed_at is not None

    @property
    def is_locked_out(self) -> bool:
        """Si agotó los intentos. Ver `MAX_FAILED_ATTEMPTS`."""
        return self.failed_attempts >= MAX_FAILED_ATTEMPTS


# ── El puerto ─────────────────────────────────────────────────────────────────
class AbstractTwoFactorRepository(abc.ABC):
    """
    Persistencia del segundo factor.

    Puerto propio y no una tabla más del núcleo: el plugin se tiene que poder implementar sobre
    otro almacén —un HSM, un servicio externo de MFA— sin que el núcleo sepa nada.
    """

    @abc.abstractmethod
    async def get_for_user(self, user_id: UUID) -> TwoFactor | None:
        """El factor del usuario, confirmado o no. `None` si no hay."""

    @abc.abstractmethod
    async def upsert(self, factor: TwoFactor) -> TwoFactor:
        """
        Crea o reemplaza el factor del usuario.

        Reemplaza en vez de agregar porque hay un `UNIQUE` sobre `user_id`: dos filas dejarían
        que un secreto de una inscripción abandonada siga sirviendo para entrar.
        """

    @abc.abstractmethod
    async def confirm(self, user_id: UUID, *, at: datetime, step: int) -> TwoFactor | None:
        """
        Marca el factor confirmado, **sólo si todavía no lo estaba**.

        Atómico y condicional a propósito: dos confirmaciones concurrentes con códigos
        distintos dejarían el `last_used_step` del que perdió la carrera, y el código del que
        ganó podría reusarse. Devuelve `None` si ya estaba confirmado.
        """

    @abc.abstractmethod
    async def consume_step(
        self, user_id: UUID, *, step: int, after_step: int | None
    ) -> bool:
        """
        Marca el paso TOTP como usado, en **una sola sentencia**.

        `True` si esta llamada fue la que lo consumió. Es la defensa de replay, y tiene que ser
        atómica: con leer-y-después-escribir, dos peticiones con el mismo código robado pasan
        las dos, que es exactamente cuando el replay importa.
        """

    @abc.abstractmethod
    async def record_failure(self, user_id: UUID) -> int:
        """Incrementa y devuelve los intentos fallidos. Atómico."""

    @abc.abstractmethod
    async def reset_failures(self, user_id: UUID) -> None:
        """Vuelve los intentos fallidos a cero. Se llama tras un código válido."""

    @abc.abstractmethod
    async def delete_for_user(self, user_id: UUID) -> bool:
        """Borra el factor. `True` si había uno."""


# ── Las excepciones ───────────────────────────────────────────────────────────
class TwoFactorError(IdentityError):
    """Base de las fallas del plugin."""


class TwoFactorRequiredError(AuthenticationError):
    """
    La contraseña era correcta pero falta el segundo factor.

    Lleva el `challenge`: un token de un solo uso, corto, que identifica **este** intento de
    login. Sin él, el segundo paso tendría que volver a recibir la contraseña —y entonces el
    cliente la tiene que guardar mientras el usuario busca el teléfono— o bastaría con el mail,
    y cualquiera que lo conozca completa el login de otro con su propio código.

    Es un `AuthenticationError` (401) y no un 403: la autenticación **no terminó**. Un 403 diría
    que el usuario está autenticado y no autorizado, que es lo contrario de lo que pasa.
    """

    def __init__(self, message: str = "", *, challenge: str | None = None) -> None:
        super().__init__(
            message or "Falta el segundo factor. Completá el desafío con tu código."
        )
        #: El token del desafío, a devolver en el cuerpo del 401.
        self.challenge = challenge


class TwoFactorInvalidCodeError(AuthenticationError):
    """
    El código no es válido, ya se usó, o se agotaron los intentos.

    **Un solo error para los tres**, igual que `InvalidCredentialsError` cubre "mail inexistente"
    y "contraseña incorrecta": distinguirlos le diría al atacante si el código que probó era
    criptográficamente válido —o sea si tiene el secreto— y cuántos intentos le quedan.
    """


class TwoFactorNotEnrolledError(TwoFactorError):
    """El usuario no tiene segundo factor inscripto. 409: falta un paso previo."""


class TwoFactorAlreadyConfirmedError(TwoFactorError):
    """
    Ya hay un factor confirmado.

    Re-inscribir sin desactivar primero **rotaría el secreto en silencio**: el usuario seguiría
    con el QR viejo en su app y quedaría afuera en el próximo login.
    """


#: El mapa que el plugin aporta vía `exception_status_map()`.
TWO_FACTOR_EXCEPTION_STATUS_MAP: dict[type[Exception], int] = {
    TwoFactorRequiredError: 401,
    TwoFactorInvalidCodeError: 401,
    TwoFactorNotEnrolledError: 409,
    TwoFactorAlreadyConfirmedError: 409,
    # `TwoFactorError` (la base) **no** se mapea, por lo mismo que `IdentityError` no se mapea
    # en el núcleo: `_specificity` ordena por profundidad de MRO, así que registrarla haría que
    # una falla nueva sin mapear se tragara con este status en vez de aparecer como un 500 en
    # los tests.
}
