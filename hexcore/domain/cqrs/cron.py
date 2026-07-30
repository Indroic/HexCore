"""
Definiciones de Dominio para el agendamiento dinámico de Tareas Periódicas (Cronjobs).
"""
import abc
import typing as t
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class CronJobDefinition:
    """
    Representa la definición de una tarea periódica que debe ejecutarse.
    Esta configuración normalmente se guarda en una BD para permitir
    cambios "en caliente" (hot reload).
    """
    job_id: str
    task_name: str
    cron_expression: str
    payload: dict[str, t.Any] = field(default_factory=dict)
    queue: str = "default"
    is_active: bool = True

    # Para control de estado si se usa sin locks distribuidos
    last_run_at: datetime | None = None

    # Qué hace el job, en prosa. No lo usa el scheduler: existe para el humano que ve la
    # tabla en un panel de administración y tiene que decidir si desactivar un cron. Sin
    # esto sólo se ve `task_name` y una expresión cron, que no dicen si apagarlo es
    # inofensivo o deja de facturar.
    #
    # Va al final del dataclass a propósito: intercalarlo cambiaría el significado de los
    # argumentos posicionales de quien ya construye definiciones a mano.
    description: str | None = None


class ICronJobRepository(abc.ABC):
    """
    Repositorio que el usuario debe implementar (ej. usando SQLAlchemy, Beanie, etc.)
    para proveer al DynamicScheduler la lista actualizada de tareas a ejecutar.
    """

    @abc.abstractmethod
    async def get_active_jobs(self) -> list[CronJobDefinition]:
        """
        Retorna la lista de trabajos activos.
        """
        pass

    @abc.abstractmethod
    async def update_last_run(self, job_id: str, run_time: datetime) -> None:
        """
        Actualiza la fecha de última ejecución de un job para evitar
        que se ejecute repetidamente en el mismo tick.
        """
        pass


class ILockProvider(abc.ABC):
    """
    Proveedor de Locks Distribuidos (ej. Redis, PostgreSQL Advisory Locks).
    Utilizado por el DynamicScheduler para garantizar que múltiples réplicas
    del scheduler no ejecuten el mismo cronjob de manera duplicada.
    """
    
    @abc.abstractmethod
    async def acquire_lock(self, lock_key: str, ttl_seconds: int) -> bool:
        """
        Intenta adquirir un lock. Retorna True si lo consiguió, False si ya estaba tomado.
        """
        pass
        
    @abc.abstractmethod
    async def release_lock(self, lock_key: str) -> None:
        """
        Libera el lock explícitamente (opcional, ya que el TTL lo hace automáticamente).
        """
        pass
