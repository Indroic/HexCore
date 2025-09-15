from __future__ import annotations
import importlib
import typing as t
from pydantic import BaseModel, ConfigDict
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


class LazyConfig:
    """
    Loader de configuración flexible.
    Busca una variable 'config' (instancia de ServerConfig) o una clase 'ServerConfig' en los módulos personalizados.
    Si no la encuentra, usa la configuración base del kernel.

    IMPORTANTE: La configuración personalizada debe estar en un módulo llamado 'config' en src.domain

    """

    _imported_config: t.Optional[ServerConfig] = None

    @classmethod
    def get_config(cls) -> ServerConfig:
        if cls._imported_config is not None:
            return cls._imported_config
        # Intenta importar la config personalizada
        for modpath in ("config", "src.domain.config"):
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
