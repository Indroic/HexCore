"""
Proveedor de Locks Distribuidos utilizando Redis.
Ideal para garantizar ejecución única de CronJobs en múltiples réplicas.
"""
from __future__ import annotations

import logging
import typing as t

from hexcore.domain.cqrs.cron import ILockProvider

if t.TYPE_CHECKING:
    from redis.asyncio import Redis

logger = logging.getLogger(__name__)

LockErrorPolicy = t.Literal["skip", "raise"]


class RedisLockProvider(ILockProvider):
    """
    Implementación de ILockProvider basada en Redis.
    Utiliza el comando SET NX EX para asegurar exclusión mutua de manera atómica
    y evitar "deadlocks" mediante la expiración (TTL).

    **Qué pasa si Redis se cae.** `acquire_lock` no puede decidir, y las dos
    respuestas posibles son malas de formas distintas: devolver `False` apaga el cron
    completo (ningún job corre), devolver `True` deja que corran todas las réplicas
    (jobs duplicados). El default es `on_error="skip"` (no correr), pero la decisión
    es tuya y explícita:

    - ``on_error="skip"``: log `critical` y `False`. El cron se detiene mientras Redis
      esté caído. Correcto si duplicar es peor que no correr (cobros, envío de mail).
    - ``on_error="raise"``: propaga la excepción para que el supervisor del scheduler
      la vea y reinicie o alerte. Correcto si prefieres caerte ruidosamente.

    El log distingue "no pude decidir" (`critical`) de "el lock estaba tomado"
    (`debug`), que antes eran indistinguibles.
    """

    def __init__(
        self,
        redis_client: Redis,
        *,
        on_error: LockErrorPolicy = "skip",
    ) -> None:
        """
        Args:
            redis_client: Instancia activa de `redis.asyncio.Redis`.
            on_error: Qué hacer si Redis no responde. Ver el docstring de la clase.
        """
        self.redis = redis_client
        self.on_error = on_error

    async def acquire_lock(self, lock_key: str, ttl_seconds: int) -> bool:
        """
        Intenta adquirir un lock en Redis.

        Args:
            lock_key: La clave única del lock (ej. "hexcore:cron_lock:job_1:2026-07-28T01:02:00").
            ttl_seconds: Tiempo de vida del lock en segundos (evita deadlocks).

        Returns:
            True si el lock fue adquirido con éxito (nadie lo tenía).
            False si el lock ya estaba tomado por otro proceso, o si Redis falló y
            `on_error="skip"`.

        Raises:
            Exception: El error original de Redis, si `on_error="raise"`.
        """
        try:
            # SET key "1" NX (solo si no existe) EX ttl_seconds (expira automáticamente)
            # Retorna True (si lo seteó) o None/False (si ya existía)
            result = await self.redis.set(lock_key, "locked", nx=True, ex=ttl_seconds)
        except Exception as e:
            if self.on_error == "raise":
                logger.critical(
                    "No se pudo decidir el lock de Redis para %s: %s. "
                    "on_error='raise': se propaga.",
                    lock_key,
                    e,
                )
                raise
            # "No pude decidir" es un incidente de infraestructura, no un lock tomado:
            # va a critical para que se distinga en los logs y en las alertas.
            logger.critical(
                "No se pudo decidir el lock de Redis para %s: %s. "
                "on_error='skip': el job NO se ejecuta en este tick.",
                lock_key,
                e,
            )
            return False

        acquired = bool(result)
        if not acquired:
            logger.debug("Lock de Redis %s ya estaba tomado por otro proceso.", lock_key)
        return acquired

    async def release_lock(self, lock_key: str) -> None:
        """
        Libera el lock explícitamente.

        No propaga nunca: el TTL libera el lock igual, así que un fallo aquí es un
        retraso, no una pérdida de corrección.
        """
        try:
            await self.redis.delete(lock_key)
        except Exception as e:
            logger.error(f"Error intentando liberar lock de Redis para {lock_key}: {e}")
