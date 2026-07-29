"""
P1-1: el `DynamicScheduler` decide por catch-up real, no por "¿coincide con el
minuto actual?".

Antes, `base_time` y `last_check_time` se calculaban y no se usaban: la decisión era
`croniter.match(expr, current_time)`. Con `tick=60s` el drift acumulado terminaba
saltándose un minuto entero (y el job no corría); con `tick<60s` el mismo minuto se
muestreaba varias veces y el job se duplicaba si no había lock.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from hexcore.application.cqrs.scheduler import DynamicScheduler
from hexcore.domain.cqrs.cron import CronJobDefinition, ICronJobRepository, ILockProvider
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer


@pytest.fixture
def anyio_backend():
    return "asyncio"


class FakeRepo(ICronJobRepository):
    def __init__(self, jobs: list[CronJobDefinition]) -> None:
        self.jobs = jobs
        self.last_runs: list[tuple[str, datetime]] = []

    async def get_active_jobs(self) -> list[CronJobDefinition]:
        return self.jobs

    async def update_last_run(self, job_id: str, run_time: datetime) -> None:
        self.last_runs.append((job_id, run_time))
        for job in self.jobs:
            if job.job_id == job_id:
                job.last_run_at = run_time


class FakeEnqueuer(ITaskEnqueuer):
    def __init__(self) -> None:
        self.tasks: list[tuple[str, dict, str]] = []

    async def enqueue_command(self, command_name: str, payload: dict, queue: str) -> None:
        raise NotImplementedError

    async def enqueue_event(self, event_name: str, payload: dict, queue: str) -> None:
        raise NotImplementedError

    async def enqueue_handler(self, handler_name: str, payload: dict, queue: str) -> None:
        raise NotImplementedError

    async def enqueue_task(self, task_name: str, payload: dict, queue: str) -> None:
        self.tasks.append((task_name, payload, queue))


class SharedLockProvider(ILockProvider):
    """Lock in-memory compartido: simula dos réplicas contra el mismo Redis."""

    def __init__(self) -> None:
        self.held: set[str] = set()
        self.attempts: list[str] = []

    async def acquire_lock(self, lock_key: str, ttl_seconds: int) -> bool:
        self.attempts.append(lock_key)
        if lock_key in self.held:
            return False
        self.held.add(lock_key)
        return True

    async def release_lock(self, lock_key: str) -> None:
        self.held.discard(lock_key)


def _job(cron: str = "* * * * *", **kwargs) -> CronJobDefinition:
    defaults = dict(job_id="j1", task_name="my_task", cron_expression=cron)
    defaults.update(kwargs)
    return CronJobDefinition(**defaults)  # type: ignore[arg-type]


def _make(repo: FakeRepo, **kwargs) -> tuple[DynamicScheduler, FakeEnqueuer]:
    enqueuer = FakeEnqueuer()
    kwargs.setdefault("tick_interval_seconds", 60)
    scheduler = DynamicScheduler(repository=repo, enqueuer=enqueuer, **kwargs)
    return scheduler, enqueuer


# ── Catch-up: el minuto saltado no se pierde ───────────────────────────────────


@pytest.mark.anyio
async def test_job_runs_even_if_its_minute_was_skipped():
    """
    El caso que motivó el ítem: un cierre contable diario cuyo minuto exacto se
    saltó por drift del tick. La ocurrencia sigue pendiente y debe encolarse.
    """
    now = datetime(2026, 7, 29, 3, 5, 0, tzinfo=timezone.utc)
    # El job era a las 03:00. El tick anterior fue a las 02:59:50, el actual a las
    # 03:05: 03:00 quedó entre ambos y `croniter.match(expr, 03:05)` no lo vería.
    repo = FakeRepo([_job("0 3 * * *")])
    scheduler, enqueuer = _make(repo)

    await scheduler._process_job(
        repo.jobs[0],
        last_check_time=now - timedelta(minutes=5, seconds=10),
        current_time=now,
    )

    assert len(enqueuer.tasks) == 1
    assert repo.last_runs == [("j1", datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc))]


@pytest.mark.anyio
async def test_job_is_not_enqueued_when_no_occurrence_is_due():
    now = datetime(2026, 7, 29, 3, 5, 0, tzinfo=timezone.utc)
    repo = FakeRepo([_job("0 3 * * *")])
    scheduler, enqueuer = _make(repo)

    # El job ya corrió a las 03:00; entre 03:00 y 03:05 no hay otra ocurrencia.
    repo.jobs[0].last_run_at = datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc)

    await scheduler._process_job(
        repo.jobs[0],
        last_check_time=now - timedelta(minutes=1),
        current_time=now,
    )

    assert enqueuer.tasks == []


@pytest.mark.anyio
async def test_catch_up_window_bounds_ancient_occurrences():
    """Un scheduler caído mucho tiempo no debe disparar ocurrencias antiguas."""
    now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
    repo = FakeRepo([_job("0 3 * * *")])
    # Última ejecución hace un mes; la ventana de catch-up es de 1h por defecto.
    repo.jobs[0].last_run_at = now - timedelta(days=30)
    scheduler, enqueuer = _make(repo, catch_up_window_seconds=3600)

    await scheduler._process_job(
        repo.jobs[0], last_check_time=now - timedelta(days=30), current_time=now
    )

    assert enqueuer.tasks == []


@pytest.mark.anyio
async def test_multiple_missed_occurrences_enqueue_only_once():
    """Recuperar N veces seguidas es peor que recuperar una."""
    now = datetime(2026, 7, 29, 3, 30, 0, tzinfo=timezone.utc)
    repo = FakeRepo([_job("* * * * *")])  # 30 ocurrencias perdidas
    scheduler, enqueuer = _make(repo)

    await scheduler._process_job(
        repo.jobs[0], last_check_time=now - timedelta(minutes=30), current_time=now
    )

    assert len(enqueuer.tasks) == 1
    # Se marca la ocurrencia más reciente, no la más antigua.
    assert repo.last_runs[0][1] == now


# ── Deduplicación con tick < 60s ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_sub_minute_ticks_do_not_duplicate_the_same_occurrence():
    repo = FakeRepo([_job("* * * * *")])
    scheduler, enqueuer = _make(
        repo, tick_interval_seconds=15, lock_provider=SharedLockProvider()
    )

    anchor = datetime(2026, 7, 29, 3, 0, 0, tzinfo=timezone.utc) - timedelta(microseconds=1)
    # Cuatro muestreos del mismo minuto, como haría un tick de 15s.
    for offset in (1, 16, 31, 46):
        await scheduler._process_job(
            repo.jobs[0],
            last_check_time=anchor,
            current_time=datetime(2026, 7, 29, 3, 0, offset, tzinfo=timezone.utc),
        )

    assert len(enqueuer.tasks) == 1, "el mismo minuto se encoló más de una vez"


@pytest.mark.anyio
async def test_local_memory_dedupes_even_if_repository_does_not_persist():
    """Si el repo ignora `update_last_run`, la memoria local sigue deduplicando."""

    class ForgetfulRepo(FakeRepo):
        async def update_last_run(self, job_id: str, run_time: datetime) -> None:
            return None  # no persiste nada

    repo = ForgetfulRepo([_job("* * * * *")])
    scheduler, enqueuer = _make(repo, tick_interval_seconds=60)

    anchor = datetime(2026, 7, 29, 3, 0, 0, tzinfo=timezone.utc) - timedelta(microseconds=1)
    for offset in (1, 20, 40):
        await scheduler._process_job(
            repo.jobs[0],
            last_check_time=anchor,
            current_time=datetime(2026, 7, 29, 3, 0, offset, tzinfo=timezone.utc),
        )

    assert len(enqueuer.tasks) == 1


# ── Locks: dos réplicas, un solo encolado ──────────────────────────────────────


@pytest.mark.anyio
async def test_two_concurrent_schedulers_with_a_shared_lock_enqueue_once():
    lock = SharedLockProvider()
    now = datetime(2026, 7, 29, 3, 0, 30, tzinfo=timezone.utc)
    anchor = datetime(2026, 7, 29, 3, 0, 0, tzinfo=timezone.utc) - timedelta(microseconds=1)

    repo_a = FakeRepo([_job("* * * * *")])
    repo_b = FakeRepo([_job("* * * * *")])
    scheduler_a, enqueuer_a = _make(repo_a, lock_provider=lock)
    scheduler_b, enqueuer_b = _make(repo_b, lock_provider=lock)

    await asyncio.gather(
        scheduler_a._process_job(repo_a.jobs[0], anchor, now),
        scheduler_b._process_job(repo_b.jobs[0], anchor, now),
    )

    assert len(enqueuer_a.tasks) + len(enqueuer_b.tasks) == 1


@pytest.mark.anyio
async def test_lock_key_is_built_from_the_occurrence_not_the_current_minute():
    """
    Dos réplicas con ticks desfasados (una a las 03:00:59, otra a las 03:01:01)
    deben competir por la misma clave, o el job se duplica.
    """
    lock = SharedLockProvider()
    anchor = datetime(2026, 7, 29, 3, 0, 0, tzinfo=timezone.utc) - timedelta(microseconds=1)

    repo = FakeRepo([_job("0 3 * * *")])
    scheduler, enqueuer = _make(repo, lock_provider=lock)

    await scheduler._process_job(
        repo.jobs[0], anchor, datetime(2026, 7, 29, 3, 0, 59, tzinfo=timezone.utc)
    )

    assert lock.attempts == ["hexcore:cron_lock:j1:2026-07-29T03:00:00+00:00"]
    assert len(enqueuer.tasks) == 1


# ── Avisos y robustez ──────────────────────────────────────────────────────────


def test_warns_when_sub_minute_tick_has_no_lock_provider():
    repo = FakeRepo([])
    with pytest.warns(RuntimeWarning, match="lock_provider"):
        DynamicScheduler(
            repository=repo, enqueuer=FakeEnqueuer(), tick_interval_seconds=30
        )


def test_does_not_warn_with_lock_provider():
    repo = FakeRepo([])
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        DynamicScheduler(
            repository=repo,
            enqueuer=FakeEnqueuer(),
            tick_interval_seconds=30,
            lock_provider=SharedLockProvider(),
        )


def test_does_not_warn_with_minute_tick():
    repo = FakeRepo([])
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("error")
        DynamicScheduler(
            repository=repo, enqueuer=FakeEnqueuer(), tick_interval_seconds=60
        )


@pytest.mark.anyio
async def test_invalid_cron_expression_is_logged_and_skipped(caplog):
    repo = FakeRepo([_job("no soy un cron")])
    scheduler, enqueuer = _make(repo)
    now = datetime(2026, 7, 29, 3, 0, 0, tzinfo=timezone.utc)

    await scheduler._process_job(repo.jobs[0], now - timedelta(minutes=5), now)

    assert enqueuer.tasks == []
    assert any("inválida" in record.getMessage() for record in caplog.records)


@pytest.mark.anyio
async def test_naive_last_run_at_is_treated_as_utc():
    """Un `last_run_at` naive de la BD no debe romper las comparaciones."""
    now = datetime(2026, 7, 29, 3, 5, 0, tzinfo=timezone.utc)
    repo = FakeRepo([_job("0 3 * * *")])
    repo.jobs[0].last_run_at = datetime(2026, 7, 29, 3, 0, 0)  # sin tzinfo
    scheduler, enqueuer = _make(repo)

    await scheduler._process_job(repo.jobs[0], now - timedelta(minutes=10), now)

    assert enqueuer.tasks == []


@pytest.mark.anyio
async def test_one_failing_job_does_not_stop_the_others():
    class BoomEnqueuer(FakeEnqueuer):
        async def enqueue_task(self, task_name: str, payload: dict, queue: str) -> None:
            if task_name == "boom":
                raise RuntimeError("broker down")
            await super().enqueue_task(task_name, payload, queue)

    repo = FakeRepo(
        [
            _job("* * * * *", job_id="bad", task_name="boom"),
            _job("* * * * *", job_id="good", task_name="fine"),
        ]
    )
    scheduler = DynamicScheduler(
        repository=repo, enqueuer=BoomEnqueuer(), tick_interval_seconds=60
    )
    enqueuer = scheduler.enqueuer

    anchor = datetime(2026, 7, 29, 3, 0, 0, tzinfo=timezone.utc) - timedelta(microseconds=1)
    await scheduler._tick(anchor)

    assert [name for name, _p, _q in enqueuer.tasks] == ["fine"]  # type: ignore[attr-defined]


@pytest.mark.anyio
async def test_first_tick_runs_without_waiting_for_the_interval():
    """El sleep era la primera sentencia del bucle: se perdía el primer tick."""
    repo = FakeRepo([_job("* * * * *")])
    scheduler, enqueuer = _make(repo, tick_interval_seconds=3600)

    task = asyncio.create_task(scheduler.start())
    await asyncio.sleep(0.05)
    scheduler.stop()
    await asyncio.wait_for(task, timeout=1)

    assert len(enqueuer.tasks) == 1


@pytest.mark.anyio
async def test_stop_interrupts_the_wait_without_waiting_the_full_interval():
    repo = FakeRepo([])
    scheduler, _enqueuer = _make(repo, tick_interval_seconds=3600)

    task = asyncio.create_task(scheduler.start())
    await asyncio.sleep(0.05)
    scheduler.stop()

    # Si el sleep no fuera interrumpible, esto haría timeout.
    await asyncio.wait_for(task, timeout=1)
