"""
`oauth`: entrar con Google, GitHub, Microsoft y compañía.

Authorization Code con **PKCE obligatorio** (`S256`, nunca `plain`) y `state` de un solo uso
guardado del lado del servidor. Reusa la tabla `account` del núcleo —que ya está diseñada para
esto: `provider_id` + `account_id` con `UNIQUE`— y aporta una tabla propia sólo para el `state` en
vuelo, porque ese necesita guardar el verificador de PKCE y `verification` no tiene dónde.

⚠️ **La decisión que más importa: por default NO se vincula por coincidencia de mail.** Es la toma
de cuentas más común de OAuth — cualquiera que consiga registrar el mail de la víctima en
*cualquier* proveedor configurado entraría a su cuenta. El default es `LinkPolicy.NEVER`, y una
coincidencia produce un 409 que le dice al usuario que inicie sesión con su método actual y
vincule desde los ajustes. Ver el docstring de `service.py` para las tres políticas y cuándo cada
una tiene sentido.

Requiere los extras `[darwin]`, `[api]` y **`[darwin-oauth]`** (que suma `httpx`). El cliente HTTP
está detrás de un puerto, así que un test del flujo completo no necesita red ni el extra.

Uso::

    from hexcore.darwin import PluginRegistry, configure_identity
    from hexcore.darwin.plugins.oauth import OAuthPlugin
    from hexcore.darwin.plugins.oauth.providers import github, google

    plugins = PluginRegistry([
        OAuthPlugin(
            providers=[
                google(client_id="...", client_secret="..."),
                github(client_id="...", client_secret="..."),
            ],
            allowed_redirect_uris=["https://mi-app.com/auth/callback"],
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
from hexcore.darwin.plugins.oauth.domain import (
    OAUTH_EXCEPTION_STATUS_MAP,
    AbstractOAuthHttpClient,
    AbstractOAuthStateRepository,
    OAuthAccountAlreadyLinkedError,
    OAuthAccountNotLinkedError,
    OAuthEmailNotVerifiedError,
    OAuthError,
    OAuthExchangeError,
    OAuthProviderNotConfiguredError,
    OAuthStateError,
    OAuthTokens,
)
from hexcore.darwin.plugins.oauth.providers import (
    OAuthProfile,
    OAuthProvider,
    PROVIDER_FACTORIES,
)
from hexcore.darwin.plugins.oauth.service import LinkPolicy

if t.TYPE_CHECKING:
    # Se importa sólo para el checker: en runtime lo resuelve el `__getattr__` de abajo, porque
    # importarlo arrastra sqlalchemy y nombrar el plugin no puede exigir el extra `[darwin-sqlalchemy]`.
    from hexcore.darwin.plugins.oauth.orms.sqlalchemy.models_mixins import OAuthStateMixin as OAuthStateMixin
    from hexcore.darwin.plugins.oauth.service import OAuthService

__all__ = [
    "OAuthPlugin",
    "OAuthProvider",
    "OAuthProfile",
    "OAuthTokens",
    "AbstractOAuthHttpClient",
    "AbstractOAuthStateRepository",
    "LinkPolicy",
    "PROVIDER_FACTORIES",
    "OAuthError",
    "OAuthProviderNotConfiguredError",
    "OAuthStateError",
    "OAuthExchangeError",
    "OAuthAccountNotLinkedError",
    "OAuthAccountAlreadyLinkedError",
    "OAuthEmailNotVerifiedError",
    "OAUTH_EXCEPTION_STATUS_MAP",
    "OAuthStateMixin",
    "get_oauth_service",
]

#: La etiqueta con la que se cifran los tokens de los proveedores. ⚠️ **Cambiarla invalida todos
#: los tokens guardados**: los usuarios siguen pudiendo entrar —el flujo los renueva— pero
#: cualquier llamada a la API de un tercero con un token viejo falla hasta que el usuario vuelva
#: a autorizar.
_ETIQUETA_TOKENS = b"hexcore.darwin.oauth.provider_tokens.v1"


class OAuthPlugin(DarwinPlugin):
    """
    El plugin de OAuth.

    Args:
        providers: Los proveedores configurados. Armalos con las funciones de `providers.py`
            (`google(...)`, `github(...)`) o a mano con `OAuthProvider`.
        allowed_redirect_uris: Las URIs de callback permitidas. **Declaralas en producción**: sin
            la lista no se valida nada, y un `redirect_uri` libre deja que un atacante inicie el
            flujo apuntando a su propio sitio y se lleve el código de la víctima.
        link_policy: Ver `LinkPolicy`. El default no vincula por mail, y es el correcto.
        state_ttl: Cuánto vive un flujo en vuelo.
        http: Un `AbstractOAuthHttpClient` propio. Por defecto, el de `httpx`.
        state_repository: Un repositorio propio del `state`.
        include_router: Si aporta su router.
    """

    name = "oauth"
    #: Los nombres que devuelve `tables()`, para que el registro valide el conflicto
    #: de homónimos sin importar sqlalchemy. Un test verifica que coincidan.
    contributed_tables = ("OAuthStateMixin",)

    #: Después de `two_factor` (20): si un usuario que entra por OAuth tiene segundo factor, el
    #: hook del sign-in no aplica igual —OAuth no pasa por `sign_in`— pero el orden deja claro
    #: cuál plugin gobierna la autenticación primaria.
    priority = 30

    def __init__(
        self,
        *,
        providers: t.Sequence[OAuthProvider] = (),
        allowed_redirect_uris: t.Sequence[str] = (),
        link_policy: LinkPolicy = LinkPolicy.NEVER,
        state_ttl: timedelta | None = None,
        http: AbstractOAuthHttpClient | None = None,
        state_repository: AbstractOAuthStateRepository | None = None,
        include_router: bool = True,
    ) -> None:
        self._providers = {p.id: p for p in providers}
        if len(self._providers) != len(providers):
            # Dos proveedores con el mismo `id` significa que uno gana en silencio, y cuál
            # depende del orden de la lista. `provider_id` es parte de la clave única de
            # `account`, así que el que pierde desvincularía a sus usuarios.
            raise ValueError(
                "Hay dos proveedores con el mismo `id`. Renombrá uno: el `id` es parte de la "
                "clave única de `account`."
            )

        self._redirects = tuple(allowed_redirect_uris)
        self._policy = link_policy
        self._state_ttl = state_ttl
        self._http = http
        self._state_repository = state_repository
        self._include_router = include_router
        self._lock = threading.RLock()
        self._service: "OAuthService | None" = None

    # ── El servicio ───────────────────────────────────────────────────────────
    def service(self) -> "OAuthService":
        """
        El servicio, construido perezosamente desde el contenedor de identidad.

        Perezoso y cacheado con `RLock`, igual que los proveedores del contenedor: el plugin se
        instancia al declarar el registro —antes de `configure_identity`— así que construirlo en
        `__init__` obligaría a un orden de cableado que nadie tiene por qué recordar.
        """
        with self._lock:
            if self._service is None:
                from hexcore.darwin.application.container import get_identity_container
                from hexcore.darwin.infrastructure.secretbox import SecretBox
                from hexcore.darwin.plugins.oauth.service import (
                    DEFAULT_STATE_TTL,
                    OAuthService,
                )

                contenedor = get_identity_container()
                clave = contenedor.config.secret_key
                if clave is None:  # pragma: no cover - `IdentityConfig` ya lo garantiza
                    raise RuntimeError(
                        "El plugin 'oauth' necesita `IdentityConfig.secret_key` para cifrar "
                        "los tokens de los proveedores y el verificador de PKCE."
                    )

                self._service = OAuthService(
                    providers=self._providers,
                    states=self._state_repository or self._repositorio_por_defecto(),
                    users=contenedor.users(),
                    accounts=contenedor.accounts(),
                    sessions=contenedor.session_service(),
                    clock=contenedor.clock(),
                    http=self._http or self._http_por_defecto(),
                    secrets_box=SecretBox(
                        clave.get_secret_value(), label=_ETIQUETA_TOKENS
                    ),
                    allowed_redirect_uris=self._redirects,
                    link_policy=self._policy,
                    state_ttl=self._state_ttl or DEFAULT_STATE_TTL,
                )
            return self._service

    @staticmethod
    def _repositorio_por_defecto() -> AbstractOAuthStateRepository:
        """El repositorio del backend que resolvió el contenedor. Ver `plugins/storage.py`."""
        from hexcore.darwin.plugins.storage import plugin_repositories

        return plugin_repositories("oauth").OAuthStateRepository()

    @staticmethod
    def _http_por_defecto() -> AbstractOAuthHttpClient:
        from hexcore.darwin.plugins.oauth.http import HttpxOAuthClient

        return HttpxOAuthClient()

    def reset(self) -> None:
        """Descarta el servicio cacheado. Para los tests, que reconfiguran el contenedor."""
        with self._lock:
            self._service = None

    # ── Lo que aporta ─────────────────────────────────────────────────────────
    def tables(self) -> t.Mapping[str, type]:
        from hexcore.darwin.plugins.oauth.orms.sqlalchemy.models_mixins import OAuthStateMixin

        return {"OAuthStateMixin": OAuthStateMixin}

    def exception_status_map(self) -> t.Mapping[type[Exception], int]:
        return OAUTH_EXCEPTION_STATUS_MAP

    def routers(self) -> t.Sequence[t.Any]:
        if not self._include_router:
            return ()

        from hexcore.darwin.plugins.oauth.router import build_oauth_router

        return [build_oauth_router()]

    def startup_steps(self) -> t.Sequence[t.Any]:
        """
        Un paso que valida el cableado al arrancar.

        Falla acá y no en el primer login, que es el criterio de toda la casa: un proveedor sin
        secreto, o una allowlist vacía con proveedores configurados, se descubre en el arranque.
        """
        return [
            _ValidarCableado(
                proveedores=len(self._providers), tiene_allowlist=bool(self._redirects)
            )
        ]


class _ValidarCableado:
    """
    `StartupStep` que revisa lo que sólo se puede revisar con todo cableado.

    Avisa —no falla— si hay proveedores sin allowlist de `redirect_uri`: en desarrollo la lista
    vacía es cómoda a propósito, y convertirla en un error de arranque haría que el primer
    contacto con el plugin sea un fallo.
    """

    name = "darwin.oauth.validate"

    def __init__(self, *, proveedores: int, tiene_allowlist: bool) -> None:
        # Los valores y no el plugin: el paso sólo necesita dos números, y guardarse el plugin
        # entero para leerle atributos privados es la clase de acoplamiento que después impide
        # cambiarlos.
        self._proveedores = proveedores
        self._tiene_allowlist = tiene_allowlist

    async def __call__(self) -> None:
        import logging

        if self._proveedores and not self._tiene_allowlist:
            logging.getLogger("hexcore.darwin.oauth").warning(
                "El plugin 'oauth' tiene %d proveedor(es) configurado(s) y ninguna URI de "
                "callback en la allowlist. En producción declaralas: sin la lista, un atacante "
                "puede iniciar el flujo apuntando a su propio sitio y llevarse el código de "
                "autorización de la víctima.",
                self._proveedores,
            )


def get_oauth_service() -> "OAuthService":
    """
    El servicio del plugin registrado en este despliegue.

    Se busca en el registro del contenedor y no en un global propio: los plugins son de un
    despliegue, y un segundo global tendría que resetearse aparte en cada test.

    Raises:
        RuntimeError: el plugin no está registrado, con la remediación copiable.
    """
    from hexcore.darwin.application.container import get_identity_container

    plugin = get_identity_container().plugins.get(OAuthPlugin.name)
    if not isinstance(plugin, OAuthPlugin):
        raise RuntimeError(
            "El plugin 'oauth' no está registrado en este despliegue.\n\n"
            "    from hexcore.darwin import PluginRegistry, configure_identity\n"
            "    from hexcore.darwin.plugins.oauth import OAuthPlugin\n"
            "    from hexcore.darwin.plugins.oauth.providers import google\n\n"
            "    configure_identity(config, plugins=PluginRegistry([\n"
            "        OAuthPlugin(providers=[google(client_id=..., client_secret=...)])\n"
            "    ]))"
        )
    return plugin.service()


def __getattr__(name: str) -> t.Any:
    """
    `OAuthStateMixin` perezoso: importarlo arrastra sqlalchemy.

    Está en `__all__` porque es parte de la API pública —el consumidor lo compone en su paquete
    ``models/``— pero nombrar el plugin no puede exigir el extra `[darwin-sqlalchemy]`. Es el mismo patrón que
    la fachada de Darwin.
    """
    if name == "OAuthStateMixin":
        from hexcore.darwin.plugins.oauth.orms.sqlalchemy.models_mixins import OAuthStateMixin

        return OAuthStateMixin
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
