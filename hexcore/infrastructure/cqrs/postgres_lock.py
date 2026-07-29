"""
Proveedor de Locks Distribuidos utilizando PostgreSQL.
Ideal para garantizar ejecución única de CronJobs cuando usas bases de datos SQL
sin necesidad de añadir Redis a tu stack.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from hexcore.domain.cqrs.cron import ILockProvider

if TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)


class PostgresLockProvider(ILockProvider):
    """
    Implementación de ILockProvider basada en PostgreSQL (vía asyncpg).
    Utiliza una tabla `hexcore_cron_locks` para gestionar la exclusión mutua
    con soporte para TTL atómico.

    **Crecimiento de la tabla.** El `DynamicScheduler` usa una clave por
    `(job_id, minuto)`, así que cada minuto genera filas nuevas: con 7 jobs son del
    orden de 10.000 filas/día. Las filas expiradas ya no sirven para nada, así que
    este provider las purga solo:

    - en `setup()`,
    - y cada `purge_every` adquisiciones (por defecto 100, ~1 query extra por 100).

    `purge_expired()` es pública si preferís purgar desde un job propio; poné
    `purge_every=0` para desactivar la purga automática.
    """

    def __init__(
        self,
        pool: asyncpg.Pool | asyncpg.Connection,
        table_name: str = "hexcore_cron_locks",
        *,
        purge_every: int = 100,
        purge_grace_seconds: int = 3600,
    ) -> None:
        """
        Args:
            pool: Pool de conexiones o conexión simple de `asyncpg`.
            table_name: Nombre de la tabla a utilizar para los locks.
            purge_every: Cada cuántas adquisiciones purgar lo expirado. 0 desactiva.
            purge_grace_seconds: Margen antes de borrar una fila expirada. Evita
                borrar locks que acaban de vencer y que otra réplica podría estar
                evaluando en este mismo instante.
        """
        self.pool = pool
        self.table_name = table_name
        self.purge_every = purge_every
        self.purge_grace_seconds = purge_grace_seconds
        self._acquisitions_since_purge = 0

    async def setup(self) -> None:
        """
        Crea la tabla y el índice necesarios para los locks si no existen,
        y purga lo que haya quedado de ejecuciones anteriores.
        Debe llamarse al inicio de la aplicación.
        """
        if not hasattr(self.pool, "execute"):
            return

        create_table = f"""
        CREATE TABLE IF NOT EXISTS {self.table_name} (
            lock_key TEXT PRIMARY KEY,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL
        )
        """
        # El índice es lo que hace que la purga no degrade con el tamaño de la tabla.
        create_index = f"""
        CREATE INDEX IF NOT EXISTS {self.table_name}_expires_at_idx
        ON {self.table_name} (expires_at)
        """
        try:
            await self.pool.execute(create_table) # type: ignore
            await self.pool.execute(create_index) # type: ignore
        except Exception as e:
            logger.error(f"Error creando tabla de locks en Postgres: {e}")
            raise

        await self.purge_expired()

    async def purge_expired(self) -> None:
        """
        Borra las filas cuyo `expires_at` venció hace más de `purge_grace_seconds`.

        No propaga errores: una purga fallida no debe tumbar el scheduler.
        """
        query = f"""
        DELETE FROM {self.table_name}
        WHERE expires_at < NOW() - ($1 || ' seconds')::interval
        """
        try:
            await self.pool.execute(query, str(self.purge_grace_seconds)) # type: ignore
            self._acquisitions_since_purge = 0
        except Exception as e:
            logger.warning(f"Error purgando locks expirados de Postgres: {e}")

    async def acquire_lock(self, lock_key: str, ttl_seconds: int) -> bool:
        """
        Intenta adquirir un lock en Postgres.
        
        Args:
            lock_key: La clave única del lock.
            ttl_seconds: Tiempo de vida del lock en segundos (evita deadlocks).
            
        Returns:
            True si el lock fue adquirido con éxito.
            False si el lock ya estaba tomado y aún no ha expirado.
        """
        # Usamos UPSERT (ON CONFLICT) condicional
        # Si no existe, lo inserta.
        # Si existe pero expiró, lo actualiza y lo toma.
        # Si existe y NO expiró, no hace nada (y no retorna fila).
        query = f"""
        INSERT INTO {self.table_name} (lock_key, expires_at)
        VALUES ($1, NOW() + ($2 || ' seconds')::interval)
        ON CONFLICT (lock_key) DO UPDATE
        SET expires_at = NOW() + ($2 || ' seconds')::interval
        WHERE {self.table_name}.expires_at < NOW()
        RETURNING lock_key;
        """
        
        try:
            # fetchrow returns a record if RETURNING gave something, else None
            result = await self.pool.fetchrow(query, lock_key, str(ttl_seconds)) # type: ignore
            acquired = result is not None
        except Exception as e:
            logger.error(f"Error intentando adquirir lock de Postgres para {lock_key}: {e}")
            return False

        await self._maybe_purge()
        return acquired

    async def _maybe_purge(self) -> None:
        """Purga amortizada: una query extra cada `purge_every` adquisiciones."""
        if self.purge_every <= 0:
            return
        self._acquisitions_since_purge += 1
        if self._acquisitions_since_purge >= self.purge_every:
            await self.purge_expired()

    async def release_lock(self, lock_key: str) -> None:
        """
        Libera el lock explícitamente eliminándolo de la tabla.
        """
        query = f"DELETE FROM {self.table_name} WHERE lock_key = $1"
        try:
            await self.pool.execute(query, lock_key) # type: ignore
        except Exception as e:
            logger.error(f"Error intentando liberar lock de Postgres para {lock_key}: {e}")
