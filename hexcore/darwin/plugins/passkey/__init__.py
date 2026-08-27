"""
`passkey`: WebAuthn, o sea entrar sin contraseña y sin nada que se pueda phishear.

Es el plugin con la mejor propiedad de seguridad de todo Darwin, y la propiedad es esta: **lo que
se guarda es público**. No hay nada en `darwin_passkey` que un atacante con un dump de la base
pueda usar para autenticarse, ni acá ni en otro sitio. Comparalo con lo demás:

| Método | Qué se guarda | Un dump sirve para… |
| :-- | :-- | :-- |
| Contraseña | hash de Argon2id | atacar por diccionario, offline |
| TOTP | secreto compartido, cifrado | generar códigos, si además tenés la clave de la app |
| **Passkey** | **clave pública** | **nada** |

Y encima está atada al origen por el navegador, así que un sitio clonado no puede reenviar la
aserción. Es la razón por la que este plugin no tiene ningún secreto que proteger.

⚠️ **El contador de firmas es la única señal de compromiso que WebAuthn da.** Un contador que no
avanza significa autenticador clonado o aserción replayeada. Muchas implementaciones lo descartan
porque "algunos autenticadores no lo incrementan"; acá se distingue el que nunca lo usa (contador 0
siempre, se acepta) del que lo usaba y dejó de avanzar (se rechaza y se corta la sesión).

**Por qué acá hay una dependencia y en `two_factor` no.** El TOTP son treinta líneas de `hmac`.
WebAuthn es CBOR, claves COSE, cuatro formatos de attestation, cadenas de certificados y un
contador — escribirlo a mano sería criptografía propia en el camino de autenticación. Va
`py_webauthn` en el extra `[darwin-passkey]`, detrás de un puerto, así que los tests del flujo
corren sin el extra y sin hardware.

Requiere los extras `[darwin]`, `[api]` y **`[darwin-passkey]`**.

Uso::

    from hexcore.darwin import PluginRegistry, configure_identity
    from hexcore.darwin.plugins.passkey import PasskeyPlugin

    plugins = PluginRegistry([
        PasskeyPlugin(
            rp_id="mi-app.com",
            rp_name="Mi App",
            origins=["https://mi-app.com"],
            # Para login sin contraseña conviene exigir PIN o biometría.
            require_user_verification=True,
        )
    ])
    configure_identity(config, plugins=plugins)

    app = create_app(
        features=AppFeatures(auth_context=True, csrf=True),
        routers=[build_identity_router(), *plugins.routers()],
    )
"""
from __future__ import annotations

import threading
import typing as t
from datetime import timedelta

from hexcore.darwin.domain.plugins import DarwinPlugin
from hexcore.darwin.plugins.passkey.domain import (
    PASSKEY_EXCEPTION_STATUS_MAP,
    AbstractPasskeyChallengeRepository,
    AbstractPasskeyRepository,
    AbstractWebAuthnVerifier,
    Passkey,
    PasskeyAlreadyRegisteredError,
    PasskeyChallenge,
    PasskeyChallengeError,
    PasskeyClonedAuthenticatorError,
    PasskeyError,
    PasskeyLastFactorError,
    PasskeyNotFoundError,
    PasskeyVerificationError,
    RegisteredCredential,
    VerifiedAssertion,
)

if t.TYPE_CHECKING:
    # Sólo para el checker: en runtime los resuelve el `__getattr__` de abajo, porque importarlos
    # arrastra sqlalchemy y nombrar el plugin no puede exigir el extra `[darwin-sqlalchemy]`.
    from hexcore.darwin.plugins.passkey.orms.sqlalchemy.models_mixins import (
        PasskeyChallengeMixin as PasskeyChallengeMixin,
        PasskeyMixin as PasskeyMixin,
    )
    from hexcore.darwin.plugins.passkey.service import PasskeyService

__all__ = [
    "PasskeyPlugin",
    "Passkey",
    "PasskeyChallenge",
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
    "PasskeyMixin",
    "PasskeyChallengeMixin",
    "get_passkey_service",
]


class PasskeyPlugin(DarwinPlugin):
    """
    El plugin de passkeys.

    Args:
        rp_id: El dominio de la Relying Party (`"mi-app.com"`), sin esquema ni puerto. **Cambiarlo
            invalida todas las credenciales registradas**: el navegador las ata al `rp_id`, así que
            va elegido de una vez y no se toca.
        rp_name: El nombre que muestra el navegador en el diálogo.
        origins: Los orígenes completos permitidos. **Obligatorio** si no pasás tu propio
            `verifier`: es el chequeo que hace a WebAuthn resistente al phishing.
        require_user_verification: `True` para login sin contraseña (exige PIN o biometría);
            `False` si las passkeys son un segundo factor y querés aceptar llaves sin PIN.
        challenge_ttl: Cuánto vive un desafío.
        verifier: Un `AbstractWebAuthnVerifier` propio. Por defecto, el de `py_webauthn`.
        include_router: Si aporta su router.
    """

    name = "passkey"
    #: Los nombres que devuelve `tables()`, para que el registro valide el conflicto
    #: de homónimos sin importar sqlalchemy. Un test verifica que coincidan.
    contributed_tables = ("PasskeyMixin", "PasskeyChallengeMixin")

    #: Con `two_factor` (20) y `oauth` (30): es un método de autenticación primaria, así que su
    #: lugar es entre los que autentican y antes de `impersonate` (60).
    priority = 40

    def __init__(
        self,
        *,
        rp_id: str | None = None,
        rp_name: str = "HexCore",
        origins: t.Sequence[str] = (),
        require_user_verification: bool = False,
        challenge_ttl: timedelta | None = None,
        verifier: AbstractWebAuthnVerifier | None = None,
        passkey_repository: AbstractPasskeyRepository | None = None,
        challenge_repository: AbstractPasskeyChallengeRepository | None = None,
        include_router: bool = True,
    ) -> None:
        if verifier is None and not rp_id:
            raise ValueError(
                "El plugin 'passkey' necesita `rp_id` (o un `verifier` propio).\n\n"
                '    PasskeyPlugin(rp_id="mi-app.com", origins=["https://mi-app.com"])'
            )

        self._rp_id = rp_id
        self._rp_name = rp_name
        self._origins = tuple(origins)
        self._uv = require_user_verification
        self._challenge_ttl = challenge_ttl
        self._verifier = verifier
        self._passkey_repository = passkey_repository
        self._challenge_repository = challenge_repository
        self._include_router = include_router
        self._lock = threading.RLock()
        self._service: "PasskeyService | None" = None

    # ── El servicio ───────────────────────────────────────────────────────────
    def service(self) -> "PasskeyService":
        """
        El servicio, construido perezosamente desde el contenedor de identidad.

        Perezoso y cacheado con `RLock`, igual que los proveedores del contenedor: el plugin se
        instancia al declarar el registro —antes de `configure_identity`— así que construirlo en
        `__init__` obligaría a un orden de cableado que nadie tiene por qué recordar.
        """
        with self._lock:
            if self._service is None:
                from hexcore.darwin.application.container import get_identity_container
                from hexcore.darwin.plugins.passkey.service import (
                    DEFAULT_CHALLENGE_TTL,
                    PasskeyService,
                )

                contenedor = get_identity_container()
                self._service = PasskeyService(
                    passkeys=self._passkey_repository
                    or self._repositorio_por_defecto(),
                    challenges=self._challenge_repository
                    or self._desafios_por_defecto(),
                    users=contenedor.users(),
                    accounts=contenedor.accounts(),
                    sessions=contenedor.session_service(),
                    clock=contenedor.clock(),
                    verifier=self._verifier or self._verificador_por_defecto(),
                    challenge_ttl=self._challenge_ttl or DEFAULT_CHALLENGE_TTL,
                )
            return self._service

    def _verificador_por_defecto(self) -> AbstractWebAuthnVerifier:
        from hexcore.darwin.plugins.passkey.webauthn_adapter import PyWebAuthnVerifier

        return PyWebAuthnVerifier(
            rp_id=t.cast(str, self._rp_id),
            rp_name=self._rp_name,
            origins=self._origins,
            require_user_verification=self._uv,
        )

    @staticmethod
    def _repositorio_por_defecto() -> AbstractPasskeyRepository:
        """El repositorio del backend que resolvió el contenedor. Ver `plugins/storage.py`."""
        from hexcore.darwin.plugins.storage import plugin_repositories

        return plugin_repositories("passkey").PasskeyRepository()

    @staticmethod
    def _desafios_por_defecto() -> AbstractPasskeyChallengeRepository:
        from hexcore.darwin.plugins.storage import plugin_repositories

        return plugin_repositories("passkey").PasskeyChallengeRepository()

    def reset(self) -> None:
        """Descarta el servicio cacheado. Para los tests, que reconfiguran el contenedor."""
        with self._lock:
            self._service = None

    # ── Lo que aporta ─────────────────────────────────────────────────────────
    def tables(self) -> t.Mapping[str, type]:
        from hexcore.darwin.plugins.passkey.orms.sqlalchemy.models_mixins import (
            PasskeyChallengeMixin,
            PasskeyMixin,
        )

        return {
            "PasskeyMixin": PasskeyMixin,
            "PasskeyChallengeMixin": PasskeyChallengeMixin,
        }

    def exception_status_map(self) -> t.Mapping[type[Exception], int]:
        return PASSKEY_EXCEPTION_STATUS_MAP

    def routers(self) -> t.Sequence[t.Any]:
        if not self._include_router:
            return ()

        from hexcore.darwin.plugins.passkey.router import build_passkey_router

        return [build_passkey_router()]

    def startup_steps(self) -> t.Sequence[t.Any]:
        """
        Un paso que avisa si el `rp_id` parece de desarrollo.

        Avisa y no falla: `localhost` es el único host que los navegadores aceptan sin HTTPS, y es
        exactamente lo que hay que poner en desarrollo. Pero shippear a producción con
        `rp_id="localhost"` deja el login roto para todos, con un error del navegador que no dice
        qué está mal.
        """
        return [_AvisarRpDeDesarrollo(self._rp_id)]


class _AvisarRpDeDesarrollo:
    """`StartupStep` que avisa si el `rp_id` es de desarrollo."""

    name = "darwin.passkey.rp_check"

    def __init__(self, rp_id: str | None) -> None:
        self._rp_id = rp_id

    async def __call__(self) -> None:
        import logging

        if self._rp_id in ("localhost", "127.0.0.1"):
            logging.getLogger("hexcore.darwin.passkey").warning(
                "El plugin 'passkey' tiene rp_id=%r, que sólo funciona en desarrollo. En "
                "producción tiene que ser tu dominio real: el navegador ata cada credencial al "
                "`rp_id`, así que con este valor nadie va a poder autenticarse — y el error que "
                "muestra el navegador no dice qué está mal.",
                self._rp_id,
            )


def get_passkey_service() -> "PasskeyService":
    """
    El servicio del plugin registrado en este despliegue.

    Se busca en el registro del contenedor y no en un global propio: los plugins son de un
    despliegue, y un segundo global tendría que resetearse aparte en cada test.

    Raises:
        RuntimeError: el plugin no está registrado, con la remediación copiable.
    """
    from hexcore.darwin.application.container import get_identity_container

    plugin = get_identity_container().plugins.get(PasskeyPlugin.name)
    if not isinstance(plugin, PasskeyPlugin):
        raise RuntimeError(
            "El plugin 'passkey' no está registrado en este despliegue.\n\n"
            "    from hexcore.darwin import PluginRegistry, configure_identity\n"
            "    from hexcore.darwin.plugins.passkey import PasskeyPlugin\n\n"
            "    configure_identity(config, plugins=PluginRegistry([\n"
            '        PasskeyPlugin(rp_id="mi-app.com", origins=["https://mi-app.com"])\n'
            "    ]))"
        )
    return plugin.service()


def __getattr__(name: str) -> t.Any:
    """
    Los dos mixins, perezosos: importarlos arrastra sqlalchemy.

    Están en `__all__` porque son parte de la API pública —el consumidor los compone en su paquete
    ``models/``— pero nombrar el plugin no puede exigir el extra `[darwin-sqlalchemy]`. Es el mismo patrón que la
    fachada de Darwin y que el plugin de OAuth.
    """
    if name in ("PasskeyMixin", "PasskeyChallengeMixin"):
        from hexcore.darwin.plugins.passkey.orms.sqlalchemy import models_mixins

        return getattr(models_mixins, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
