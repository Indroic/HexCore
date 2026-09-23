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

from pydantic import BaseModel, ConfigDict

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
    "AbstractPrincipalResolver",
    "NullPrincipalResolver",
    "CompositePrincipalResolver",
    "ResolvedPrincipal",
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
    async def get_by_username(self, username: str) -> "User | None":
        """
        `username` ya viene normalizado por `UsernamePolicy.normalize`. No normalizar de nuevo.

        Mismo contrato que `get_by_email` y por el mismo motivo: si cada adaptador normalizara
        por su cuenta, dos backends podrían discrepar y el mismo usuario existiría en uno y no
        en el otro.
        """
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
        Marca la sesión como consumida y la devuelve, o `None` si no se pudo: ya estaba
        consumida, revocada, o es una sesión impersonada.

        **Tiene que ser una sola sentencia atómica** del tipo
        ``UPDATE ... WHERE consumed_at IS NULL AND revoked_at IS NULL AND actor_user_id =
        subject_user_id RETURNING``. Con leer-y-después-escribir, dos refresh concurrentes con
        el mismo token pasan los dos y la detección de reuso —que es el único mecanismo que
        detecta un token robado— no dispara nunca.

        Las tres condiciones en el `WHERE` —y no sólo `consumed_at`— son lo que le permite a
        `SessionService.refresh()` intentar la rotación **sin leer la fila antes**: en el
        camino feliz (la inmensa mayoría de los refresh) esta es la única consulta de sesión
        del rotado entero, en vez de un `get()` seguido de este mismo `UPDATE`. La condición de
        impersonación tiene que ir acá y no sólo chequearse en la aplicación *después* de leer,
        porque una vez consumida la fila queda inutilizable por lo que le queda de vida — si el
        chequeo llegara tarde, una sesión impersonada perdería la suya por el intento.
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


class ResolvedPrincipal(BaseModel):
    """
    Lo que la app sabe de un usuario y Darwin no: sus roles, sus permisos y su estado.

    Es un modelo y no una tupla **porque va a crecer**. Empezó siendo `(roles, scopes)` y a la
    semana hizo falta el estado; con una tupla, cada campo nuevo rompe la firma de todos los
    resolvers escritos hasta ese momento. Con un modelo de campos opcionales, agregar uno no
    rompe a nadie.

    `status` es un `str` libre y **Darwin no lo interpreta**: no sabe si "pending" puede entrar
    ni si "banned" puede leer. Sólo lo transporta hasta el `AuthContext`, donde la app lo lee
    sin volver a consultar la base. Cerrarlo a un `Literal` obligaría al framework a conocer
    los estados de cada consumidor, que es exactamente lo que no puede saber.
    """

    model_config = ConfigDict(frozen=True)

    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()

    #: El estado de la cuenta **según la app**, no según Darwin.
    #:
    #: Viaja en el token, así que leerlo no consulta nada — y por eso puede estar hasta un
    #: `access_ttl` desactualizado (120 s por defecto): se vuelve a resolver en cada rotación.
    #: Si un cambio de estado tiene que echar a alguien **ya**, el flujo que lo cambia llama
    #: además a `SessionService.revoke_all_for`, que sube la generación e invalida el cache.
    status: str | None = None


class AbstractPrincipalResolver(abc.ABC):
    """
    De dónde salen los roles y los scopes de un usuario.

    Existe porque **no había forma de poblarlos por HTTP**. `Principal` tiene `roles` y
    `scopes` desde siempre, y `AccessTokenClaims` transporta los scopes, pero el único camino
    para llenarlos era llamar a `IdentityService.sign_in(scopes=...)` a mano: la ruta
    `POST /auth/sign-in` no los pasa, así que toda sesión abierta por el router salía con los
    dos conjuntos vacíos y `auth.require_scopes(...)` rechazaba a todo el mundo.

    El hook `SIGN_IN_AUTHENTICATED` tampoco alcanzaba. Es un hook `before` sobre el usuario:
    puede reemplazarlo o abortar el login, pero corre **antes** de que la sesión exista y no
    tiene dónde depositar unos permisos. Servía para exigir un segundo factor, no para decidir
    qué puede hacer alguien.

    Es un puerto y no un callable en la config por lo mismo que el resto de los puertos del
    módulo: se puede testear solo, se puede declarar su contrato, y `configure_identity` lo
    valida al cablear en vez de fallar en el primer login.

    **Dónde se llama**, y esto es lo que hay que tener presente al implementarlo:

    - En el sign-in, una vez.
    - En **cada rotación de refresh**, o sea cada `access_ttl` (120 s por defecto) mientras la
      sesión esté activa. Re-resolver es deliberado: es lo que hace que quitarle un rol a
      alguien tenga efecto sin esperar a que cierre sesión. La contracara es que esto está en
      un camino caliente-ish, así que una implementación que pegue tres queries por llamada se
      va a notar. Cachear adentro es válido y esperable.

    Si el corte tiene que ser inmediato y no "dentro de dos minutos", la herramienta es
    `bump_token_generation`, que invalida todos los tokens del usuario de una.

    Uso::

        class PrincipalesDeLaApp(AbstractPrincipalResolver):
            def __init__(self, uow_scope):
                self._uow_scope = uow_scope

            async def resolve(self, user):
                async with self._uow_scope() as uow:
                    fila = await uow.membresias.get_by_user(user.id)
                return ResolvedPrincipal(
                    roles=frozenset(fila.roles),
                    scopes=frozenset(fila.permisos),
                    status=fila.status,
                )

        configure_identity(IdentityConfig(), principals=PrincipalesDeLaApp(uow_scope))
    """

    @abc.abstractmethod
    async def resolve(self, user: "User") -> "ResolvedPrincipal":
        """
        Los roles, scopes y estado de ese usuario.

        Devolver un `ResolvedPrincipal()` vacío es válido y es lo que hace el default:
        significa "esta app no usa permisos de Darwin", no "este usuario no puede nada".

        **Lanzar desde acá aborta el flujo**, y es el mecanismo para un estado que no puede
        seguir. Lanzá una `IdentityError` —cualquier otra excepción escapa al mapeo del módulo
        y sale como 500. Ojo con dónde cae: en una rotación esto corre **después** de consumir
        la fila de sesión, así que el usuario queda deslogueado, que suele ser lo que se quiere
        para una cuenta suspendida.
        """
        raise NotImplementedError


class NullPrincipalResolver(AbstractPrincipalResolver):
    """
    El resolver por defecto: nadie tiene roles, scopes ni estado.

    Es el comportamiento de 9.x, y por eso es el default: una app que no declara permisos no
    empieza a recibirlos de golpe porque actualizó. Concreto y no `None` para que
    `SessionService` no tenga que preguntar si hay resolver en cada rotación.
    """

    async def resolve(self, user: "User") -> "ResolvedPrincipal":
        del user
        return ResolvedPrincipal()


class CompositePrincipalResolver(AbstractPrincipalResolver):
    """
    Combina varios `AbstractPrincipalResolver` en uno solo.

    `configure_identity(principals=...)` sólo tiene **un** slot (HC-16): no hay forma de
    cablear a la vez, por ejemplo, `RbacPrincipalResolver` —que resuelve roles/scopes y nunca
    toca `status`— con un resolver propio de la app que sólo sabe de `status` ("pending",
    "banned", lo que sea). Sin este puente, quien necesitara ambos tenía que reimplementar a
    mano lo que `RbacPrincipalResolver` ya hace, sólo para poder agregarle un `status`.

    Corre los resolvers en orden y los combina:

    - `roles` y `scopes`: unión de todos.
    - `status`: el primer resolver de la lista que devuelva algo distinto de `None` — no una
      unión, porque el estado de una cuenta no es un conjunto, es un valor. El orden importa:
      poné primero el resolver cuyo `status` tiene que ganar.

    Uso::

        configure_identity(
            IdentityConfig(),
            principals=CompositePrincipalResolver([
                rbac.principal_resolver(),      # roles y scopes
                EstadoDeCuentaResolver(uow),     # status
            ]),
        )
    """

    def __init__(self, resolvers: t.Sequence[AbstractPrincipalResolver]) -> None:
        self._resolvers = list(resolvers)

    async def resolve(self, user: "User") -> "ResolvedPrincipal":
        roles: set[str] = set()
        scopes: set[str] = set()
        status: str | None = None

        for resolver in self._resolvers:
            resuelto = await resolver.resolve(user)
            roles.update(resuelto.roles)
            scopes.update(resuelto.scopes)
            if status is None and resuelto.status is not None:
                status = resuelto.status

        return ResolvedPrincipal(
            roles=frozenset(roles), scopes=frozenset(scopes), status=status
        )
