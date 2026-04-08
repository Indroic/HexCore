from __future__ import annotations
import importlib
import os
import typing as t
from pydantic import BaseModel, ConfigDict, Field
from pathlib import Path
from hexcore.infrastructure.cache import ICache
from hexcore.domain.events import IEventDispatcher


from hexcore.infrastructure.cache.cache_backends.memory import MemoryCache
from hexcore.infrastructure.events.events_backends.memory import InMemoryEventDispatcher


class ServerConfig(BaseModel):
    # Project Config
    base_dir: Path = Path(".")

    # SERVER CONFIG
    host: str = "localhost"
    port: int = 8000
    debug: bool = True

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
    allow_origins: list[str] = [
        "*" if debug else "http://localhost:{port}".format(port=port)
    ]
    allow_credentials: bool = True
    allow_methods: list[str] = ["*"]
    allow_headers: list[str] = ["*"]

    # caching
    cache_backend: ICache = (
        MemoryCache()
    )  # Debe ser una instancia de ICache(o subclase)

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Event Dispatcher
    event_dispatcher: IEventDispatcher = InMemoryEventDispatcher()

    # Repository Discovery
    # v2 (breaking): discovery explicito y folder-agnostic.
    # Si se deja vacio, no se autoloadearan modulos de repositorios.
    repository_discovery_paths: set[str] = Field(default_factory=set)


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
