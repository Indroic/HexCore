"""
F2 y F3: la capa de sesión SQL tiene que ser usable en producción, y los scopes
tienen que existir fuera de FastAPI.

Defectos que cubren estos tests:
1. `expire_on_commit` no se pasaba → quedaba en True (default de SQLAlchemy), así que
   tras `commit()` los atributos expiraban y el siguiente acceso disparaba un lazy-load
   sobre una sesión cerrada (`MissingGreenlet` / `DetachedInstanceError`).
2. Sin tuning del pool (`pre_ping`, `recycle`, `size`, `max_overflow`).
3. Sin normalización del DSN: `postgresql://…` reventaba en `create_async_engine`.
4. No se podía pasar la URL explícitamente.
5. Sin cierre ordenado con un nombre que lo dijera.
6. Globals sin lock.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

from sqlalchemy import String, select  # noqa: E402
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402

from hexcore.infrastructure.repositories.orms.sqlalchemy import Base  # noqa: E402
from hexcore.infrastructure.repositories.orms.sqlalchemy import session as session_module  # noqa: E402
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    PoolSettings,
    dispose_engine,
    get_engine,
    get_session_factory,
    init_engine,
    normalize_async_dsn,
    reset_sqlalchemy_engine,
)
from hexcore.infrastructure.uow.scopes import session_scope  # noqa: E402

SQLITE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_engine():
    # Fixture síncrono a propósito: los tests de concurrencia no son async y una
    # fixture async no se resolvería para ellos.
    asyncio.run(dispose_engine())
    yield
    asyncio.run(dispose_engine())


class Widget(Base):
    __tablename__ = "f2_widgets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(50))


# ── Normalización del DSN (defecto 3) ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("postgresql://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("postgres://u:p@h/db", "postgresql+asyncpg://u:p@h/db"),
        ("sqlite:///./db.sqlite3", "sqlite+aiosqlite:///./db.sqlite3"),
        ("mysql://u:p@h/db", "mysql+aiomysql://u:p@h/db"),
    ],
)
def test_normalize_async_dsn_adds_the_async_driver(raw, expected):
    assert normalize_async_dsn(raw) == expected


@pytest.mark.parametrize(
    "already_async",
    [
        "postgresql+asyncpg://u:p@h/db",
        "sqlite+aiosqlite:///./db.sqlite3",
        # Un driver explícito distinto se respeta: puede ser a propósito.
        "postgresql+psycopg://u:p@h/db",
    ],
)
def test_normalize_async_dsn_leaves_explicit_drivers_alone(already_async):
    assert normalize_async_dsn(already_async) == already_async


def test_normalize_async_dsn_leaves_unknown_schemes_alone():
    assert normalize_async_dsn("oracle://u:p@h/db") == "oracle://u:p@h/db"
    assert normalize_async_dsn("not-a-dsn") == "not-a-dsn"


# ── URL explícita y pool (defectos 2 y 4) ──────────────────────────────────────


@pytest.mark.anyio
async def test_init_engine_accepts_an_explicit_url():
    engine = init_engine(SQLITE_URL)

    assert engine.url.database == ":memory:"
    assert engine.url.drivername == "sqlite+aiosqlite"


@pytest.mark.anyio
async def test_init_engine_normalizes_the_explicit_url():
    engine = init_engine("sqlite:///:memory:")

    assert engine.url.drivername == "sqlite+aiosqlite"


@pytest.mark.anyio
async def test_pool_pre_ping_is_on_by_default():
    engine = init_engine(SQLITE_URL)

    assert engine.pool._pre_ping is True  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_pool_settings_are_applied_for_non_sqlite():
    engine = init_engine(
        "postgresql+asyncpg://u:p@localhost/db",
        pool=PoolSettings(size=7, max_overflow=3, recycle=60),
    )

    assert engine.pool.size() == 7  # type: ignore[attr-defined]
    assert engine.pool._max_overflow == 3  # type: ignore[attr-defined]
    assert engine.pool._recycle == 60  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_sqlite_ignores_pool_size_instead_of_crashing():
    """SQLite no usa un pool con tamaño; pasarle pool_size es un TypeError."""
    engine = init_engine(SQLITE_URL, pool=PoolSettings(size=10, max_overflow=5))

    assert engine is not None


@pytest.mark.anyio
async def test_engine_kwargs_are_forwarded():
    engine = init_engine(SQLITE_URL, echo=True)

    assert engine.echo is True


# ── expire_on_commit (defecto 1) ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_session_factory_sets_expire_on_commit_to_false():
    init_engine(SQLITE_URL)
    factory = get_session_factory()

    assert factory.kw["expire_on_commit"] is False


@pytest.mark.anyio
async def test_attribute_access_after_commit_does_not_lazy_load():
    """
    El síntoma real: con expire_on_commit=True esto lanza MissingGreenlet, porque el
    acceso al atributo dispara IO sobre una sesión ya comiteada.
    """
    engine = init_engine(SQLITE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = get_session_factory()
    async with factory() as session:
        widget = Widget(name="gizmo")
        session.add(widget)
        await session.commit()
        # Sin expire_on_commit=False, este acceso revienta.
        assert widget.name == "gizmo"


@pytest.mark.anyio
async def test_entity_survives_outside_the_session_block():
    engine = init_engine(SQLITE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    factory = get_session_factory()
    async with factory() as session:
        widget = Widget(name="detached")
        session.add(widget)
        await session.commit()

    # Fuera del bloque la sesión está cerrada: sin el arreglo, DetachedInstanceError.
    assert widget.name == "detached"


# ── Ciclo de vida (defecto 5) ──────────────────────────────────────────────────


@pytest.mark.anyio
async def test_dispose_engine_leaves_the_module_reinitialisable():
    first = init_engine(SQLITE_URL)
    await dispose_engine()
    second = init_engine(SQLITE_URL)

    assert first is not second


@pytest.mark.anyio
async def test_dispose_engine_clears_the_session_factory_too():
    init_engine(SQLITE_URL)
    first_factory = get_session_factory()
    await dispose_engine()
    init_engine(SQLITE_URL)

    assert get_session_factory() is not first_factory


@pytest.mark.anyio
async def test_dispose_engine_is_safe_without_an_engine():
    await dispose_engine()
    await dispose_engine()


@pytest.mark.anyio
async def test_reset_sqlalchemy_engine_is_still_available():
    init_engine(SQLITE_URL)
    await reset_sqlalchemy_engine()

    assert session_module._engine is None


@pytest.mark.anyio
async def test_init_engine_is_idempotent():
    first = init_engine(SQLITE_URL)
    second = init_engine(SQLITE_URL)

    assert first is second


@pytest.mark.anyio
async def test_get_engine_lazily_initialises():
    assert session_module._engine is None
    engine = get_engine()

    assert engine is not None
    assert session_module._engine is engine


# ── Globals con lock (defecto 6) ───────────────────────────────────────────────


def test_concurrent_init_engine_creates_a_single_engine():
    asyncio.run(dispose_engine())
    ready = threading.Barrier(8)
    engines: list[object] = []
    lock = threading.Lock()

    def worker() -> None:
        ready.wait()
        engine = init_engine(SQLITE_URL)
        with lock:
            engines.append(engine)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len({id(engine) for engine in engines}) == 1
    asyncio.run(dispose_engine())


def test_concurrent_get_session_factory_creates_a_single_factory():
    asyncio.run(dispose_engine())
    init_engine(SQLITE_URL)
    ready = threading.Barrier(8)
    factories: list[object] = []
    lock = threading.Lock()

    def worker() -> None:
        ready.wait()
        factory = get_session_factory()
        with lock:
            factories.append(factory)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len({id(factory) for factory in factories}) == 1
    asyncio.run(dispose_engine())


# ── F3: session_scope fuera de FastAPI ─────────────────────────────────────────


@pytest.mark.anyio
async def test_session_scope_yields_a_usable_session():
    engine = init_engine(SQLITE_URL)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_scope() as session:
        session.add(Widget(name="from-scope"))
        await session.commit()

    async with session_scope() as session:
        names = (await session.execute(select(Widget.name))).scalars().all()

    assert "from-scope" in names


@pytest.mark.anyio
async def test_session_scope_closes_the_session_on_exit():
    init_engine(SQLITE_URL)

    async with session_scope() as session:
        pass

    assert session.is_active is False or not session.in_transaction()


@pytest.mark.anyio
async def test_session_scope_does_not_instantiate_repositories():
    """
    El punto de F3: construir un UoW corre el auto-discovery e instancia todos los
    repositorios de dominio. `session_scope` no debe hacer nada de eso — de hecho
    funciona sin `repository_discovery_paths` configurado, que es lo que haría fallar
    a un UoW.
    """
    init_engine(SQLITE_URL)

    async with session_scope() as session:
        assert not hasattr(session, "repositories")
