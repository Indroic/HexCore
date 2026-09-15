"""
Entidades de dominio de identidad. Port del esquema de 4 tablas de Better Auth.

Heredan de `BaseEntity`, así que traen `id`, `created_at`, `updated_at`, `is_active` y el
registro de eventos de dominio. **Ojo con la asimetría**: estas entidades son del dominio, y
los modelos SQLAlchemy que las persisten (Fase 3) **no** heredan de `BaseModel[T]` — porque
`get_domain_entity()` no tiene default y `SqlAlchemyUnitOfWork.collect_domain_entities()` lo
llama para todo `BaseModel` trackeado, así que una fila insertada sin `set_domain_entity()`
hace explotar el commit *después* de que la transacción ya se confirmó.

`IdentitySession` no se llama `Session` a propósito: `Session` colisiona visualmente con
`sqlalchemy.orm.Session` y con `AsyncSession` en cualquier archivo que toque las dos cosas, y
el bug que sale de confundirlas es de los caros.

Los desvíos frente a Better Auth están anotados campo por campo.
"""
from __future__ import annotations

import typing as t
from datetime import datetime
from uuid import UUID, uuid4

from pydantic import Field

from hexcore.darwin.domain.value_objects import VerificationPurpose
from hexcore.domain.base import BaseEntity

__all__ = [
    "CREDENTIAL_PROVIDER",
    "User",
    "IdentitySession",
    "Account",
    "Verification",
]

#: El `provider_id` de la credencial local. Better Auth usa el mismo valor, así que la fila
#: de contraseña es sólo otra "cuenta" — lo que hace que agregar OAuth no requiera un esquema
#: nuevo, sólo filas nuevas.
CREDENTIAL_PROVIDER = "credential"


class User(BaseEntity):
    """
    Un usuario. Port de la tabla `user` de Better Auth.

    La contraseña **no está acá**: vive en `Account` con `provider_id="credential"`. Es el
    diseño de Better Auth y es el correcto — un usuario puede tener cero contraseñas (entra
    sólo con Google) o cambiar de método sin tocar su fila.
    """

    email: str
    email_verified: bool = False
    name: str | None = None
    image: str | None = None

    #: Fuera de Better Auth. Permite revocar **todas** las sesiones del usuario con un solo
    #: UPDATE, sin importar cuántas tenga: el token lleva `gen` y la verificación compara.
    #: La alternativa —recorrer y revocar sesión por sesión— es O(n) y no es atómica.
    token_generation: int = 0

    #: Bloqueo temporal por intentos fallidos o por decisión administrativa. Separado de
    #: `is_active` (que es el borrado lógico de `BaseEntity`) porque son cosas distintas: una
    #: cuenta bloqueada existe y vuelve, una desactivada se fue.
    locked_until: datetime | None = None

    #: Paridad con `additionalFields` de Better Auth. Para flags escalares que nadie
    #: consulta por SQL (`has_seen_tour`, `two_factor_enabled`). Lo que necesite un índice,
    #: un NOT NULL o una FK va como columna propia extendiendo el mixin — ver la estrategia
    #: híbrida en el documento de arquitectura.
    extra: dict[str, t.Any] = Field(default_factory=dict)

    def is_locked_at(self, moment: datetime) -> bool:
        return self.locked_until is not None and moment < self.locked_until


class IdentitySession(BaseEntity):
    """
    Una sesión. Port de `session`, con **el desvío más importante del esquema**.

    Better Auth tiene un solo `userId` más un `impersonatedBy` opcional. Acá hay **dos
    principales y ninguno es opcional**:

    - `actor_user_id`: la persona física que ejecuta.
    - `subject_user_id`: la cuenta afectada.

    En una sesión normal son el mismo. En una impersonación difieren, y como los dos están
    siempre presentes, toda fila escrita por esa sesión es atribuible sin ambigüedad. Con un
    solo id y un flag opcional, reconstruir quién hizo qué depende de que el flag se haya
    seteado bien en todos los caminos — y ese es exactamente el tipo de invariante que se
    rompe en el camino menos transitado.
    """

    actor_user_id: UUID
    subject_user_id: UUID

    #: **SHA-256 del token, nunca el token.** Better Auth guarda el token en claro; acá no,
    #: porque un dump de esta tabla sería un set de credenciales de sesión utilizables. No se
    #: usa Argon2: el token es aleatorio de 256 bits, no una contraseña, así que no hay
    #: diccionario del que defenderse y SHA-256 es tres órdenes de magnitud más rápido en el
    #: camino caliente.
    token_hash: str

    expires_at: datetime
    revoked_at: datetime | None = None
    #: Cuándo se canjeó por rotación. Una sesión consumida que se vuelve a presentar es la
    #: señal de robo de token.
    consumed_at: datetime | None = None

    #: Linaje de rotación de refresh. Todas las sesiones que salen de rotaciones sucesivas
    #: comparten familia, así que un reuso puede revocar el linaje entero.
    family_id: UUID = Field(default_factory=uuid4)

    #: Atado al `aud` del token: impide replayear una cookie como Bearer y esquivar CSRF.
    transport: str = "cookie"

    ip_address: str | None = None
    user_agent: str | None = None

    #: Datos de la impersonación, si la hay. Redundante con `actor != subject` a propósito:
    #: el motivo y quién autorizó no se pueden deducir de los ids.
    impersonation_reason: str | None = None
    impersonation_granted_by: UUID | None = None
    impersonation_expires_at: datetime | None = None

    @property
    def is_impersonated(self) -> bool:
        return self.actor_user_id != self.subject_user_id

    def is_live_at(self, moment: datetime) -> bool:
        """
        Si la sesión sirve en `moment`.

        Un solo lugar donde se juntan las tres condiciones —no revocada, no consumida, no
        vencida— para que ningún camino se olvide de chequear una. Tener esto disperso es
        cómo aparece el bug de "el logout no cierra la sesión".
        """
        if self.revoked_at is not None:
            return False
        if self.consumed_at is not None:
            return False
        return moment < self.expires_at


class Account(BaseEntity):
    """
    Una forma de entrar: OAuth, o la credencial local. Port de `account`.

    Los tokens de terceros van cifrados en reposo (lo hace la capa de infraestructura, Fase
    4): son credenciales de otro sistema, y filtrarlas es un incidente en la API de un
    tercero además del propio.
    """

    user_id: UUID
    #: ``"credential"`` para la contraseña local; ``"google"``, ``"github"``… para OAuth.
    provider_id: str
    #: El id del usuario **en el proveedor**. Para la credencial local, el propio `user_id`.
    account_id: str

    #: Sólo con `provider_id == CREDENTIAL_PROVIDER`. Es un hash de Argon2id, no la contraseña.
    password: str | None = None

    access_token: str | None = None
    refresh_token: str | None = None
    id_token: str | None = None
    scope: str | None = None
    access_token_expires_at: datetime | None = None
    refresh_token_expires_at: datetime | None = None

    @property
    def is_credential(self) -> bool:
        return self.provider_id == CREDENTIAL_PROVIDER


class Verification(BaseEntity):
    """
    Un token de un solo uso. Port de `verification`.

    Tres desvíos frente a Better Auth, los tres por el mismo motivo —que la tabla no sea
    utilizable si se filtra, y que la fuerza bruta tenga techo:

    - `value_hash` en vez de `value`: se guarda el hash, no el código. Un dump no sirve para
      canjear nada.
    - `purpose`: parte de la identidad del token. Un código de reset de contraseña no se
      puede canjear en el flujo de verificación de mail.
    - `attempts`: un OTP de 6 dígitos son 10⁶ combinaciones, que sin techo de intentos se
      agotan en minutos.
    """

    identifier: str
    value_hash: str
    purpose: VerificationPurpose
    expires_at: datetime
    consumed_at: datetime | None = None
    attempts: int = 0

    def is_usable_at(self, moment: datetime, *, max_attempts: int = 5) -> bool:
        if self.consumed_at is not None:
            return False
        if self.attempts >= max_attempts:
            return False
        return moment < self.expires_at
