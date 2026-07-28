"""
Servicio encargado de evaluar y encolar tareas periódicas.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from hexcore.domain.cqrs.cron import ICronJobRepository, ILockProvider
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer

logger = logging.getLogger(__name__)


class DynamicScheduler:
    """
    Evalúa constantemente un repositorio de tareas periódicas y delega la ejecución
    al TaskEnqueuer (Celery/Procrastinate). Permite cambiar configuraciones cron en "caliente".
    """

    def __init__(
        self,
        repository: ICronJobRepository,
        enqueuer: ITaskEnqueuer,
        lock_provider: ILockProvider | None = None,
        tick_interval_seconds: int = 30
    ) -> None:
        self.repository = repository
        self.enqueuer = enqueuer
        self.lock_provider = lock_provider
        self.tick_interval_seconds = tick_interval_seconds
        
        self._stop_event = asyncio.Event()

    async def start(self) -> None:
        """Inicia el bucle infinito del scheduler."""
        import croniter
        
        logger.info(f"[*] DynamicScheduler started (Tick interval: {self.tick_interval_seconds}s)")
        self._stop_event.clear()

        # El estado base es el momento en que se levantó el scheduler.
        last_check_time = datetime.now(timezone.utc)

        while not self._stop_event.is_set():
            try:
                # 1. Esperamos el tiempo del tick
                await asyncio.sleep(self.tick_interval_seconds)
                
                current_time = datetime.now(timezone.utc)
                
                # 2. Obtenemos la configuración fresca de BD
                active_jobs = await self.repository.get_active_jobs()
                
                for job in active_jobs:
                    # Determinamos desde cuándo chequear. Si el job tiene `last_run_at`, lo usamos.
                    # Sino, usamos el `last_check_time` global del bucle.
                    base_time = job.last_run_at or last_check_time
                    
                    try:
                        # 3. Comprobamos si la expresión cron cayó entre base_time y current_time
                        if croniter.croniter.match(job.cron_expression, current_time):
                            # Evitar múltiples encolados en el mismo tick (minuto)
                            # Intentar adquirir lock si hay proveedor (Distributed Locks)
                            if self.lock_provider:
                                # Usamos el minuto actual (truncado) para evitar colisiones distribuidas
                                minute_key = current_time.replace(second=0, microsecond=0).isoformat()
                                lock_key = f"hexcore:cron_lock:{job.job_id}:{minute_key}"
                                
                                lock_acquired = await self.lock_provider.acquire_lock(lock_key, ttl_seconds=60)
                                if not lock_acquired:
                                    logger.debug(f"CronJob '{job.job_id}' ignorado (lock adquirido por otra réplica).")
                                    continue
                                    
                            logger.info(f"Encolando CronJob '{job.job_id}' -> Task: {job.task_name}")
                            
                            await self.enqueuer.enqueue_task(
                                task_name=job.task_name,
                                payload=job.payload,
                                queue=job.queue
                            )
                            
                            # Actualizamos el timestamp en el repositorio
                            await self.repository.update_last_run(job.job_id, current_time)
                            
                    except Exception as e:
                        logger.error(f"Error procesando el cron job {job.job_id}: {e}")

                last_check_time = current_time

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error crítico en el bucle del scheduler: {e}")

    def stop(self) -> None:
        """Detiene el bucle del scheduler de forma segura."""
        self._stop_event.set()
