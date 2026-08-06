from __future__ import annotations
import importlib
import os
import typing as t
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pathlib import Path
from hexcore.infrastructure.cache import ICache
from hexcore.domain.events import EventBus


from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache
from hexcore.infrastructure.events.events_backends.memory import InMemoryEventBus


class ServerConfig(BaseModel):
    # Project Config
    base_dir: Path = Path(".")

    # SERVER CONFIG
    host: str = "localhost"
    port: int = 8000
    debug: bool = True

    # Identidad de la app. La usa `create_app()` para el título y la versión de FastAPI,
    # de modo que el camino feliz no necesite pasarlos.
    app_title: str = "HexCore API"
    app_version: str = "0.1.0"

    # DB CONFIG
    sql_database_url: str = "sqlite:///./db.sqlite3"
    async_sql_database_url: str = "sqlite+aiosqlite:///./db.sqlite3"

    mongo_database_url: str = "mongodb://localhost:27017"
    async_mongo_database_url: str = "mongodb+async://localhost:27017"
    mongo_db_name: str = "euphoria_db"
    mongo_uri: str = "mongodb://localhost:27017/euphoria_db"

    redis_uri: str = "redis://localhost:6379/0"
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_cache_duration: int = 300  # seconds

    # Security
    #
    # `allow_origins` NO se puede derivar en el cuerpo de la clase. La versión anterior
    # era ``["*" if debug else "http://localhost:{port}"]``, y ese `debug` es el del
    # cuerpo de clase (siempre `True`), así que el condicional era código muerto y el
    # valor era **siempre** `["*"]` — incluso con `ServerConfig(debug=False)`.
    #
    # Combinado con `allow_credentials=True`, eso es un agujero real y no teórico:
    # Starlette no puede mandar `*` junto con credenciales, así que cuando hay cookie
    # **refleja el Origin del atacante** (`CORSMiddleware`, rama
    # `if self.allow_all_origins and has_cookie`) y agrega
    # `Access-Control-Allow-Credentials: true`. Cualquier origen puede entonces leer
    # respuestas autenticadas con la cookie de sesión de la víctima, sin necesidad de XSS.
    #
    # Se deriva en un validador `mode="after"`, que sí ve el `debug` y el `port` de **la
    # instancia**. Si lo pasás explícito, no se toca.
    allow_origins: list[str] = Field(default_factory=list)
    allow_credentials: bool = True
    allow_methods: list[str] = ["*"]
    allow_headers: list[str] = ["*"]

    # caching
    cache_backend: ICache = (
        MemoryCache()
    )  # Debe ser una instancia de ICache(o subclase)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Event Bus
    event_bus: EventBus = InMemoryEventBus()

    @property
    def event_dispatcher(self) -> EventBus:
        """Retrocompatibilidad para acceso a event_dispatcher."""
        import warnings
        warnings.warn(
            "ServerConfig.event_dispatcher is deprecated. Use ServerConfig.event_bus instead.",
            DeprecationWarning,
            stacklevel=2,
        )
        return self.event_bus

    @event_dispatcher.setter
    def event_dispatcher(self, value: EventBus) -> None:
        self.event_bus = value

    # Repository Discovery
    # v2 (breaking): discovery explicito y folder-agnostic.
    # Si se deja vacio, no se autoloadearan modulos de repositorios.
    repository_discovery_paths: set[str] = Field(default_factory=set)

    # CQRS (opcional — None = deshabilitado, sin impacto en módulos existentes)
    # Tipo: Optional[hexcore.application.cqrs.config.CQRSConfig]
    cqrs: t.Any = None

    @model_validator(mode="before")
    @classmethod
    def map_deprecated_fields(cls, data: t.Any) -> t.Any:
        if isinstance(data, dict) and "event_dispatcher" in data:
            import warnings
            warnings.warn(
                "Passing 'event_dispatcher' to ServerConfig is deprecated. Use 'event_bus' instead.",
                DeprecationWarning,
                stacklevel=2,
            )
            data["event_bus"] = data.pop("event_dispatcher")
        return data

    # ── CORS ──────────────────────────────────────────────────────────────────
    @model_validator(mode="after")
    def _resolve_cors_origins(self) -> "ServerConfig":
        """
        Deriva `allow_origins` del `debug`/`port` reales y rechaza la combinación insegura.

        Dos cosas, en este orden:

        1. Si no pasaste `allow_origins`, se completa: en `debug` queda `["*"]` (comodidad
           de desarrollo, que era la intención original), y fuera de `debug` queda
           `["http://localhost:<port>"]`. Un valor explícito —incluso `[]`— se respeta.
        2. `"*"` con `allow_credentials=True` fuera de `debug` **no arranca**. No es un
           warning: es la configuración que permite que cualquier origen lea respuestas
           autenticadas, y en producción no hay ningún caso legítimo. En `debug` se avisa
           y se sigue —pero sólo si lo pediste vos: avisar sobre nuestro propio default
           de desarrollo sería ruido en cada `ServerConfig()`, y el ruido entrena a
           ignorar los warnings.

        Recordá que si vas a servir sesiones por cookie `HttpOnly`, `"*"` no sirve ni en
        desarrollo: poné los orígenes de tu frontend a mano.
        """
        # Se lee ANTES de asignar: en pydantic v2, asignar un campo lo agrega a
        # `model_fields_set`, así que consultarlo después de derivar el default daría
        # "lo pidió el usuario" para un valor que puso el framework.
        lo_pidio_el_usuario = "allow_origins" in self.model_fields_set

        if not lo_pidio_el_usuario and not self.allow_origins:
            self.allow_origins = (
                ["*"] if self.debug else [f"http://localhost:{self.port}"]
            )

        if "*" in self.allow_origins and self.allow_credentials:
            if not self.debug:
                raise ValueError(
                    "allow_origins=['*'] junto con allow_credentials=True permite que "
                    "cualquier origen lea respuestas autenticadas: el navegador no puede "
                    "mandar '*' con credenciales, así que Starlette refleja el Origin de "
                    "quien pregunte. Con debug=False no arranca.\n\n"
                    "Elegí una de las dos:\n\n"
                    "    config.allow_origins = ['https://tu-front.com']\n"
                    "    # o, si de verdad querés una API pública sin cookies:\n"
                    "    config.allow_credentials = False\n"
                )
            if not lo_pidio_el_usuario:
                return self

            import warnings

            warnings.warn(
                "allow_origins=['*'] con allow_credentials=True: Starlette va a reflejar "
                "el Origin de cualquiera que mande una cookie. Pasa porque debug=True; "
                "con debug=False esto no arranca. Antes de servir sesiones por cookie, "
                "declará los orígenes de tu frontend a mano.",
                stacklevel=2,
            )

        return self


class LazyConfig:
    """
    Loader de configuración flexible y agnóstico de estructura de carpetas.

    Prioridad de resolución:
    1) Variable de entorno HEXCORE_CONFIG_MODULE (módulo único)
    2) Variable de entorno HEXCORE_CONFIG_MODULES (lista separada por comas)
    3) Lista configurada por set_config_modules(...)
    4) Valor por defecto: "config" (archivo config.py en la raíz del proyecto)

    En cada módulo candidato se busca:
    - atributo `config` (instancia o clase derivada de ServerConfig)
    - o clase `ServerConfig` derivada de la base.

    Si no se encuentra nada válido, usa ServerConfig() por defecto.

    """

    _imported_config: t.Optional[ServerConfig] = None
    _config_modules: tuple[str, ...] = ("config",)

    @classmethod
    def set_config_modules(cls, modules: t.Iterable[str]) -> None:
        """Define módulos candidatos para resolver configuración personalizada."""
        normalized_modules = tuple(
            module_name.strip() for module_name in modules if str(module_name).strip()
        )
        cls._config_modules = normalized_modules
        cls._imported_config = None

    @classmethod
    def clear_cache(cls) -> None:
        """Limpia la configuración cacheada para forzar nueva resolución."""
        cls._imported_config = None

    @classmethod
    def _iter_config_module_candidates(cls) -> tuple[str, ...]:
        env_single_module = os.getenv("HEXCORE_CONFIG_MODULE", "").strip()
        if env_single_module:
            return (env_single_module,)

        env_modules_raw = os.getenv("HEXCORE_CONFIG_MODULES", "").strip()
        if env_modules_raw:
            env_modules = tuple(
                module_name.strip()
                for module_name in env_modules_raw.split(",")
                if module_name.strip()
            )
            if env_modules:
                return env_modules

        if cls._config_modules:
            return cls._config_modules

        return ("config",)

    @classmethod
    def get_config(cls) -> ServerConfig:
        if cls._imported_config is not None:
            return cls._imported_config
        # Intenta importar la config personalizada
        for modpath in cls._iter_config_module_candidates():
            try:
                mod = importlib.import_module(modpath)
                config_instance = getattr(mod, "config", None)
                if config_instance is not None:
                    # Si es clase, instanciar
                    if isinstance(config_instance, type) and issubclass(
                        config_instance, ServerConfig
                    ):
                        config_instance = config_instance()
                    if isinstance(config_instance, ServerConfig):
                        cls._imported_config = config_instance
                        return cls._imported_config
                # Alternativamente, busca la clase ServerConfig
                config_class = getattr(mod, "ServerConfig", None)
                if isinstance(config_class, type) and issubclass(
                    config_class, ServerConfig
                ):
                    config_instance = config_class()
                    cls._imported_config = config_instance
                    return cls._imported_config
            except (ModuleNotFoundError, AttributeError):
                continue
        # Fallback: config base del kernel
        cls._imported_config = ServerConfig()
        return cls._imported_config



# Esto es solo para disparar el Workflow pra subir la ultima version a PyPI, por favor ignora este comentario
