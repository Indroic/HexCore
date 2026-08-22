"""
`two_factor`: TOTP como segundo factor, con el sign-in partido en dos pasos.

Es el primer plugin que **intercepta** un flujo del núcleo en vez de agregar uno al costado, así
que es el que ejercita de verdad el sistema de la Fase 8. Cómo se engancha, y por qué así:

1. **Un hook en `user.sign_in.authenticated`.** Ese punto corre con la contraseña ya validada y
   la sesión **todavía no creada**, que es el único lugar donde un segundo factor puede
   exigirse: antes no se sabe quién es el usuario, y después ya hay un par de tokens emitido
   que habría que revocar. El hook lanza `TwoFactorRequiredError` y **no se emite nada**.
2. **Su propio router**, con los cinco endpoints del ciclo de vida más el segundo paso del
   login.
3. **Su propio mapa de excepciones**, vía `exception_status_map()`. Las excepciones viven en el
   plugin: el núcleo no tiene por qué conocer los modos de falla del 2FA.
4. **Un mixin de tabla**, que el consumidor compone en su paquete ``models/`` — igual que los
   del núcleo, y por el mismo motivo (un `--autogenerate` que no ve la tabla le emite
   ``op.drop_table``).

Lo que **no** hace, a propósito: no aporta códigos de respaldo. Un código de respaldo es una
credencial de un solo uso y alta entropía, o sea exactamente lo que `verification` ya modela —
así que va como un plugin aparte que dependa de este, no como una tabla nueva acá.

Requiere el extra `[darwin-two-factor]`, que no agrega paquetes sobre `[darwin]`: el TOTP
es `hmac` de la stdlib y el
cifrado del secreto reusa el JWE de `joserfc`, que ya está.

Uso::

    from hexcore.darwin import PluginRegistry, configure_identity
    from hexcore.darwin.plugins.two_factor import TwoFactorPlugin

    plugins = PluginRegistry([TwoFactorPlugin(issuer="Mi App")])
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

from hexcore.darwin.domain.plugins import DarwinPlugin, HookBinding
from hexcore.darwin.plugins.two_factor.domain import (
    MAX_FAILED_ATTEMPTS,
    TWO_FACTOR_EXCEPTION_STATUS_MAP,
    AbstractTwoFactorRepository,
    TwoFactor,
    TwoFactorAlreadyConfirmedError,
    TwoFactorError,
    TwoFactorInvalidCodeError,
    TwoFactorNotEnrolledError,
    TwoFactorRequiredError,
)

if t.TYPE_CHECKING:
    from hexcore.darwin.plugins.two_factor.service import TwoFactorService

__all__ = [
    "TwoFactorPlugin",
    "TwoFactor",
    "AbstractTwoFactorRepository",
    "TwoFactorError",
    "TwoFactorRequiredError",
    "TwoFactorInvalidCodeError",
    "TwoFactorNotEnrolledError",
    "TwoFactorAlreadyConfirmedError",
    "MAX_FAILED_ATTEMPTS",
    "TWO_FACTOR_EXCEPTION_STATUS_MAP",
    "get_two_factor_service",
]


class TwoFactorPlugin(DarwinPlugin):
    """
    El plugin del segundo factor.

    Args:
        issuer: El nombre que muestran las apps autenticadoras. Poné el de tu producto: un
            usuario con tres cuentas en apps distintas ve tres entradas y necesita distinguirlas.
        challenge_ttl: Cuánto vive el desafío del segundo paso.
        repository: Un `AbstractTwoFactorRepository` propio. Por defecto, el del backend
            que resolvió el contenedor.
        include_router: Si aporta su router. Apagalo si querés exponer los flujos con tus
            propias rutas y validaciones.
        rate_limit: `(intentos, ventana)` del endpoint que canjea el desafío. **No lo apagues**:
            es la ruta donde se prueban códigos de 6 dígitos, y el techo por fila
            (`MAX_FAILED_ATTEMPTS`) sólo protege a un usuario ya inscripto — el límite por IP es
            lo que corta a quien rota entre cuentas.
    """

    name = "two_factor"

    #: Corre temprano entre los hooks del sign-in: si falta el segundo factor, no tiene sentido
    #: que otros plugins hagan trabajo sobre un login que no va a completarse.
    priority = 20

    def __init__(
        self,
        *,
        issuer: str = "HexCore",
        challenge_ttl: timedelta | None = None,
        repository: "AbstractTwoFactorRepository | None" = None,
        include_router: bool = True,
        rate_limit: tuple[int, int] | None = (10, 300),
    ) -> None:
        self._issuer = issuer
        self._challenge_ttl = challenge_ttl
        self._repository = repository
        self._include_router = include_router
        self._rate_limit = rate_limit
        self._lock = threading.RLock()
        self._service: "TwoFactorService | None" = None

    # ── El servicio ───────────────────────────────────────────────────────────
    def service(self) -> "TwoFactorService":
        """
        El servicio, construido perezosamente desde el contenedor de identidad.

        Perezoso y cacheado con `RLock`, igual que los proveedores del contenedor: el plugin se
        instancia al declarar el registro —antes de `configure_identity`— así que construirlo
        en `__init__` obligaría a un orden de cableado que nadie tiene por qué recordar.
        """
        with self._lock:
            if self._service is None:
                from hexcore.darwin.application.container import get_identity_container
                from hexcore.darwin.plugins.two_factor.crypto import TotpSecretCipher
                from hexcore.darwin.plugins.two_factor.service import (
                    DEFAULT_CHALLENGE_TTL,
                    TwoFactorService,
                )

                contenedor = get_identity_container()
                clave = contenedor.config.secret_key
                if clave is None:  # pragma: no cover - `IdentityConfig` ya lo garantiza
                    raise RuntimeError(
                        "El plugin 'two_factor' necesita `IdentityConfig.secret_key` para "
                        "cifrar los secretos TOTP en reposo."
                    )

                self._service = TwoFactorService(
                    repository=self._repository or self._repositorio_por_defecto(),
                    users=contenedor.users(),
                    verifications=contenedor.verifications(),
                    cipher=TotpSecretCipher(clave.get_secret_value()),
                    clock=contenedor.clock(),
                    sessions=contenedor.session_service(),
                    issuer=self._issuer,
                    challenge_ttl=self._challenge_ttl or DEFAULT_CHALLENGE_TTL,
                )
            return self._service

    @staticmethod
    def _repositorio_por_defecto() -> "AbstractTwoFactorRepository":
        """El repositorio del backend que resolvió el contenedor. Ver `plugins/storage.py`."""
        from hexcore.darwin.plugins.storage import plugin_repositories

        return plugin_repositories("two_factor").TwoFactorRepository()

    def reset(self) -> None:
        """Descarta el servicio cacheado. Para los tests, que reconfiguran el contenedor."""
        with self._lock:
            self._service = None

    # ── Lo que aporta ─────────────────────────────────────────────────────────
    def tables(self) -> t.Mapping[str, type]:
        from hexcore.darwin.plugins.two_factor.orms.sqlalchemy.models_mixins import (
            TwoFactorMixin,
        )

        return {"TwoFactorMixin": TwoFactorMixin}

    def exception_status_map(self) -> t.Mapping[type[Exception], int]:
        return TWO_FACTOR_EXCEPTION_STATUS_MAP

    def hooks(self) -> t.Sequence[HookBinding]:
        from hexcore.darwin.application.services import SIGN_IN_AUTHENTICATED

        return [
            HookBinding(
                action=SIGN_IN_AUTHENTICATED,
                phase="before",
                handler=self._exigir_segundo_factor,
                priority=10,
            )
        ]

    async def _exigir_segundo_factor(self, usuario: t.Any) -> None:
        """
        El hook. Devuelve `None` —no cambia el payload— o lanza.

        Lanzar `TwoFactorRequiredError` funciona porque `run_hooks` deja pasar los
        `IdentityError` sin envolverlos: envolverlos convertiría el desafío en un 500.
        """
        await self.service().require(usuario)
        return None

    def routers(self) -> t.Sequence[t.Any]:
        if not self._include_router:
            return ()

        from hexcore.darwin.plugins.two_factor.router import build_two_factor_router

        return [build_two_factor_router(rate_limit=self._rate_limit)]

    def register_handlers(self, registry: t.Any) -> None:
        from hexcore.darwin.plugins.two_factor.commands import (
            CompleteTwoFactorSignIn,
            CompleteTwoFactorSignInHandler,
            ConfirmTwoFactor,
            ConfirmTwoFactorHandler,
            DisableTwoFactor,
            DisableTwoFactorHandler,
            EnrollTwoFactor,
            EnrollTwoFactorHandler,
        )

        registry.register_command_handler(
            EnrollTwoFactor, registry.factory(EnrollTwoFactorHandler)
        )
        registry.register_command_handler(
            ConfirmTwoFactor, registry.factory(ConfirmTwoFactorHandler)
        )
        registry.register_command_handler(
            DisableTwoFactor, registry.factory(DisableTwoFactorHandler)
        )
        registry.register_command_handler(
            CompleteTwoFactorSignIn, registry.factory(CompleteTwoFactorSignInHandler)
        )


def get_two_factor_service() -> "TwoFactorService":
    """
    El servicio del plugin registrado en este despliegue.

    Se busca en el registro del contenedor y no en un global propio: los plugins son de un
    despliegue, y un segundo global tendría que resetearse aparte en cada test — que es
    exactamente la clase de estado compartido que produce los tests que pasan aislados y fallan
    en la suite.

    Raises:
        RuntimeError: el plugin no está registrado, con la remediación copiable.
    """
    from hexcore.darwin.application.container import get_identity_container

    plugin = get_identity_container().plugins.get(TwoFactorPlugin.name)
    if not isinstance(plugin, TwoFactorPlugin):
        raise RuntimeError(
            "El plugin 'two_factor' no está registrado en este despliegue.\n\n"
            "    from hexcore.darwin import PluginRegistry, configure_identity\n"
            "    from hexcore.darwin.plugins.two_factor import TwoFactorPlugin\n\n"
            "    configure_identity(config, plugins=PluginRegistry([TwoFactorPlugin()]))"
        )
    return plugin.service()
