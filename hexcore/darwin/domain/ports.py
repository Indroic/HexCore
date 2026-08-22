"""
Puertos del subsistema de identidad.

Nombrados `Abstract*`, que es la convención canónica del repo desde 5.0 (`I*` está deprecado
para la superficie de CQRS). Ninguno hereda de `IBaseRepository`, y eso es **deliberado**:
`_repository_key_from_class_name` mapea `UserRepository` a la clave ``user`` y **levanta
`ValueError` ante una colisión**, así que un repositorio de identidad autodescubrible
rompería el Unit of Work de todo consumidor que ya tenga el suyo. Es la misma regla que
`SqlAlchemyCronJobRepository` documenta y que sus tests fijan.

Consecuencia práctica: estos puertos declaran las operaciones que los flujos de auth
necesitan y nada más. No hay `list_all` genérico ni paginación de propósito general — para
eso el consumidor usa sus propios repositorios sobre las mismas tablas.

Módulo de dominio puro: sólo stdlib, pydantic y los value objects de al lado. Sin sqlalchemy,
sin crypto.
"""
from __future__ import annotations

import abc
import typing as t
from datetime import datetime
from uuid import UUID

from hexcore.darwin.domain.value_objects import VerificationPurpose

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.entities import (
        Account,
        IdentitySession,
        User,
        Verification,
    )

#: Contraseña señuelo de `AbstractPasswordHasher.hash_dummy`. El valor no importa —nunca se
#: compara con nada— sólo que hashearla cueste lo mismo que una real.
_SENUELO = "$senuelo$para$igualar$el$tiempo$de$respuesta$"

__all__ = [
    "AbstractClock",
    "AbstractPasswordHasher",
    "AbstractUserRepository",
    "AbstractSessionRepository",
    "AbstractAccountRepository",
    "AbstractVerificationRepository",
    "AbstractRevocationList",
    "AbstractAuditSink",
]


class AbstractClock(abc.ABC):
    """
    El reloj, como puerto.

    Existe para que los tests de TTL, ventanas de rotación y vencimiento de impersonación no
    necesiten `freezegun` ni `time-machine`: se inyecta un reloj falso y se lo adelanta. Es
    la razón por la que este módulo **no agrega ninguna dependencia de desarrollo** al
    proyecto.

    Todo lo que dependa del tiempo en Darwin lo pide por acá. Un `datetime.now()` suelto en
    un handler es un test que no se puede escribir.
    """

    @abc.abstractmethod
    def now(self) -> datetime:
        """El instante actual, **siempre tz-aware en UTC**."""
        raise NotImplementedError


class AbstractPasswordHasher(abc.ABC):
    """
    Hasheo y verificación de contraseñas.

    `verify` **no** devuelve por qué falló, y `needs_rehash` está separado para que la
    migración de un algoritmo viejo a uno nuevo se pueda hacer de forma transparente al
    validar un login.
    """

    @abc.abstractmethod
    def hash(self, password: str) -> str:
        raise NotImplementedError

    @abc.abstractmethod
    def verify(self, password: str, hashed: str) -> bool:
        """
        Si la contraseña coincide. No lanza si el hash es de otro algoritmo: devuelve `False`.

        La implementación tiene que comparar en tiempo constante. Y el flujo de sign-in tiene
        que llamar a `hash` con una contraseña señuelo cuando **no encuentra** la fila del
        usuario, para que el tiempo de respuesta no delate si el mail existe.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def needs_rehash(self, hashed: str) -> bool:
        """Si el hash es de un algoritmo o coste viejo y conviene regenerarlo al próximo login."""
        raise NotImplementedError

    def hash_dummy(self) -> None:
        """
        Hashea una contraseña señuelo, para igualar el tiempo cuando el usuario **no existe**.

        Está en el puerto y no sólo en el adaptador porque es un **requisito de contrato**: el
        flujo de sign-in lo llama en la rama "no encontré la fila", y sin él responder
        "credenciales inválidas" tarda microsegundos para un mail inexistente y decenas de
        milisegundos para uno real. Esa diferencia enumera usuarios registrados sin adivinar ni
        una contraseña.

        Es **concreto** y no abstracto: el default correcto es hashear una constante, así que
        obligar a cada implementador a reescribirlo sólo agrega la oportunidad de olvidarlo — y
        olvidarlo no rompe ningún test funcional, sólo abre el oráculo.
        """
        self.hash(_SENUELO)


class AbstractUserRepository(abc.ABC):
    """Persistencia de usuarios. Deliberadamente angosto: sólo lo que los flujos de auth usan."""

    @abc.abstractmethod
    async def get_by_id(self, user_id: UUID) -> "User | None":
        raise NotImplementedError

    @abc.abstractmethod
    async def get_by_email(self, email: str) -> "User | None":
        """`email` ya viene normalizado por `Email`. No normalizar acá de nuevo."""
        raise NotImplementedError

    @abc.abstractmethod
    async def add(self, user: "User") -> "User":
        raise NotImplementedError

    @abc.abstractmethod
    async def update(self, user: "User") -> "User":
        raise NotImplementedError

    @abc.abstractmethod
    async def bump_token_generation(self, user_id: UUID) -> int:
        """
        Incrementa `token_generation` y devuelve el valor nuevo.

        Tiene que ser **un solo UPDATE atómico**, no leer-sumar-escribir: dos revocaciones
        masivas concurrentes con read-modify-write dejarían una de las dos sin efecto, y el
        efecto que se pierde es "cerrá todas las sesiones de este usuario".
        """
        raise NotImplementedError


class AbstractSessionRepository(abc.ABC):
    """Persistencia de sesiones. Es el puerto que hace posible la revocación."""

    @abc.abstractmethod
    async def get(self, session_id: UUID) -> "IdentitySession | None":
        raise NotImplementedError

    @abc.abstractmethod
    async def get_by_token_hash(self, token_hash: str) -> "IdentitySession | None":
        """
        Busca por el **hash** del token, nunca por el token en claro.

        Las filas guardan `token_hash` y no el token: un dump de la tabla de sesiones no
        puede ser un set de credenciales utilizables.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def add(self, identity_session: "IdentitySession") -> "IdentitySession":
        raise NotImplementedError

    @abc.abstractmethod
    async def revoke(self, session_id: UUID, *, at: datetime, reason: str) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    async def revoke_family(self, family_id: UUID, *, at: datetime, reason: str) -> int:
        """
        Revoca el linaje entero de rotación. Devuelve cuántas revocó.

        Se usa ante un reuso de refresh token: si el atacante y el usuario legítimo tienen
        los dos un token de la familia, revocar uno solo deja al otro adentro.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def consume_for_rotation(
        self, session_id: UUID, *, at: datetime
    ) -> "IdentitySession | None":
        """
        Marca la sesión como consumida y la devuelve, o `None` si ya estaba consumida.

        **Tiene que ser una sola sentencia atómica** del tipo
        ``UPDATE ... WHERE consumed_at IS NULL RETURNING``. Con leer-y-después-escribir, dos
        refresh concurrentes con el mismo token pasan los dos y la detección de reuso —que es
        el único mecanismo que detecta un token robado— no dispara nunca.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def list_active_for_user(self, user_id: UUID) -> list["IdentitySession"]:
        """Para el "listar mis sesiones" de una pantalla de seguridad."""
        raise NotImplementedError

    @abc.abstractmethod
    async def delete_expired(self, *, before: datetime) -> int:
        """Barrido del reaper. Devuelve cuántas borró."""
        raise NotImplementedError


class AbstractAccountRepository(abc.ABC):
    """Cuentas externas (OAuth) y la credencial local."""

    @abc.abstractmethod
    async def get_by_provider(
        self, provider_id: str, account_id: str
    ) -> "Account | None":
        raise NotImplementedError

    @abc.abstractmethod
    async def get_credential(self, user_id: UUID) -> "Account | None":
        """La cuenta del provider ``credential``, que es donde vive el hash de la contraseña."""
        raise NotImplementedError

    @abc.abstractmethod
    async def list_for_user(self, user_id: UUID) -> list["Account"]:
        raise NotImplementedError

    @abc.abstractmethod
    async def add(self, account: "Account") -> "Account":
        raise NotImplementedError

    @abc.abstractmethod
    async def update(self, account: "Account") -> "Account":
        raise NotImplementedError

    @abc.abstractmethod
    async def delete(self, account_id: UUID) -> None:
        raise NotImplementedError


class AbstractVerificationRepository(abc.ABC):
    """
    Tokens de un solo uso: verificación de mail, reset de contraseña, OTP.

    Es también la tabla que reusan los plugins que necesitan un token efímero en vez de aportar
    una propia, y por eso `purpose` es abierto. Ver `VerificationPurpose`.
    """

    @abc.abstractmethod
    async def add(self, verification: "Verification") -> "Verification":
        raise NotImplementedError

    @abc.abstractmethod
    async def consume(
        self,
        identifier: str,
        purpose: VerificationPurpose,
        value_hash: str,
        *,
        at: datetime,
    ) -> "Verification | None":
        """
        Canjea un token y lo marca consumido, o devuelve `None`.

        Atómico por el mismo motivo que `consume_for_rotation`: si no, el mismo magic link
        sirve dos veces y "de un solo uso" es una afirmación falsa.

        Se pide `purpose` además del identificador para que un código emitido para resetear
        la contraseña no se pueda canjear en el flujo de verificar el mail.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def increment_attempts(self, verification_id: UUID) -> int:
        """Cuenta intentos fallidos, para ponerle techo a la fuerza bruta sobre un OTP de 6 dígitos."""
        raise NotImplementedError

    @abc.abstractmethod
    async def invalidate_for(
        self, identifier: str, purpose: VerificationPurpose, *, at: datetime
    ) -> int:
        """
        Invalida los pendientes de ese identificador y propósito.

        Se llama al emitir uno nuevo: si no, cincuenta clicks en "reenviar" dejan cincuenta
        códigos válidos y el espacio a adivinar se multiplica por cincuenta.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def delete_expired(self, *, before: datetime) -> int:
        raise NotImplementedError


class AbstractRevocationList(abc.ABC):
    """
    Denylist de sesiones revocadas, para no pegarle a la base en el camino caliente.

    Semántica: **si no está en la lista, se permite.** Es correcto porque la entrada tiene que
    cubrir toda la vida restante de cualquier token que lleve ese `sid`.

    Falla **cerrando**: si el backend no responde, la implementación por defecto rechaza.
    Es al revés que `rate_limit`, y la diferencia es a propósito — dejar pasar una petición
    sin limitar es una molestia, dejar pasar un token revocado es la vulnerabilidad que esta
    clase existe para evitar.
    """

    @abc.abstractmethod
    async def revoke(self, session_id: UUID, *, until: datetime) -> None:
        """
        Marca la sesión como revocada hasta `until`.

        El vencimiento va **dentro del valor**, no delegado al TTL del backend: `MemoryCache`
        ignora su parámetro `expire` y nunca desaloja, así que una revocación con TTL sería
        permanente con el backend por defecto y la lista crecería sin techo.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def is_revoked(self, session_id: UUID) -> bool:
        raise NotImplementedError


class AbstractAuditSink(abc.ABC):
    """
    Dónde se escribe la auditoría.

    Es un puerto aparte del bus de eventos a propósito: los eventos son *notificaciones* y
    pueden perderse, reordenarse o procesarse en otro proceso. La auditoría de una
    impersonación tiene que escribirse **en la misma transacción** que el cambio de estado
    que registra, o existe la ventana donde la acción ocurrió y el registro no.
    """

    @abc.abstractmethod
    async def record(
        self,
        *,
        action: str,
        actor_id: UUID | str | None,
        subject_id: UUID | str | None,
        impersonated: bool = False,
        request_id: str | None = None,
        metadata: t.Mapping[str, t.Any] | None = None,
    ) -> None:
        raise NotImplementedError
