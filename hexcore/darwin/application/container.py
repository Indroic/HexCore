"""
Contenedor de Darwin: `configure_identity` / `get_identity_container` / `reset_identity`.

Copia la forma de `hexcore.infrastructure.api.cqrs.CQRSContainer` **a propósito**, hasta en los
detalles que parecen accidentales:

- **`RLock` y propiedades perezosas cacheadas.** `configure_identity()` se puede llamar en
  import time sin tocar la base ni generar claves; el trabajo real ocurre al primer uso.
  Reentrante y no `Lock` porque un componente construye otros (el servicio de sesión pide el
  emisor, que pide el almacén de claves) y todo pasa por el mismo lock.
- **`RuntimeError` con la remediación copiable** cuando nadie configuró. El mensaje dice la
  línea exacta que falta, no "no configurado".
- **`reset_identity()`** para tests y para reconfigurar en un worker.
- **`provide_*` triviales.** Su valor no es lo que hacen: es ser un objeto sobre el que hacer
  `app.dependency_overrides[provide_identity] = ...`.

Que la forma sea la misma no es simetría estética: quien ya sabe cablear CQRS en HexCore no
tiene que aprender un segundo patrón, y quien lea los dos ve que son la misma idea.
"""
from __future__ import annotations

import threading
import typing as t

if t.TYPE_CHECKING:
    from hexcore.darwin.application.config import IdentityConfig
    from hexcore.darwin.application.services import IdentityService, SessionService
    from hexcore.darwin.domain.ports import (
        AbstractAccountRepository,
        AbstractAuditSink,
        AbstractClock,
        AbstractPasswordHasher,
        AbstractRevocationList,
        AbstractSessionRepository,
        AbstractUserRepository,
        AbstractVerificationRepository,
    )
    from hexcore.darwin.infrastructure.envelope import (
        AuthEnvelopeCodec,
        AuthEnvelopeRestorer,
    )
    from hexcore.darwin.infrastructure.keys import AbstractKeyStore
    from hexcore.darwin.infrastructure.tokens import (
        JoserfcTokenIssuer,
        JoserfcTokenVerifier,
    )
    from hexcore.domain.events import EventBus

__all__ = [
    "IdentityContainer",
    "configure_identity",
    "get_identity_container",
    "reset_identity",
    "provide_identity",
    "provide_session_service",
    "provide_identity_config",
]

_NO_CONFIGURADO = (
    "Darwin no está configurado. Llamá a "
    "`hexcore.darwin.configure_identity(...)` al arrancar la aplicación (o el worker), "
    "antes de resolver cualquier servicio:\n\n"
    "    from hexcore.darwin import IdentityConfig, configure_identity\n\n"
    "    configure_identity(IdentityConfig())\n\n"
    "Si esto salta en un test, usá el fixture del kit de testing o llamá a "
    "`reset_identity()` en el teardown para no filtrar el contenedor entre tests."
)


class IdentityContainer:
    """
    Contenedor perezoso de los componentes de identidad.

    Todo lo que construye es reemplazable por inyección: los repositorios, el hasher, el reloj,
    el almacén de claves y la denylist entran por el constructor. Sin nada, arma los adaptadores
    por defecto — que requieren el extra `[darwin]`, y por eso se importan perezosamente y no
    arriba.

    Uso::

        contenedor = configure_identity(IdentityConfig())

        # en un handler o una dependencia
        servicio = get_identity_container().identity_service()
    """

    def __init__(
        self,
        config: "IdentityConfig",
        *,
        users: "AbstractUserRepository | None" = None,
        sessions: "AbstractSessionRepository | None" = None,
        accounts: "AbstractAccountRepository | None" = None,
        verifications: "AbstractVerificationRepository | None" = None,
        hasher: "AbstractPasswordHasher | None" = None,
        clock: "AbstractClock | None" = None,
        key_store: "AbstractKeyStore | None" = None,
        revocations: "AbstractRevocationList | None" = None,
        audit: "AbstractAuditSink | None" = None,
        events: "EventBus | None" = None,
    ) -> None:
        self._config = config
        self._lock = threading.RLock()

        # Inyectados (o `None`, y entonces se construye el default al primer uso).
        self._users = users
        self._sessions_repo = sessions
        self._accounts = accounts
        self._verifications = verifications
        self._hasher = hasher
        self._clock = clock
        self._key_store = key_store
        self._revocations = revocations
        self._audit = audit
        self._events = events

        # Cacheados.
        self._issuer: "JoserfcTokenIssuer | None" = None
        self._verifier: "JoserfcTokenVerifier | None" = None
        self._session_service: "SessionService | None" = None
        self._identity_service: "IdentityService | None" = None
        self._envelope_codec: "AuthEnvelopeCodec | None" = None
        self._envelope_restorer: "AuthEnvelopeRestorer | None" = None

    @property
    def config(self) -> "IdentityConfig":
        return self._config

    # ── Puertos ───────────────────────────────────────────────────────────────
    def clock(self) -> "AbstractClock":
        with self._lock:
            if self._clock is None:
                from hexcore.darwin.infrastructure.clock import SystemClock

                self._clock = SystemClock()
            return self._clock

    def hasher(self) -> "AbstractPasswordHasher":
        with self._lock:
            if self._hasher is None:
                from hexcore.darwin.infrastructure.hashing import Argon2PasswordHasher

                self._hasher = Argon2PasswordHasher()
            return self._hasher

    def key_store(self) -> "AbstractKeyStore":
        """
        El almacén de claves.

        El default es `StaticKeyStore` con **una clave generada al arrancar**, y eso sirve para
        desarrollo y tests pero **no para producción**: la clave cambia en cada arranque, así
        que un reload invalida todas las sesiones, y con más de un proceso cada uno firma con
        una clave distinta. En producción se inyecta un almacén persistido.
        """
        with self._lock:
            if self._key_store is None:
                from hexcore.darwin.infrastructure.keys import (
                    StaticKeyStore,
                    generate_signing_key,
                )

                self._key_store = StaticKeyStore(
                    [generate_signing_key(algorithm=self._config.tokens.algorithm)]
                )
            return self._key_store

    def revocations(self) -> "AbstractRevocationList":
        with self._lock:
            if self._revocations is None:
                from hexcore.darwin.infrastructure.revocation import (
                    CacheRevocationList,
                )

                self._revocations = CacheRevocationList(clock=self.clock())
            return self._revocations

    def users(self) -> "AbstractUserRepository":
        with self._lock:
            if self._users is None:
                from hexcore.darwin.infrastructure.repositories import (
                    SqlAlchemyUserRepository,
                )

                self._users = SqlAlchemyUserRepository(model=self._config.user_model)
            return self._users

    def sessions_repository(self) -> "AbstractSessionRepository":
        with self._lock:
            if self._sessions_repo is None:
                from hexcore.darwin.infrastructure.repositories import (
                    SqlAlchemySessionRepository,
                )

                self._sessions_repo = SqlAlchemySessionRepository()
            return self._sessions_repo

    def accounts(self) -> "AbstractAccountRepository":
        with self._lock:
            if self._accounts is None:
                from hexcore.darwin.infrastructure.repositories import (
                    SqlAlchemyAccountRepository,
                )

                self._accounts = SqlAlchemyAccountRepository()
            return self._accounts

    def verifications(self) -> "AbstractVerificationRepository":
        with self._lock:
            if self._verifications is None:
                from hexcore.darwin.infrastructure.repositories import (
                    SqlAlchemyVerificationRepository,
                )

                self._verifications = SqlAlchemyVerificationRepository()
            return self._verifications

    def events(self) -> "EventBus | None":
        """
        El bus de eventos de dominio.

        Se toma de `ServerConfig.event_bus` si no se inyectó, porque es donde el resto del
        framework lo publica — el UoW hace lo mismo. Devuelve `None` si no hay: los eventos son
        notificaciones, y un flujo de auth no debe fallar porque nadie escuche.
        """
        with self._lock:
            if self._events is None:
                try:
                    from hexcore.config import LazyConfig

                    self._events = LazyConfig.get_config().event_bus
                except Exception:
                    return None
            return self._events

    # ── Tokens ────────────────────────────────────────────────────────────────
    def issuer(self) -> "JoserfcTokenIssuer":
        with self._lock:
            if self._issuer is None:
                from hexcore.darwin.infrastructure.tokens import (
                    JoserfcTokenIssuer,
                    TokenTtl,
                )

                self._issuer = JoserfcTokenIssuer(
                    issuer=self._config.tokens.issuer,
                    key_store=self.key_store(),
                    clock=self.clock(),
                    ttl=TokenTtl(
                        access=self._config.tokens.access_ttl,
                        refresh=self._config.tokens.refresh_ttl,
                    ),
                )
            return self._issuer

    def verifier(self) -> "JoserfcTokenVerifier":
        with self._lock:
            if self._verifier is None:
                from hexcore.darwin.infrastructure.tokens import JoserfcTokenVerifier

                self._verifier = JoserfcTokenVerifier(
                    issuer=self._config.tokens.issuer,
                    key_store=self.key_store(),
                    clock=self.clock(),
                    # **Sólo** el algoritmo configurado. La allowlist por defecto acepta cinco;
                    # acá se estrecha al que de verdad se usa, que es la configuración más
                    # restrictiva posible sin dejar de funcionar.
                    allowed_algorithms=[self._config.tokens.algorithm],
                    leeway=self._config.tokens.leeway,
                )
            return self._verifier

    # ── El sobre que cruza la cola ────────────────────────────────────────────
    def envelope_codec(self) -> "AuthEnvelopeCodec":
        """
        El códec del sobre firmado.

        Usa `config.secret_key` (simétrica) y no la clave del JWKS: el sobre lo produce y lo
        consume el mismo despliegue, así que no hay verificador de terceros al que publicarle
        una clave pública, y un HMAC es mucho más barato en un camino que corre por cada
        mensaje encolado.
        """
        with self._lock:
            if self._envelope_codec is None:
                from hexcore.darwin.infrastructure.envelope import AuthEnvelopeCodec

                clave = self._config.secret_key
                if clave is None:  # pragma: no cover - `IdentityConfig` ya lo garantiza
                    raise RuntimeError(
                        "Darwin no tiene clave de firma, así que no puede sellar el sobre "
                        "que cruza la cola. Declará HEXCORE_DARWIN_SECRET_KEY."
                    )
                self._envelope_codec = AuthEnvelopeCodec(
                    secret=clave.get_secret_value(),
                    clock=self.clock(),
                    ttl=self._config.worker_context_ttl,
                )
            return self._envelope_codec

    def envelope_restorer(self) -> "AuthEnvelopeRestorer":
        """El restaurador que el worker usa para reabrir el sobre y revalidar la sesión."""
        with self._lock:
            if self._envelope_restorer is None:
                from hexcore.darwin.infrastructure.envelope import AuthEnvelopeRestorer

                self._envelope_restorer = AuthEnvelopeRestorer(
                    codec=self.envelope_codec(),
                    sessions=self.sessions_repository(),
                    clock=self.clock(),
                )
            return self._envelope_restorer

    # ── Servicios ─────────────────────────────────────────────────────────────
    def session_service(self) -> "SessionService":
        with self._lock:
            if self._session_service is None:
                from hexcore.darwin.application.services import SessionService

                self._session_service = SessionService(
                    sessions=self.sessions_repository(),
                    users=self.users(),
                    issuer=self.issuer(),
                    verifier=self.verifier(),
                    revocations=self.revocations(),
                    clock=self.clock(),
                    config=self._config,
                    events=self.events(),
                    audit=self._audit,
                )
            return self._session_service

    def identity_service(self) -> "IdentityService":
        with self._lock:
            if self._identity_service is None:
                from hexcore.darwin.application.services import IdentityService

                self._identity_service = IdentityService(
                    users=self.users(),
                    accounts=self.accounts(),
                    verifications=self.verifications(),
                    sessions=self.session_service(),
                    hasher=self.hasher(),
                    clock=self.clock(),
                    config=self._config,
                    events=self.events(),
                )
            return self._identity_service


_container: IdentityContainer | None = None
_container_lock = threading.RLock()


def configure_identity(
    config: "IdentityConfig | None" = None,
    **componentes: t.Any,
) -> IdentityContainer:
    """
    Configura Darwin en el proceso. Llamalo una vez, al arrancar.

    Args:
        config: La configuración. Por defecto, la de `ServerConfig.darwin` si está, o una
            `IdentityConfig()` — que en producción **falla** si no hay clave de firma, y eso es
            deliberado.
        **componentes: Cualquier puerto a inyectar (`users=`, `clock=`, `key_store=`, …). Es lo
            que usan los tests y lo que permite persistir las claves en producción.

    Returns:
        El contenedor, por si lo querés usar directo.

    Reconfigurar **reemplaza** el contenedor entero en vez de mutarlo: mutar dejaría los
    servicios ya cacheados apuntando a los componentes viejos, y el síntoma sería que la
    reconfiguración "no tomó" en la mitad de las llamadas.

    Uso::

        from hexcore.darwin import IdentityConfig, configure_identity

        configure_identity(IdentityConfig(), key_store=mi_almacen_persistido)
    """
    from hexcore.darwin.application.config import IdentityConfig
    from hexcore.darwin.infrastructure.schema import validate_user_model

    global _container

    if config is None:
        try:
            from hexcore.config import LazyConfig

            config = getattr(LazyConfig.get_config(), "darwin", None)
        except Exception:
            config = None
        if config is None:
            config = IdentityConfig()

    # Al **configurar**, no en el primer login. Rechaza un `BaseModel[T]` (que explotaría
    # después del commit) y una clase que no componga `UserMixin`. Mismo criterio que
    # `CQRSFactory._assert_enqueuer_for_background_commands`.
    if config.user_model is not None:
        validate_user_model(config.user_model)

    with _container_lock:
        _container = IdentityContainer(config, **componentes)
        _registrar_el_sobre()
        return _container


def get_identity_container() -> IdentityContainer:
    """
    El contenedor configurado.

    Raises:
        RuntimeError: si nadie llamó a `configure_identity()`, con la línea que falta.
    """
    with _container_lock:
        if _container is None:
            raise RuntimeError(_NO_CONFIGURADO)
        return _container


def reset_identity() -> None:
    """
    Descarta el contenedor **y** el cableado del sobre. Para tests y para reconfigurar.

    Deregistra el proveedor y el restaurador porque son estado global del **núcleo**, no del
    contenedor: dejarlos puestos haría que un test posterior sellara un sobre contra un
    contenedor que ya no existe, y el error saldría en el encolado de otro test.
    """
    global _container

    from hexcore.darwin.infrastructure.envelope import ENVELOPE_KEY
    from hexcore.domain.cqrs.envelope import unregister_envelope_key

    with _container_lock:
        _container = None
        unregister_envelope_key(ENVELOPE_KEY)


def _registrar_el_sobre() -> None:
    """
    Registra el proveedor y el restaurador del sobre en el núcleo.

    Se hace al **configurar** y no al importar: un proceso que importa Darwin pero no lo
    cablea no debe empezar a sellar sobres que nadie va a poder abrir. Y se hace acá y no en
    los transportes porque el sobre es una propiedad del proceso: los cinco transportes
    tienen que sellar igual sin que nadie los configure de a uno.

    Los dos objetos que se registran resuelven el contenedor **en cada uso**, así que esto no
    construye el códec ni toca la clave de firma — `configure_identity()` sigue siendo
    perezoso.
    """
    from hexcore.darwin.infrastructure.envelope import (
        AUTH_RESTORER,
        ENVELOPE_KEY,
        auth_envelope_provider,
    )
    from hexcore.domain.cqrs.envelope import (
        register_envelope_metadata_provider,
        register_envelope_restorer,
    )

    register_envelope_metadata_provider(ENVELOPE_KEY, auth_envelope_provider)
    register_envelope_restorer(ENVELOPE_KEY, AUTH_RESTORER)


# ── Dependencias FastAPI ──────────────────────────────────────────────────────
# Deliberadamente triviales: su valor está en ser un objeto sobre el que hacer
# `app.dependency_overrides[...] = ...`.


def provide_identity() -> "IdentityService":
    """Dependencia FastAPI: el `IdentityService` del proceso."""
    return get_identity_container().identity_service()


def provide_session_service() -> "SessionService":
    """Dependencia FastAPI: el `SessionService` del proceso."""
    return get_identity_container().session_service()


def provide_identity_config() -> "IdentityConfig":
    """Dependencia FastAPI: la `IdentityConfig` vigente."""
    return get_identity_container().config
