"""
Entrypoint del worker CQRS.

El `scheduler.py` que escribe cada app son ~110 líneas que hacen siempre lo mismo:
`init_db` → registrar las tasks del consumer → seed del cron → construir el
`DynamicScheduler` con su lock → abrir el pool → correr worker y scheduler concurrentes
con muerte mutua. Todo genérico.

La semántica importante, que es la que cuesta descubrir: si **cualquiera** de los dos
bucles muere, se cancela el otro y el proceso sale con error, para que el orquestador lo
reinicie completo. Correr con un bucle caído —encolar sin consumir, o consumir sin
encolar— es peor que caerse.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import typing as t

if t.TYPE_CHECKING:
    from hexcore.application.cqrs.scheduler import DynamicScheduler

logger = logging.getLogger("hexcore.workers.runner")

__all__ = [
    "WorkerLoop",
    "worker_loop",
    "run_cqrs_worker",
    "run_procrastinate_worker",
    "WorkerDied",
]


class WorkerDied(RuntimeError):
    """Uno de los bucles del worker terminó. El proceso debe reiniciarse completo."""


class WorkerLoop(t.Protocol):
    """
    Un bucle de vida larga que el runner supervisa.

    Lo cumplen el worker de la cola (Procrastinate/Celery), el `DynamicScheduler` y
    cualquier consumidor propio.
    """

    name: str

    async def run(self) -> None:
        """Corre hasta que se cancele. No debe retornar en operación normal."""
        ...

    async def stop(self) -> None:
        """Pide el apagado ordenado. Debe ser idempotente."""
        ...


class _CallableLoop:
    """Adapta un coroutine-function suelto al protocolo `WorkerLoop`."""

    def __init__(
        self,
        name: str,
        run: t.Callable[[], t.Awaitable[None]],
        stop: t.Callable[[], t.Awaitable[None]] | t.Callable[[], None] | None = None,
    ) -> None:
        self.name = name
        self._run = run
        self._stop = stop

    async def run(self) -> None:
        await self._run()

    async def stop(self) -> None:
        if self._stop is None:
            return
        result = self._stop()
        if asyncio.iscoroutine(result):
            await result


class _SchedulerLoop:
    """Adapta un `DynamicScheduler` al protocolo `WorkerLoop`."""

    name = "scheduler"

    def __init__(self, scheduler: "DynamicScheduler") -> None:
        self._scheduler = scheduler

    async def run(self) -> None:
        await self._scheduler.start()

    async def stop(self) -> None:
        self._scheduler.stop()


async def run_cqrs_worker(
    *loops: WorkerLoop,
    scheduler: "DynamicScheduler | None" = None,
    on_startup: t.Sequence[t.Callable[[], t.Awaitable[None]]] = (),
    on_shutdown: t.Sequence[t.Callable[[], t.Awaitable[None]]] = (),
    handle_signals: bool = True,
    drain_timeout: float = 30.0,
) -> None:
    """
    Corre uno o más bucles de worker con muerte mutua y drenaje ordenado.

    Args:
        *loops: Los bucles a supervisar. Típicamente el worker de la cola. Se puede
            pasar cualquier objeto con `name`, `run()` y `stop()`, o construirlos con
            `worker_loop(...)`.
        scheduler: Un `DynamicScheduler` a correr en paralelo. None → sólo los loops.
        on_startup: Corutinas a ejecutar **antes** de arrancar los bucles, en orden
            (init del engine, registro de tasks, seed del cron, apertura del pool).
        on_shutdown: Corutinas a ejecutar al final, en el orden declarado. Se ejecutan
            siempre, incluso si el arranque o un bucle falló, y un fallo en una no
            impide las siguientes ni tapa el error original.
        handle_signals: Instalar handlers de SIGTERM/SIGINT para drenaje ordenado. Es
            lo que le falta al `scheduler.py` escrito a mano: sin esto, un `docker stop`
            mata el proceso a mitad de un job.
        drain_timeout: Segundos a esperar a que los bucles terminen tras pedir el stop.

    Raises:
        WorkerDied: Si un bucle termina por su cuenta (con o sin excepción). Se propaga
            la excepción original como `__cause__`.
    """
    if not loops and scheduler is None:
        raise ValueError(
            "run_cqrs_worker necesita al menos un bucle o un scheduler; sin nada que "
            "correr, el proceso saldría de inmediato."
        )

    all_loops: list[WorkerLoop] = list(loops)
    if scheduler is not None:
        all_loops.append(t.cast(WorkerLoop, _SchedulerLoop(scheduler)))

    shutdown_requested = asyncio.Event()

    try:
        for hook in on_startup:
            logger.info("[Worker] startup: %s", _callable_name(hook))
            await hook()

        if handle_signals:
            _install_signal_handlers(shutdown_requested)

        await _supervise(all_loops, shutdown_requested, drain_timeout)
    finally:
        for hook in on_shutdown:
            logger.info("[Worker] shutdown: %s", _callable_name(hook))
            # Un teardown que falla no debe tapar el error original ni impedir los
            # teardowns siguientes.
            try:
                await hook()
            except Exception:
                logger.exception("Error en el hook de shutdown %s", _callable_name(hook))


async def _supervise(
    loops: t.Sequence[WorkerLoop],
    shutdown_requested: asyncio.Event,
    drain_timeout: float,
) -> None:
    """Corre los bucles hasta que uno muera o se pida el apagado."""
    tasks = {
        asyncio.create_task(loop.run(), name=loop.name): loop for loop in loops
    }
    waiter = asyncio.create_task(shutdown_requested.wait(), name="shutdown")

    try:
        done, _pending = await asyncio.wait(
            [*tasks, waiter], return_when=asyncio.FIRST_COMPLETED
        )
    except asyncio.CancelledError:
        await _drain(loops, tasks, drain_timeout)
        raise

    graceful = waiter in done
    dead_loop: WorkerLoop | None = None
    failure: BaseException | None = None

    # `done` mezcla los bucles con el waiter del apagado; sólo los primeros están en
    # `tasks`, así que se busca ahí en vez de indexar a ciegas.
    for candidate, loop in tasks.items():
        if candidate in done:
            dead_loop = loop
            failure = candidate.exception()
            break

    if graceful:
        logger.info("[Worker] apagado solicitado; drenando los bucles.")
    else:
        assert dead_loop is not None
        if failure is not None:
            logger.error(
                "[Worker] el bucle '%s' murió con %s: %s",
                dead_loop.name,
                type(failure).__name__,
                failure,
            )
        else:
            logger.error("[Worker] el bucle '%s' terminó por su cuenta.", dead_loop.name)

    await _drain(loops, tasks, drain_timeout)
    waiter.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await waiter

    if not graceful:
        assert dead_loop is not None
        error = WorkerDied(
            f"El bucle '{dead_loop.name}' terminó; el proceso sale para que el "
            "orquestador lo reinicie completo. Correr con un bucle caído (encolar sin "
            "consumir, o al revés) es peor que caerse."
        )
        if failure is not None:
            raise error from failure
        raise error


async def _drain(
    loops: t.Sequence[WorkerLoop],
    tasks: dict[asyncio.Task[None], WorkerLoop],
    drain_timeout: float,
) -> None:
    """Pide el stop a todos los bucles y espera, con cancelación como último recurso."""
    for loop in loops:
        try:
            await loop.stop()
        except Exception:
            logger.exception("Error pidiendo el stop del bucle '%s'", loop.name)

    pending = [task for task in tasks if not task.done()]
    if not pending:
        return

    _done, still_running = await asyncio.wait(pending, timeout=drain_timeout)
    for task in still_running:
        logger.warning(
            "[Worker] el bucle '%s' no drenó en %ss; se cancela.",
            task.get_name(),
            drain_timeout,
        )
        task.cancel()

    if still_running:
        await asyncio.gather(*still_running, return_exceptions=True)


def worker_loop(
    name: str,
    run: t.Callable[[], t.Awaitable[None]],
    stop: t.Callable[[], t.Awaitable[None]] | t.Callable[[], None] | None = None,
) -> WorkerLoop:
    """
    Envuelve un par run/stop en un `WorkerLoop`.

    Uso con Procrastinate::

        run_cqrs_worker(
            worker_loop(
                "procrastinate",
                lambda: app.run_worker_async(queues=["default"], concurrency=4),
                app.stop_worker,
            ),
            scheduler=scheduler,
        )
    """
    return t.cast(WorkerLoop, _CallableLoop(name, run, stop))


async def run_procrastinate_worker(
    app: t.Any,
    *,
    queues: t.Sequence[str] | None = None,
    concurrency: int = 4,
    scheduler: "DynamicScheduler | None" = None,
    on_startup: t.Sequence[t.Callable[[], t.Awaitable[None]]] = (),
    on_shutdown: t.Sequence[t.Callable[[], t.Awaitable[None]]] = (),
    **runner_kwargs: t.Any,
) -> None:
    """
    Atajo para el caso más común: worker de Procrastinate + scheduler opcional.

    Abre y cierra el pool de la app, y delega el resto en `run_cqrs_worker`, así que
    hereda la muerte mutua y el manejo de SIGTERM.

    Uso (el `scheduler.py` completo de una app)::

        async def main() -> None:
            consumer = CQRSConsumer(command_bus, event_bus)
            register_hexcore_procrastinate_tasks(procrastinate_app, consumer)
            await run_procrastinate_worker(
                procrastinate_app,
                queues=["default", "reactive"],
                scheduler=DynamicScheduler(repo, enqueuer, lock_provider=lock),
                on_startup=[lambda: seed_cron_jobs(CRON_JOBS)],
            )
    """
    async def _run() -> None:
        await app.run_worker_async(
            queues=list(queues) if queues else None, concurrency=concurrency
        )

    async def _stop() -> None:
        stop = getattr(app, "stop_worker", None)
        if stop is None:
            return
        result = stop()
        if asyncio.iscoroutine(result):
            await result

    async def _open_pool() -> None:
        await app.open_async()

    async def _close_pool() -> None:
        await app.close_async()

    await run_cqrs_worker(
        worker_loop("procrastinate", _run, _stop),
        scheduler=scheduler,
        on_startup=[_open_pool, *on_startup],
        on_shutdown=[*on_shutdown, _close_pool],
        **runner_kwargs,
    )


def _install_signal_handlers(shutdown_requested: asyncio.Event) -> None:
    """
    Convierte SIGTERM/SIGINT en una petición de apagado ordenado.

    En Windows `add_signal_handler` no está implementado; ahí se deja el comportamiento
    por defecto (KeyboardInterrupt para SIGINT), que el supervisor traduce igual a
    cancelación.
    """
    loop = asyncio.get_running_loop()
    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, shutdown_requested.set)
        except (NotImplementedError, RuntimeError, ValueError):
            logger.debug("No se pudo instalar el handler de %s en esta plataforma.", sig_name)


def _callable_name(func: t.Any) -> str:
    return getattr(func, "__qualname__", None) or getattr(func, "__name__", repr(func))
