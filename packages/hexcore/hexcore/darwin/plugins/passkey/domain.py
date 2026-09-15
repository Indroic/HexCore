"""
El dominio de `passkey`: las entidades, el puerto de WebAuthn y las excepciones.

**Por qué acá hay una dependencia y en `two_factor` no.** El TOTP son treinta líneas de `hmac` y
aritmética: una librería no compra corrección, compra superficie de cadena de suministro. WebAuthn
es lo contrario — CBOR, claves COSE, cuatro formatos de attestation, cadenas de certificados, un
contador de firmas— y escribirlo a mano sería criptografía propia en el camino de autenticación.
Va `py_webauthn` en el extra `[darwin-passkey]`, **detrás de un puerto**, así que los tests del
flujo corren sin el extra y sin un autenticador real.

Nada de este módulo toca sqlalchemy ni webauthn: lo importa el borde HTTP para mapear los status.
"""
from __future__ import annotations

import abc
import typing as t
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from hexcore.darwin.domain.exceptions import AuthenticationError, IdentityError

__all__ = [
    "Passkey",
    "PasskeyChallenge",
    "ChallengePurpose",
    "RegisteredCredential",
    "VerifiedAssertion",
    "AbstractWebAuthnVerifier",
    "AbstractPasskeyRepository",
    "AbstractPasskeyChallengeRepository",
    "PasskeyError",
    "PasskeyChallengeError",
    "PasskeyVerificationError",
    "PasskeyClonedAuthenticatorError",
    "PasskeyNotFoundError",
    "PasskeyAlreadyRegisteredError",
    "PasskeyLastFactorError",
    "PASSKEY_EXCEPTION_STATUS_MAP",
]

#: Para qué se emitió un desafío. Parte de la clave de canje: un desafío de registro no se puede
#: usar para autenticar, que si no sería una forma de saltear la verificación de la firma sobre el
#: `clientDataJSON` correcto.
ChallengePurpose = t.Literal["register", "authenticate"]


# ── Las entidades ─────────────────────────────────────────────────────────────
class Passkey(BaseModel):
    """
    Una credencial WebAuthn registrada.

    `sign_count` es el campo que importa y no es contabilidad: si un autenticador devuelve un
    contador **menor o igual** al guardado, o está clonado o alguien está replayeando una
    aserción. Es la única señal de compromiso que el protocolo da, y descartarla —muchas
    implementaciones lo hacen porque "algunos autenticadores no lo incrementan"— es tirar la única
    detección que hay.
    """

    id: UUID
    user_id: UUID
    #: El `credentialId` en base64url. Único globalmente: lo genera el autenticador.
    credential_id: str
    #: La clave pública en CBOR/COSE, en base64url. **No es un secreto**: es pública por diseño,
    #: y eso es lo que hace a WebAuthn resistente al phishing — un servidor comprometido no
    #: entrega nada que sirva para autenticarse en otro lado.
    public_key: str
    sign_count: int = 0
    #: El nombre que le puso el usuario ("iPhone de Ana"). Es lo único que le permite distinguir
    #: entre cinco credenciales al momento de borrar una.
    name: str | None = None
    #: El identificador del modelo de autenticador, si vino. Sirve para políticas ("sólo llaves
    #: certificadas") y para mostrarle al usuario un ícono reconocible.
    aaguid: str | None = None
    #: Si la credencial está respaldada en la nube del proveedor (una passkey sincronizada) o vive
    #: sólo en el dispositivo. Cambia la recomendación que se le hace al usuario: con una sola
    #: credencial no sincronizada, perder el teléfono es perder la cuenta.
    backed_up: bool = False
    transports: tuple[str, ...] = ()
    last_used_at: datetime | None = None
    created_at: datetime | None = None


class PasskeyChallenge(BaseModel):
    """
    Un desafío en vuelo.

    Vive segundos y es de un solo uso: el desafío es lo que ata la firma a *este* intento, así que
    poder canjearlo dos veces sería poder replayear la aserción.

    Se guarda **en claro** — ver el docstring del mixin. Es un nonce público, y hashearlo obligaría
    a que el `expected_challenge` saliera del propio cliente.
    """

    id: UUID
    challenge: str
    purpose: ChallengePurpose
    #: A qué usuario. `None` en un login sin usuario declarado —el flujo "usernameless" con
    #: credenciales descubribles—, que es el caso donde el navegador elige la credencial y el
    #: servidor todavía no sabe quién es.
    user_id: UUID | None = None
    expires_at: datetime
    consumed_at: datetime | None = None


class RegisteredCredential(t.NamedTuple):
    """Lo que sale de verificar un registro."""

    credential_id: str
    public_key: str
    sign_count: int
    aaguid: str | None = None
    backed_up: bool = False
    transports: tuple[str, ...] = ()
    user_verified: bool = False


class VerifiedAssertion(t.NamedTuple):
    """Lo que sale de verificar una autenticación."""

    credential_id: str
    new_sign_count: int
    user_verified: bool = False


# ── El puerto de WebAuthn ─────────────────────────────────────────────────────
class AbstractWebAuthnVerifier(abc.ABC):
    """
    Las cuatro operaciones del protocolo.

    Es un puerto por dos razones y la segunda es la que importa: `webauthn` va en un extra, y un
    test del flujo no puede depender de un autenticador real. Con el puerto, el test inyecta un
    doble y ejercita registro, login, credencial ajena y **contador clonado** — que es el caso que
    con hardware real sería imposible de reproducir.
    """

    @abc.abstractmethod
    def registration_options(
        self,
        *,
        user_id: UUID,
        user_name: str,
        exclude_credential_ids: t.Sequence[str] = (),
    ) -> tuple[dict[str, t.Any], bytes]:
        """
        Las opciones para `navigator.credentials.create()` y el desafío en bytes.

        `exclude_credential_ids` va siempre con lo que el usuario ya tiene: sin eso, el navegador
        le ofrece registrar de nuevo una credencial que ya está y el flujo falla al guardar, con un
        error de base en vez de un mensaje.
        """

    @abc.abstractmethod
    def verify_registration(
        self, *, credential: t.Mapping[str, t.Any], expected_challenge: bytes
    ) -> RegisteredCredential:
        """
        Verifica la respuesta del autenticador.

        Raises:
            PasskeyVerificationError: cualquier cosa que no cierre — firma, `origin`, `rp_id`,
                tipo. **Un solo error para todas**: el detalle va al log, no a la respuesta.
        """

    @abc.abstractmethod
    def authentication_options(
        self, *, allow_credential_ids: t.Sequence[str] = ()
    ) -> tuple[dict[str, t.Any], bytes]:
        """
        Las opciones para `navigator.credentials.get()` y el desafío.

        `allow_credential_ids` vacío es el flujo **sin usuario declarado**: el navegador elige
        entre las credenciales descubribles que tenga. Es el que da la mejor experiencia y el que
        obliga a que el desafío se pueda canjear sin saber de quién es.
        """

    @abc.abstractmethod
    def verify_authentication(
        self,
        *,
        credential: t.Mapping[str, t.Any],
        expected_challenge: bytes,
        public_key: str,
        current_sign_count: int,
    ) -> VerifiedAssertion:
        """
        Verifica la aserción contra la clave pública guardada.

        Raises:
            PasskeyVerificationError: la firma no valida, o el `origin`/`rp_id` no coinciden.
        """


# ── Los puertos de persistencia ───────────────────────────────────────────────
class AbstractPasskeyRepository(abc.ABC):
    """Las credenciales registradas."""

    @abc.abstractmethod
    async def add(self, passkey: Passkey) -> Passkey:
        """Guarda una credencial nueva."""

    @abc.abstractmethod
    async def get_by_credential_id(self, credential_id: str) -> Passkey | None:
        """La credencial por su id de autenticador. Es la búsqueda del login."""

    @abc.abstractmethod
    async def list_for_user(self, user_id: UUID) -> list[Passkey]:
        """Las credenciales del usuario, para la pantalla de ajustes."""

    @abc.abstractmethod
    async def bump_sign_count(
        self, credential_id: str, *, new_count: int, at: datetime
    ) -> bool:
        """
        Sube el contador de firmas, **sólo si el nuevo es mayor**, en una sola sentencia.

        `False` si no subió: o el autenticador está clonado, o es un replay, o dos aserciones
        concurrentes con el mismo contador. La condición va en el `WHERE` y no en Python porque
        leer-comparar-escribir deja pasar las dos peticiones de un replay concurrente — que es
        justamente cuando la detección importa.
        """

    @abc.abstractmethod
    async def delete(self, passkey_id: UUID) -> bool:
        """Borra una credencial. `True` si existía."""


class AbstractPasskeyChallengeRepository(abc.ABC):
    """Los desafíos en vuelo."""

    @abc.abstractmethod
    async def add(self, challenge: PasskeyChallenge) -> PasskeyChallenge:
        """Guarda el desafío."""

    @abc.abstractmethod
    async def consume(
        self, purpose: ChallengePurpose, challenge: str, *, at: datetime
    ) -> PasskeyChallenge | None:
        """
        Canjea el desafío, en **una sola sentencia**.

        Filtra por `purpose`: un desafío de registro no se canjea autenticando. `None` si no
        existe, venció o ya se usó — un solo valor para los tres.
        """

    @abc.abstractmethod
    async def delete_expired(self, *, before: datetime) -> int:
        """Barre los que quedaron. El usuario que cancela el diálogo del navegador deja uno."""


# ── Las excepciones ───────────────────────────────────────────────────────────
class PasskeyError(IdentityError):
    """Base de las fallas del plugin."""


class PasskeyChallengeError(AuthenticationError):
    """
    El desafío no existe, venció, ya se usó o es de otro propósito. 401.

    Un solo error para los cuatro: distinguirlos le diría a quien prueba si hay un flujo en curso.
    """


class PasskeyVerificationError(AuthenticationError):
    """
    La respuesta del autenticador no verifica. 401.

    **Un solo error para todos los motivos** —firma inválida, `origin` que no coincide, `rp_id`
    equivocado, tipo incorrecto— y el detalle va al log. Decirle a quien prueba *qué* chequeo
    falló es darle el camino para el siguiente intento.
    """


class PasskeyClonedAuthenticatorError(AuthenticationError):
    """
    El contador de firmas no avanzó. 401.

    ⚠️ **Es la única señal de compromiso que el protocolo WebAuthn da.** Un contador que no avanza
    significa que la aserción se está replayeando o que el autenticador fue clonado — y la
    respuesta correcta no es "reintentá", es cortar y avisarle al usuario.

    Muchas implementaciones descartan el contador porque "algunos autenticadores no lo
    incrementan". Acá se maneja distinto: si el guardado es 0 el chequeo no aplica (es el caso de
    los autenticadores que no lo usan), pero si alguna vez avanzó, tiene que seguir avanzando.
    """


class PasskeyNotFoundError(PasskeyError):
    """No existe esa credencial. 404 en el ciclo de vida, y 401 en el login."""


class PasskeyAlreadyRegisteredError(PasskeyError):
    """
    Esa credencial ya está registrada. 409.

    Puede ser de **otro** usuario, y ahí no se mueve: mover la credencial le sacaría al primero un
    método de acceso.
    """


class PasskeyLastFactorError(PasskeyError):
    """
    Borrar esa credencial dejaría la cuenta sin ningún método de acceso. 409.

    El botón que hace eso está a un click en cualquier pantalla de ajustes, y el usuario que lo
    aprieta no tiene forma de volver.
    """


#: El mapa que el plugin aporta vía `exception_status_map()`.
PASSKEY_EXCEPTION_STATUS_MAP: dict[type[Exception], int] = {
    PasskeyChallengeError: 401,
    PasskeyVerificationError: 401,
    PasskeyClonedAuthenticatorError: 401,
    PasskeyNotFoundError: 404,
    PasskeyAlreadyRegisteredError: 409,
    PasskeyLastFactorError: 409,
    # `PasskeyError` (la base) **no** se mapea, por lo mismo que el núcleo no mapea
    # `IdentityError`: `_specificity` ordena por profundidad de MRO, así que mapearla haría que
    # una falla nueva se tragara con ese status en vez de aparecer como un 500 en los tests.
}
