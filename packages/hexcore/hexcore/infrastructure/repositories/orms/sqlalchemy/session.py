"""
Engine y session factory de SQLAlchemy para HexCore.

Punto de entrada recomendado: `init_engine()` en el arranque y `dispose_engine()` en
el apagado. `get_engine()` / `get_session_factory()` siguen existiendo y hacen
lazy-init con los defaults correctos, así que nada obliga a llamar a `init_engine`.

Decisiones que **no** son parámetros porque son el comportamiento correcto:

- ``expire_on_commit=False``. Con el default de SQLAlchemy (``True``) los atributos de
  las entidades expiran al comitear, y el siguiente acceso dispara un lazy-load sobre
  una sesión ya cerrada → ``MissingGreenlet`` / ``DetachedInstanceError``. Quien
  necesite ``True`` construye su propio ``async_sessionmaker``.
- La normalización del driver en el DSN. Un ``DATABASE_URL`` de PaaS viene como
  ``postgresql://…`` y ``create_async_engine`` no lo acepta.
"""
from __future__ import annotations

import threading
import typing as t
from collections.abc import AsyncGenerator

from pydantic import BaseModel as PydanticBaseModel
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hexcore.config import LazyConfig

__all__ = [
    "PoolSettings",
    "init_engine",
    "dispose_engine",
    "get_engine",
    "get_session_factory",
    "get_async_db_session",
    "normalize_async_dsn",
]


class PoolSettings(PydanticBaseModel):
    """
    Tuning del pool de conexiones.

    ``pre_ping`` va en True por defecto a propósito: un pool sin pre-ping contra
    Postgres detrás de un balanceador entrega conexiones muertas al primer failover.
    """

    size: int | None = None
    max_overflow: int | None = None
    pre_ping: bool = True
    recycle: int | None = 1800


# Mapeo de esquemas "síncronos" al driver async equivalente. Sólo se aplica cuando el
# DSN no declara driver (`postgresql://`, no `postgresql+psycopg://`).
_ASYNC_DRIVERS: dict[str, str] = {
    "postgresql": "postgresql+asyncpg",
    "postgres": "postgresql+asyncpg",
    "sqlite": "sqlite+aiosqlite",
    "mysql": "mysql+aiomysql",
    "mariadb": "mariadb+aiomysql",
}

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
# Los globals se tocan desde el lifespan, desde los workers y potencialmente desde
# varios hilos (free-threading en 3.14): sin lock se pueden crear dos engines.
_lock = threading.RLock()


def normalize_async_dsn(url: str) -> str:
    """
    Devuelve un DSN utilizable por ``create_async_engine``.

    ``postgresql://host/db`` → ``postgresql+asyncpg://host/db``. Si el DSN ya declara
    un driver (``+asyncpg``, ``+psycopg``…) se deja intacto: puede que sea a propósito.
    """
    scheme, separator, rest = url.partition("://")
    if not separator or "+" in scheme:
        return url
    replacement = _ASYNC_DRIVERS.get(scheme.lower())
    if replacement is None:
        return url
    return f"{replacement}://{rest}"


def init_engine(
    url: str | None = None,
    *,
    pool: PoolSettings | None = None,
    **engine_kwargs: t.Any,
) -> AsyncEngine:
    """
    Crea (o devuelve) el engine asíncrono del proceso.

    Args:
        url: DSN explícito. Si es None, se usa ``config.async_sql_database_url``.
            En ambos casos se normaliza el driver. Los workers, scripts y tests
            necesitan poder pasarlo explícitamente.
        pool: Tuning del pool. Ver `PoolSettings`.
        **engine_kwargs: Se pasan tal cual a ``create_async_engine`` (``echo``,
            ``connect_args``, ``json_serializer``…).

    Returns:
        El engine. Si ya había uno inicializado se devuelve ése **sin** aplicar los
        parámetros nuevos: llamá a `dispose_engine()` antes si querés reconfigurar.
    """
    global _engine

    with _lock:
        if _engine is not None:
            return _engine

        dsn = normalize_async_dsn(url or LazyConfig.get_config().async_sql_database_url)
        options = _pool_kwargs(dsn, pool or PoolSettings())
        options.update(engine_kwargs)
        _engine = create_async_engine(dsn, **options)
        return _engine


def _pool_kwargs(dsn: str, pool: PoolSettings) -> dict[str, t.Any]:
    """
    Traduce `PoolSettings` a kwargs de ``create_async_engine``.

    SQLite no usa un pool con tamaño (``NullPool``/``StaticPool`` según el caso), y
    pasarle ``pool_size`` es un ``TypeError``, así que esas dos opciones se omiten.
    """
    options: dict[str, t.Any] = {"pool_pre_ping": pool.pre_ping}
    if pool.recycle is not None:
        options["pool_recycle"] = pool.recycle

    if dsn.startswith("sqlite"):
        return options

    if pool.size is not None:
        options["pool_size"] = pool.size
    if pool.max_overflow is not None:
        options["max_overflow"] = pool.max_overflow
    return options


async def dispose_engine() -> None:
    """
    Cierra el pool de conexiones y deja el módulo re-inicializable.

    Es lo que hay que llamar en el shutdown de un lifespan, y también antes de
    reconfigurar el engine.
    """
    global _engine, _session_factory

    with _lock:
        engine, _engine = _engine, None
        _session_factory = None

    if engine is not None:
        await engine.dispose()


def get_engine() -> AsyncEngine:
    """
    Retorna el engine asíncrono de SQLAlchemy.
    Se inicializa de forma lazy con los defaults de `init_engine()`.
    """
    return init_engine()


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """
    Retorna la factoría de sesiones de SQLAlchemy.
    Se inicializa de forma lazy, con ``expire_on_commit=False``.
    """
    global _session_factory

    with _lock:
        if _session_factory is None:
            _session_factory = async_sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=get_engine(),
                class_=AsyncSession,
                # Sin esto, acceder a un atributo después de `uow.commit()` dispara un
                # lazy-load sobre una sesión cerrada.
                expire_on_commit=False,
            )
        return _session_factory


async def get_async_db_session() -> AsyncGenerator[AsyncSession, None]:
    """Generador de sesiones asíncronas delegando el ciclo de vida al context manager."""
    factory = get_session_factory()
    async with factory() as db:
        yield db
