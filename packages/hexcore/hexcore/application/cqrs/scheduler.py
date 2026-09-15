"""
Servicio encargado de evaluar y encolar tareas periódicas.
"""
from __future__ import annotations

import asyncio
import logging
import warnings
from datetime import datetime, timedelta, timezone

from hexcore.domain.cqrs.cron import CronJobDefinition, ICronJobRepository, ILockProvider
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer

logger = logging.getLogger(__name__)


class DynamicScheduler:
    """
    Evalúa constantemente un repositorio de tareas periódicas y delega la ejecución
    al TaskEnqueuer (Celery/Procrastinate). Permite cambiar configuraciones cron en "caliente".

    **Catch-up.** La decisión no es "¿la expresión cron coincide con el minuto
    actual?" —que se salta el job cuando el tick deriva un segundo— sino "¿hubo
    alguna ocurrencia entre la última ejecución y ahora?". Eso hace que:

    - un minuto saltado por drift del tick no pierda la ejecución;
    - el mismo minuto muestreado varias veces (``tick_interval_seconds < 60``) no
      duplique el encolado, porque ``last_run_at`` deduplica de verdad.

    El catch-up se acota con ``catch_up_window_seconds`` para que un scheduler que
    estuvo caído mucho tiempo no dispare ocurrencias antiguas ni itere millones de
    minutos.
    """

    def __init__(
        self,
        repository: ICronJobRepository,
        enqueuer: ITaskEnqueuer,
        lock_provider: ILockProvider | None = None,
        tick_interval_seconds: int = 30,
        *,
        catch_up_window_seconds: int = 3600,
    ) -> None:
        self.repository = repository
        self.enqueuer = enqueuer
        self.lock_provider = lock_provider
        self.tick_interval_seconds = tick_interval_seconds
        self.catch_up_window_seconds = catch_up_window_seconds

        self._stop_event = asyncio.Event()
        # Memoria local de la última ocurrencia encolada por job. Complementa a
        # `last_run_at` del repositorio: si el repo tarda en reflejar la escritura,
        # esto evita que el mismo minuto se encole dos veces en este proceso.
        self._last_enqueued: dict[str, datetime] = {}

        if tick_interval_seconds < 60 and lock_provider is None:
            warnings.warn(
                f"DynamicScheduler con tick_interval_seconds={tick_interval_seconds} "
                "(<60) y sin lock_provider: el mismo minuto se muestrea varias veces. "
                "En este proceso la deduplicación por last_run_at lo cubre, pero con "
                "varias réplicas del scheduler el job se duplicará. Pasá un "
                "lock_provider (RedisLockProvider / PostgresLockProvider) o subí el "
                "tick a 60s.",
                RuntimeWarning,
                stacklevel=2,
            )

    async def start(self) -> None:
        """Inicia el bucle infinito del scheduler."""
        logger.info(
            "[*] DynamicScheduler started (Tick interval: %ss, catch-up window: %ss)",
            self.tick_interval_seconds,
            self.catch_up_window_seconds,
        )
        self._stop_event.clear()

        # Ancla inicial: el comienzo del minuto en curso, exclusivo. Así una
        # ocurrencia de *este* minuto cuenta como pendiente (arrancar a las 03:00:30
        # sí dispara el job de las 03:00), pero no se disparan ocurrencias
        # anteriores al arranque. Si el job tiene `last_run_at`, manda ése.
        last_check_time = _start_of_minute(datetime.now(timezone.utc)) - _EPSILON

        while not self._stop_event.is_set():
            # El tick corre *antes* del sleep: con el sleep primero se perdía el
            # primer tick entero.
            try:
                # `current_time` del tick se reutiliza como ancla del siguiente, para
                # que no quede un hueco sin cubrir entre dos ticks.
                last_check_time = await self._tick(last_check_time)
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("Error en el tick del scheduler; el bucle continúa")
                last_check_time = datetime.now(timezone.utc)

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=self.tick_interval_seconds
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    async def _tick(self, last_check_time: datetime) -> datetime:
        """
        Evalúa todos los jobs activos una vez.

        Returns:
            El `current_time` usado, para que el siguiente tick lo tome como ancla.
        """
        current_time = datetime.now(timezone.utc)
        active_jobs = await self.repository.get_active_jobs()

        for job in active_jobs:
            try:
                await self._process_job(job, last_check_time, current_time)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Error procesando el cron job %s", job.job_id)

        return current_time

    async def _process_job(
        self,
        job: CronJobDefinition,
        last_check_time: datetime,
        current_time: datetime,
    ) -> None:
        """Encola el job si le tocaba correr en algún momento desde su última ejecución."""
        base_time = self._resolve_base_time(job, last_check_time, current_time)
        occurrence = self._latest_due_occurrence(
            job.cron_expression, base_time, current_time
        )
        if occurrence is None:
            return

        # El lock se toma sobre la *ocurrencia*, no sobre el minuto actual: dos
        # réplicas con ticks desfasados compiten por la misma clave.
        if self.lock_provider:
            lock_key = f"hexcore:cron_lock:{job.job_id}:{occurrence.isoformat()}"
            acquired = await self.lock_provider.acquire_lock(lock_key, ttl_seconds=60)
            if not acquired:
                logger.debug(
                    "CronJob '%s' ignorado (lock adquirido por otra réplica).",
                    job.job_id,
                )
                return

        logger.info(
            "Encolando CronJob '%s' -> Task: %s (ocurrencia %s)",
            job.job_id,
            job.task_name,
            occurrence.isoformat(),
        )

        await self.enqueuer.enqueue_task(
            task_name=job.task_name,
            payload=job.payload,
            queue=job.queue,
        )

        # Marcar *la ocurrencia*, no `current_time`: es lo que hace que el siguiente
        # tick sepa que este minuto ya se sirvió.
        self._last_enqueued[job.job_id] = occurrence
        await self.repository.update_last_run(job.job_id, occurrence)

    def _resolve_base_time(
        self,
        job: CronJobDefinition,
        last_check_time: datetime,
        current_time: datetime,
    ) -> datetime:
        """
        Desde cuándo buscar ocurrencias pendientes.

        Se toma la más reciente entre `last_run_at`, la memoria local de este proceso
        y el arranque del bucle, y se acota con la ventana de catch-up.
        """
        candidates = [last_check_time]
        if job.last_run_at is not None:
            candidates.append(_as_utc(job.last_run_at))
        local = self._last_enqueued.get(job.job_id)
        if local is not None:
            candidates.append(local)

        base_time = max(candidates)
        window_start = current_time - timedelta(seconds=self.catch_up_window_seconds)
        return max(base_time, window_start)

    def _latest_due_occurrence(
        self,
        cron_expression: str,
        base_time: datetime,
        current_time: datetime,
    ) -> datetime | None:
        """
        Última ocurrencia de `cron_expression` en el intervalo `(base_time, current_time]`.

        Devuelve `None` si no hubo ninguna. Se encola una sola vez aunque se hayan
        saltado varias ocurrencias: para una tarea periódica, correr N veces seguidas
        para "recuperar" es casi siempre peor que correr una.
        """
        import croniter

        try:
            iterator = croniter.croniter(cron_expression, base_time)
        except (croniter.CroniterBadCronError, croniter.CroniterBadDateError) as exc:
            logger.error("Expresión cron inválida '%s': %s", cron_expression, exc)
            return None

        latest: datetime | None = None
        while True:
            try:
                candidate = iterator.get_next(datetime)
            except croniter.CroniterBadDateError:
                # Expresión que no vuelve a ocurrir (p. ej. "0 0 31 2 *").
                break
            if candidate > current_time:
                break
            latest = candidate

        return latest

    def stop(self) -> None:
        """Detiene el bucle del scheduler de forma segura."""
        self._stop_event.set()


_EPSILON = timedelta(microseconds=1)


def _as_utc(value: datetime) -> datetime:
    """Normaliza a UTC: un `last_run_at` naive de la BD rompería las comparaciones."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _start_of_minute(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0)
