import asyncio
from datetime import datetime, timezone, timedelta
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import croniter

from hexcore.domain.cqrs.cron import CronJobDefinition, ICronJobRepository, ILockProvider
from hexcore.application.cqrs.scheduler import DynamicScheduler


class MockCronRepository(ICronJobRepository):
    def __init__(self, jobs: list[CronJobDefinition]):
        self.jobs = jobs
        self.update_last_run_mock = AsyncMock()
        
    async def get_active_jobs(self) -> list[CronJobDefinition]:
        return self.jobs
        
    async def update_last_run(self, job_id: str, run_time: datetime) -> None:
        await self.update_last_run_mock(job_id, run_time)


class MockLockProvider(ILockProvider):
    def __init__(self, acquire_result: bool = True):
        self.acquire_result = acquire_result
        self.acquire_mock = AsyncMock(return_value=self.acquire_result)
        
    async def acquire_lock(self, lock_key: str, ttl_seconds: int) -> bool:
        return await self.acquire_mock(lock_key, ttl_seconds)
        
    async def release_lock(self, lock_key: str) -> None:
        pass


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_dynamic_scheduler_enqueues_matched_jobs():
    enqueuer = AsyncMock()
    
    # Trabajo que DEBE correr cada minuto
    job1 = CronJobDefinition(
        job_id="1",
        task_name="my_task",
        cron_expression="* * * * *",
        payload={"msg": "hello"}
    )
    
    # Trabajo que NO debería correr (el 31 de Febrero nunca ocurre)
    job2 = CronJobDefinition(
        job_id="2",
        task_name="impossible_task",
        cron_expression="0 0 31 2 *",
        payload={}
    )
    
    repo = MockCronRepository([job1, job2])
    
    scheduler = DynamicScheduler(repository=repo, enqueuer=enqueuer, tick_interval_seconds=1)
    
    # Correr scheduler en background
    task = asyncio.create_task(scheduler.start())
    
    # Dejar que itere 1 vez
    await asyncio.sleep(1.1)
    
    # Detenerlo
    scheduler.stop()
    await task
        
    # Verificar que job1 se encoló (al menos 1 vez si iteró rápido)
    enqueuer.enqueue_task.assert_awaited()
    
    # Verificar que se actualizó su fecha
    repo.update_last_run_mock.assert_awaited()
    args = repo.update_last_run_mock.call_args[0]
    assert args[0] == "1"


@pytest.mark.anyio
async def test_dynamic_scheduler_skips_when_lock_not_acquired():
    enqueuer = AsyncMock()
    job = CronJobDefinition(
        job_id="1",
        task_name="my_task",
        cron_expression="* * * * *",
    )
    repo = MockCronRepository([job])
    
    # Proveedor de locks que siempre falla (simulando que otra réplica lo tomó)
    lock_provider = MockLockProvider(acquire_result=False)
    
    scheduler = DynamicScheduler(
        repository=repo, 
        enqueuer=enqueuer, 
        lock_provider=lock_provider, 
        tick_interval_seconds=1
    )
    
    task = asyncio.create_task(scheduler.start())
    await asyncio.sleep(1.1)
    scheduler.stop()
    await task
    
    # Verificar que se intentó adquirir el lock
    lock_provider.acquire_mock.assert_awaited()
    
    # Verificar que NO se encoló la tarea porque el lock falló
    enqueuer.enqueue_task.assert_not_awaited()
    
    # Verificar que NO se actualizó la fecha
    repo.update_last_run_mock.assert_not_awaited()
