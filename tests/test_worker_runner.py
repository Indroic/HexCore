"""
F8: entrypoint del worker en una llamada, con muerte mutua y drenaje ordenado.

La semántica clave: si **cualquiera** de los bucles muere, se cancela el otro y el
proceso sale con error, para que el orquestador lo reinicie completo. Correr con un
bucle caído (encolar sin consumir, o al revés) es peor que caerse.
"""
from __future__ import annotations

import asyncio

import pytest

from hexcore.infrastructure.workers.runner import (
    WorkerDied,
    run_cqrs_worker,
    worker_loop,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


class SpyLoop:
    """Bucle que corre hasta que se le pide parar, y anota lo que le pasó."""

    def __init__(self, name: str, *, fail_after: float | None = None) -> None:
        self.name = name
        self._stop_event = asyncio.Event()
        self._fail_after = fail_after
        self.ran = False
        self.stopped = False
        self.cancelled = False

    async def run(self) -> None:
        self.ran = True
        try:
            if self._fail_after is not None:
                await asyncio.sleep(self._fail_after)
                raise RuntimeError(f"{self.name} explotó")
            await self._stop_event.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def stop(self) -> None:
        self.stopped = True
        self._stop_event.set()


class SilentlyExitingLoop:
    """Bucle que retorna por su cuenta sin excepción: también es una muerte."""

    name = "quitter"

    def __init__(self) -> None:
        self.stopped = False

    async def run(self) -> None:
        await asyncio.sleep(0.01)

    async def stop(self) -> None:
        self.stopped = True


# ── Muerte mutua ───────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_a_dying_loop_takes_down_the_others_and_raises():
    healthy = SpyLoop("healthy")
    doomed = SpyLoop("doomed", fail_after=0.01)

    with pytest.raises(WorkerDied) as exc:
        await run_cqrs_worker(healthy, doomed, handle_signals=False)

    assert "doomed" in str(exc.value)
    assert isinstance(exc.value.__cause__, RuntimeError)
    assert healthy.stopped is True, "el bucle sano no recibió el stop"


@pytest.mark.anyio
async def test_a_loop_that_returns_silently_also_kills_the_process():
    healthy = SpyLoop("healthy")
    quitter = SilentlyExitingLoop()

    with pytest.raises(WorkerDied, match="quitter"):
        await run_cqrs_worker(healthy, quitter, handle_signals=False)

    assert healthy.stopped is True


@pytest.mark.anyio
async def test_the_original_exception_is_chained():
    doomed = SpyLoop("doomed", fail_after=0.01)

    with pytest.raises(WorkerDied) as exc:
        await run_cqrs_worker(doomed, handle_signals=False)

    assert str(exc.value.__cause__) == "doomed explotó"


# ── Drenaje ordenado ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_stop_is_requested_before_cancelling():
    loop = SpyLoop("graceful")

    async def request_shutdown() -> None:
        await asyncio.sleep(0.02)
        await loop.stop()

    asyncio.create_task(request_shutdown())

    with pytest.raises(WorkerDied):
        # El bucle termina limpiamente al recibir el stop; para el runner sigue siendo
        # una muerte, porque un bucle no debe retornar en operación normal.
        await run_cqrs_worker(loop, handle_signals=False)

    assert loop.cancelled is False, "se canceló en vez de drenar"


@pytest.mark.anyio
async def test_a_loop_that_ignores_stop_is_cancelled_after_the_timeout():
    class StubbornLoop:
        name = "stubborn"

        def __init__(self) -> None:
            self.cancelled = False

        async def run(self) -> None:
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                self.cancelled = True
                raise

        async def stop(self) -> None:
            return None  # ignora la petición

    stubborn = StubbornLoop()
    doomed = SpyLoop("doomed", fail_after=0.01)

    with pytest.raises(WorkerDied):
        await run_cqrs_worker(
            stubborn, doomed, handle_signals=False, drain_timeout=0.05
        )

    assert stubborn.cancelled is True


@pytest.mark.anyio
async def test_an_error_in_stop_does_not_hide_the_original_failure():
    class BadStopLoop:
        name = "bad-stop"

        async def run(self) -> None:
            await asyncio.sleep(3600)

        async def stop(self) -> None:
            raise RuntimeError("stop falló")

    doomed = SpyLoop("doomed", fail_after=0.01)

    with pytest.raises(WorkerDied, match="doomed"):
        await run_cqrs_worker(
            BadStopLoop(), doomed, handle_signals=False, drain_timeout=0.05
        )


# ── Hooks de arranque y apagado ────────────────────────────────────────────────


@pytest.mark.anyio
async def test_startup_hooks_run_in_order_before_the_loops():
    order: list[str] = []

    async def first() -> None:
        order.append("first")

    async def second() -> None:
        order.append("second")

    class RecordingLoop(SpyLoop):
        async def run(self) -> None:
            order.append("loop")
            await super().run()

    loop = RecordingLoop("recording", fail_after=0.01)

    with pytest.raises(WorkerDied):
        await run_cqrs_worker(
            loop, on_startup=[first, second], handle_signals=False
        )

    assert order == ["first", "second", "loop"]


@pytest.mark.anyio
async def test_shutdown_hooks_run_in_declared_order():
    order: list[str] = []

    async def close_pool() -> None:
        order.append("close_pool")

    async def dispose_engine() -> None:
        order.append("dispose_engine")

    doomed = SpyLoop("doomed", fail_after=0.01)

    with pytest.raises(WorkerDied):
        await run_cqrs_worker(
            doomed,
            on_shutdown=[close_pool, dispose_engine],
            handle_signals=False,
        )

    assert order == ["close_pool", "dispose_engine"]


@pytest.mark.anyio
async def test_shutdown_hooks_run_even_if_startup_failed():
    order: list[str] = []

    async def boom() -> None:
        raise RuntimeError("no pude conectar a la BD")

    async def cleanup() -> None:
        order.append("cleanup")

    loop = SpyLoop("never-runs")

    with pytest.raises(RuntimeError, match="no pude conectar"):
        await run_cqrs_worker(
            loop, on_startup=[boom], on_shutdown=[cleanup], handle_signals=False
        )

    assert order == ["cleanup"]
    assert loop.ran is False, "el bucle arrancó pese a que el startup falló"


@pytest.mark.anyio
async def test_a_failing_shutdown_hook_does_not_stop_the_others():
    order: list[str] = []

    async def bad() -> None:
        raise RuntimeError("teardown roto")

    async def good() -> None:
        order.append("good")

    doomed = SpyLoop("doomed", fail_after=0.01)

    with pytest.raises(WorkerDied):
        await run_cqrs_worker(
            doomed, on_shutdown=[bad, good], handle_signals=False
        )

    assert order == ["good"]


# ── Scheduler integrado ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_scheduler_runs_alongside_the_loops_and_shares_their_fate():
    from hexcore.application.cqrs.scheduler import DynamicScheduler
    from hexcore.domain.cqrs.cron import ICronJobRepository
    from hexcore.domain.cqrs.task_queues import ITaskEnqueuer

    class EmptyRepo(ICronJobRepository):
        async def get_active_jobs(self):
            return []

        async def update_last_run(self, job_id, run_time) -> None: ...

    class NoopEnqueuer(ITaskEnqueuer):
        async def enqueue_command(self, *a, **k) -> None: ...
        async def enqueue_event(self, *a, **k) -> None: ...
        async def enqueue_handler(self, *a, **k) -> None: ...
        async def enqueue_task(self, *a, **k) -> None: ...

    scheduler = DynamicScheduler(
        repository=EmptyRepo(), enqueuer=NoopEnqueuer(), tick_interval_seconds=3600
    )
    doomed = SpyLoop("doomed", fail_after=0.05)

    with pytest.raises(WorkerDied, match="doomed"):
        await run_cqrs_worker(doomed, scheduler=scheduler, handle_signals=False)

    # El scheduler recibió el stop y su bucle terminó.
    assert scheduler._stop_event.is_set()


@pytest.mark.anyio
async def test_a_dying_scheduler_kills_the_worker_loop():
    class ExplodingScheduler:
        def __init__(self) -> None:
            self.stopped = False

        async def start(self) -> None:
            await asyncio.sleep(0.01)
            raise RuntimeError("el repositorio de cron no responde")

        def stop(self) -> None:
            self.stopped = True

    healthy = SpyLoop("healthy")

    with pytest.raises(WorkerDied, match="scheduler"):
        await run_cqrs_worker(
            healthy,
            scheduler=ExplodingScheduler(),  # type: ignore[arg-type]
            handle_signals=False,
        )

    assert healthy.stopped is True


# ── worker_loop() y validación ─────────────────────────────────────────────────


@pytest.mark.anyio
async def test_worker_loop_adapts_a_plain_coroutine_pair():
    events: list[str] = []
    stop_event = asyncio.Event()

    async def run() -> None:
        events.append("run")
        await stop_event.wait()

    def stop() -> None:
        events.append("stop")
        stop_event.set()

    loop = worker_loop("adapted", run, stop)
    doomed = SpyLoop("doomed", fail_after=0.02)

    with pytest.raises(WorkerDied):
        await run_cqrs_worker(loop, doomed, handle_signals=False)

    assert events == ["run", "stop"]


@pytest.mark.anyio
async def test_worker_loop_without_stop_is_cancelled():
    async def run() -> None:
        await asyncio.sleep(3600)

    loop = worker_loop("no-stop", run)
    doomed = SpyLoop("doomed", fail_after=0.01)

    with pytest.raises(WorkerDied):
        await run_cqrs_worker(loop, doomed, handle_signals=False, drain_timeout=0.05)


@pytest.mark.anyio
async def test_running_with_nothing_to_do_is_an_error():
    with pytest.raises(ValueError, match="al menos un bucle"):
        await run_cqrs_worker(handle_signals=False)
