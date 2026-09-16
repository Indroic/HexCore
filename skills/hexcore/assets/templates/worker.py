"""
The worker process.

Run this as a SEPARATE process from the API -- same image, different command. The runner
exits with WorkerDied so the orchestrator restarts it, and in the same container that would
restart your API too.
"""
from __future__ import annotations

import asyncio

import hexcore.cqrs as cqrs
from hexcore.infrastructure.task_queues.procrastinate_adapter import (
    ProcrastinateEnqueuer,
    register_hexcore_procrastinate_tasks,
)


# ---- Scheduled jobs ----------------------------------------------------------


@cqrs.background_task(queue="maintenance")
async def clean_old_records_task(days_retention: int) -> None:
    """Deletes records older than the retention window."""
    # That first docstring line becomes the job's `description`, which is what an admin panel
    # shows an operator before they disable it. With only a task name and a cron expression,
    # you cannot tell turning off something harmless from stopping invoicing.
    ...


CRON_JOBS = [
    # Pass the FUNCTION, never its name. The task name comes from __cqrs_task_name__; writing
    # it by hand is how you end up with a cron enqueuing a task that was since renamed, and
    # the failure shows up in the worker, far from the mistake.
    cqrs.cron_job(clean_old_records_task, "*/5 * * * *", payload={"days_retention": 30}),
]


async def main() -> None:
    # The SAME buses the web process uses. The consumer marks the message as "came from the
    # worker", so the bus executes it locally instead of re-enqueuing it. Building separate
    # buses for web and worker reintroduces the 2.x bug: a silent infinite loop where the
    # queue grows without bound and the handler never runs.
    consumer = cqrs.CQRSConsumer(command_bus, event_bus)

    # Registers hexcore.process_command / process_event / process_handler / process_task.
    # Idempotent -- returns False if they were already there, so calling it from both the
    # lifespan and here does not break.
    register_hexcore_procrastinate_tasks(procrastinate_app, consumer)

    enqueuer = ProcrastinateEnqueuer(procrastinate_app)

    scheduler = cqrs.DynamicScheduler(
        repository=cqrs.SqlAlchemyCronJobRepository(),
        enqueuer=enqueuer,
        # REQUIRED with more than one replica: without a lock, two replicas enqueue the same
        # job. The scheduler emits a RuntimeWarning if it sees a sub-minute tick without one.
        lock_provider=cqrs.RedisLockProvider(redis_client),
        tick_interval_seconds=30,
        # Bounds the catch-up, so a scheduler that was down for a week does not fire every
        # missed occurrence at once.
        catch_up_window_seconds=3600,
    )

    await cqrs.run_procrastinate_worker(
        procrastinate_app,
        queues=["default", "maintenance"],
        concurrency=4,
        scheduler=scheduler,
        # seed_cron_jobs is idempotent and does NOT overwrite database edits: it inserts what
        # is missing and leaves alone what is there. A seed that overwrote would revert, on
        # every deploy, the job an operator disabled at three in the morning.
        on_startup=[lambda: cqrs.seed_cron_jobs(CRON_JOBS)],
        drain_timeout=30.0,
    )
    # If ANY loop dies -- the worker's or the scheduler's -- the runner cancels the rest and
    # the process exits with WorkerDied. Running with a dead loop (enqueuing without
    # consuming, or the reverse) is worse than crashing: the queue grows, nobody notices, and
    # the process keeps reporting itself alive. SIGTERM and SIGINT become an orderly drain.


if __name__ == "__main__":
    asyncio.run(main())


# Celery instead of Procrastinate: use CeleryEnqueuer and register_hexcore_celery_tasks, and
# NEVER asyncio.run() per task -- it closes the event loop the AsyncEngine pool is bound to,
# surfacing as `Event loop is closed` in some unrelated task. Use run_in_worker_loop(coro)
# from hexcore.infrastructure.task_queues.celery_adapter, which keeps a persistent
# per-process loop, and shutdown_worker_loop(timeout=5.0) on shutdown.
