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
    """

    def __init__(self, pool: asyncpg.Pool | asyncpg.Connection, table_name: str = "hexcore_cron_locks") -> None:
        """
        Args:
            pool: Pool de conexiones o conexión simple de `asyncpg`.
            table_name: Nombre de la tabla a utilizar para los locks.
        """
        self.pool = pool
        self.table_name = table_name

    async def setup(self) -> None:
        """
        Crea la tabla necesaria para los locks si no existe.
        Debe llamarse al inicio de la aplicación.
        """
        query = f"""
        CREATE TABLE IF NOT EXISTS {self.table_name} (
            lock_key TEXT PRIMARY KEY,
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL
        )
        """
        try:
            if hasattr(self.pool, "execute"):
                await self.pool.execute(query) # type: ignore
        except Exception as e:
            logger.error(f"Error creando tabla de locks en Postgres: {e}")
            raise

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
            return result is not None
        except Exception as e:
            logger.error(f"Error intentando adquirir lock de Postgres para {lock_key}: {e}")
            return False

    async def release_lock(self, lock_key: str) -> None:
        """
        Libera el lock explícitamente eliminándolo de la tabla.
        """
        query = f"DELETE FROM {self.table_name} WHERE lock_key = $1"
        try:
            await self.pool.execute(query, lock_key) # type: ignore
        except Exception as e:
            logger.error(f"Error intentando liberar lock de Postgres para {lock_key}: {e}")
