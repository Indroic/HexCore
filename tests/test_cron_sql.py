"""
F7: modelo + repositorio + seed de `cron_jobs` de serie.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

from hexcore.domain.cqrs.cron import CronJobDefinition, ICronJobRepository  # noqa: E402
from hexcore.domain.cqrs.decorators import background_task  # noqa: E402
from hexcore.infrastructure.cqrs.cron_sql import (  # noqa: E402
    CronJobModel,
    CronJobModelMixin,
    SqlAlchemyCronJobRepository,
    create_cron_tables,
    cron_job,
    seed_cron_jobs,
)
from hexcore.infrastructure.repositories.base import BaseSQLAlchemyRepository  # noqa: E402
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base, BaseModel  # noqa: E402
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    dispose_engine,
    init_engine,
)
from hexcore.infrastructure.uow.scopes import session_scope  # noqa: E402

SQLITE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _fresh_engine():
    asyncio.run(dispose_engine())
    yield
    asyncio.run(dispose_engine())


async def _prepare() -> SqlAlchemyCronJobRepository:
    # `:memory:` con aiosqlite necesita StaticPool para que todas las sesiones vean la
    # misma base; si no, cada conexión estrena una BD vacía.
    from sqlalchemy.pool import StaticPool

    init_engine(SQLITE_URL, poolclass=StaticPool)
    await create_cron_tables()
    return SqlAlchemyCronJobRepository()


def _definition(job_id: str, cron: str = "0 3 * * *", **kwargs) -> CronJobDefinition:
    defaults = dict(job_id=job_id, task_name=f"app.tasks.{job_id}", cron_expression=cron)
    defaults.update(kwargs)
    return CronJobDefinition(**defaults)  # type: ignore[arg-type]


# ── Los dos detalles que la librería debe encapsular ───────────────────────────


def test_cron_model_does_not_inherit_domain_base_model():
    """
    Si heredara de `BaseModel[T]`, el `collect_domain_entities()` del UoW intentaría
    sacarle una entidad de dominio y explotaría.
    """
    assert not issubclass(CronJobModel, BaseModel)
    assert issubclass(CronJobModel, Base)


def test_cron_repository_does_not_inherit_the_domain_repository_base():
    """
    Si heredara de `BaseSQLAlchemyRepository`, el auto-discovery lo inyectaría en todos
    los UoW como si fuera un repositorio de dominio.
    """
    assert not issubclass(SqlAlchemyCronJobRepository, BaseSQLAlchemyRepository)
    assert issubclass(SqlAlchemyCronJobRepository, ICronJobRepository)


def test_cron_repository_is_not_picked_up_by_auto_discovery():
    from hexcore.infrastructure.repositories.utils import _get_all_subclasses

    discovered = _get_all_subclasses(BaseSQLAlchemyRepository)

    assert SqlAlchemyCronJobRepository not in discovered


def test_custom_table_name_via_the_mixin():
    class CustomCronJob(CronJobModelMixin, Base):
        __tablename__ = "mis_cron_jobs"

    assert CustomCronJob.__tablename__ == "mis_cron_jobs"
    assert "job_id" in CustomCronJob.__table__.columns
    assert "payload" in CustomCronJob.__table__.columns


# ── Repositorio ────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_get_active_jobs_returns_definitions():
    repo = await _prepare()
    await seed_cron_jobs([_definition("close_books"), _definition("purge_logs")])

    jobs = await repo.get_active_jobs()

    assert {job.job_id for job in jobs} == {"close_books", "purge_logs"}
    assert all(isinstance(job, CronJobDefinition) for job in jobs)


@pytest.mark.anyio
async def test_get_active_jobs_excludes_inactive():
    repo = await _prepare()
    await seed_cron_jobs(
        [_definition("active_one"), _definition("inactive_one", is_active=False)]
    )

    jobs = await repo.get_active_jobs()

    assert [job.job_id for job in jobs] == ["active_one"]


@pytest.mark.anyio
async def test_get_all_jobs_includes_inactive():
    repo = await _prepare()
    await seed_cron_jobs(
        [_definition("a"), _definition("b", is_active=False)]
    )

    assert {job.job_id for job in await repo.get_all_jobs()} == {"a", "b"}


@pytest.mark.anyio
async def test_update_last_run_persists():
    repo = await _prepare()
    await seed_cron_jobs([_definition("close_books")])
    moment = datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc)

    await repo.update_last_run("close_books", moment)

    job = (await repo.get_active_jobs())[0]
    assert job.last_run_at is not None
    assert job.last_run_at.replace(tzinfo=timezone.utc) == moment


@pytest.mark.anyio
async def test_update_last_run_normalizes_naive_datetimes():
    repo = await _prepare()
    await seed_cron_jobs([_definition("close_books")])

    await repo.update_last_run("close_books", datetime(2026, 7, 29, 3, 0))

    job = (await repo.get_active_jobs())[0]
    assert job.last_run_at is not None


@pytest.mark.anyio
async def test_set_active_toggles_the_job():
    repo = await _prepare()
    await seed_cron_jobs([_definition("toggle_me")])

    await repo.set_active("toggle_me", False)
    assert await repo.get_active_jobs() == []

    await repo.set_active("toggle_me", True)
    assert len(await repo.get_active_jobs()) == 1


@pytest.mark.anyio
async def test_payload_round_trips():
    repo = await _prepare()
    await seed_cron_jobs([_definition("with_payload", payload={"days": 30, "dry": True})])

    job = (await repo.get_active_jobs())[0]

    assert job.payload == {"days": 30, "dry": True}


@pytest.mark.anyio
async def test_repository_accepts_an_injected_session_scope():
    from sqlalchemy.pool import StaticPool

    init_engine(SQLITE_URL, poolclass=StaticPool)
    await create_cron_tables()
    calls: list[int] = []

    def scope():
        calls.append(1)
        return session_scope()

    repo = SqlAlchemyCronJobRepository(session_scope=scope)
    await repo.get_active_jobs()

    assert calls == [1]


# ── Seed idempotente y no destructivo ──────────────────────────────────────────


@pytest.mark.anyio
async def test_seed_is_idempotent():
    await _prepare()
    definitions = [_definition("a"), _definition("b")]

    assert await seed_cron_jobs(definitions) == 2
    assert await seed_cron_jobs(definitions) == 0


@pytest.mark.anyio
async def test_seed_only_inserts_the_missing_ones():
    await _prepare()
    await seed_cron_jobs([_definition("a")])

    assert await seed_cron_jobs([_definition("a"), _definition("b")]) == 1


@pytest.mark.anyio
async def test_seed_does_not_overwrite_edits_made_in_the_database():
    """
    El sentido de la tabla es editar el cron en caliente: un seed que sobrescribiera
    revertiría en cada deploy los cambios hechos a propósito.
    """
    repo = await _prepare()
    await seed_cron_jobs([_definition("nightly", cron="0 3 * * *")])

    async with session_scope() as session:
        row = await session.get(CronJobModel, "nightly")
        assert row is not None
        row.cron_expression = "0 5 * * *"
        row.is_active = False
        await session.commit()

    # Volver a sembrar la definición original no debe deshacer la edición.
    assert await seed_cron_jobs([_definition("nightly", cron="0 3 * * *")]) == 0

    jobs = await repo.get_all_jobs()
    assert jobs[0].cron_expression == "0 5 * * *"
    assert jobs[0].is_active is False


@pytest.mark.anyio
async def test_seed_with_an_empty_list_is_a_noop():
    await _prepare()

    assert await seed_cron_jobs([]) == 0


@pytest.mark.anyio
async def test_concurrent_seeds_do_not_duplicate():
    """Dos réplicas sembrando en el mismo arranque."""
    await _prepare()
    definitions = [_definition("a"), _definition("b")]

    inserted = await asyncio.gather(
        seed_cron_jobs(definitions),
        seed_cron_jobs(definitions),
        return_exceptions=True,
    )

    successes = [n for n in inserted if isinstance(n, int)]
    assert sum(successes) <= 2

    repo = SqlAlchemyCronJobRepository()
    assert len(await repo.get_all_jobs()) == 2


# ── cron_job(): el task_name se deriva, no se escribe ──────────────────────────


@background_task(queue="maintenance")
async def scheduled_cleanup(days: int = 30) -> None:  # pragma: no cover
    ...


def test_cron_job_derives_task_name_and_queue_from_the_decorator():
    definition = cron_job(scheduled_cleanup, "0 4 * * *", payload={"days": 7})

    assert definition.task_name == f"{__name__}.scheduled_cleanup"
    assert definition.job_id == definition.task_name
    assert definition.queue == "maintenance"
    assert definition.payload == {"days": 7}
    assert definition.cron_expression == "0 4 * * *"


def test_cron_job_allows_overriding_job_id_and_queue():
    definition = cron_job(
        scheduled_cleanup, "0 4 * * *", job_id="nightly-cleanup", queue="low"
    )

    assert definition.job_id == "nightly-cleanup"
    assert definition.queue == "low"


def test_cron_job_rejects_an_undecorated_function():
    async def not_decorated() -> None:  # pragma: no cover
        ...

    with pytest.raises(ValueError, match="background_task"):
        cron_job(not_decorated, "0 4 * * *")


@pytest.mark.anyio
async def test_cron_job_definitions_are_seedable():
    repo = await _prepare()

    assert await seed_cron_jobs([cron_job(scheduled_cleanup, "0 4 * * *")]) == 1

    job = (await repo.get_active_jobs())[0]
    assert job.task_name == f"{__name__}.scheduled_cleanup"
    assert job.queue == "maintenance"


# ── Integración con el scheduler ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_scheduler_can_drive_the_sql_repository():
    from hexcore.application.cqrs.scheduler import DynamicScheduler
    from hexcore.domain.cqrs.task_queues import ITaskEnqueuer

    repo = await _prepare()
    await seed_cron_jobs([_definition("every_minute", cron="* * * * *")])

    class Spy(ITaskEnqueuer):
        def __init__(self) -> None:
            self.tasks: list[str] = []

        async def enqueue_command(self, *a, **k) -> None: ...
        async def enqueue_event(self, *a, **k) -> None: ...
        async def enqueue_handler(self, *a, **k) -> None: ...

        async def enqueue_task(self, task_name: str, payload: dict, queue: str) -> None:
            self.tasks.append(task_name)

    enqueuer = Spy()
    scheduler = DynamicScheduler(
        repository=repo, enqueuer=enqueuer, tick_interval_seconds=3600
    )

    task = asyncio.create_task(scheduler.start())
    await asyncio.sleep(0.1)
    scheduler.stop()
    await asyncio.wait_for(task, timeout=2)

    assert enqueuer.tasks == ["app.tasks.every_minute"]
    # El scheduler marcó la ejecución en la tabla real.
    assert (await repo.get_active_jobs())[0].last_run_at is not None
