"""
Proveedor de Locks Distribuidos utilizando Redis.
Ideal para garantizar ejecución única de CronJobs en múltiples réplicas.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from hexcore.domain.cqrs.cron import ILockProvider

if TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)


class RedisLockProvider(ILockProvider):
    """
    Implementación de ILockProvider basada en Redis.
    Utiliza el comando SET NX EX para asegurar exclusión mutua de manera atómica
    y evitar "deadlocks" mediante la expiración (TTL).
    """

    def __init__(self, redis_client: Redis) -> None:
        """
        Args:
            redis_client: Instancia activa de `redis.asyncio.Redis`.
        """
        self.redis = redis_client

    async def acquire_lock(self, lock_key: str, ttl_seconds: int) -> bool:
        """
        Intenta adquirir un lock en Redis.
        
        Args:
            lock_key: La clave única del lock (ej. "hexcore:cron_lock:job_1:2026-07-28T01:02:00").
            ttl_seconds: Tiempo de vida del lock en segundos (evita deadlocks).
            
        Returns:
            True si el lock fue adquirido con éxito (nadie lo tenía).
            False si el lock ya estaba tomado por otro proceso.
        """
        try:
            # SET key "1" NX (solo si no existe) EX ttl_seconds (expira automáticamente)
            # Retorna True (si lo seteó) o None/False (si ya existía)
            result = await self.redis.set(lock_key, "locked", nx=True, ex=ttl_seconds)
            return bool(result)
        except Exception as e:
            logger.error(f"Error intentando adquirir lock de Redis para {lock_key}: {e}")
            # En caso de error de Redis, asumimos False para evitar ejecución duplicada por dudas,
            # o podríamos dejar que explote. En un scheduler, es más seguro no correr.
            return False

    async def release_lock(self, lock_key: str) -> None:
        """
        Libera el lock explícitamente.
        """
        try:
            await self.redis.delete(lock_key)
        except Exception as e:
            logger.error(f"Error intentando liberar lock de Redis para {lock_key}: {e}")
