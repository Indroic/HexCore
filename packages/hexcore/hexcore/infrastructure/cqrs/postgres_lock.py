"""
Proveedor de Locks Distribuidos utilizando PostgreSQL.
Ideal para garantizar ejecución única de CronJobs cuando usas bases de datos SQL
sin necesidad de añadir Redis a tu stack.
"""
from __future__ import annotations

import logging
import typing as t

from hexcore.domain.cqrs.cron import ILockProvider

if t.TYPE_CHECKING:
    import asyncpg

logger = logging.getLogger(__name__)

LockErrorPolicy = t.Literal["skip", "raise"]


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

    **Qué pasa si Postgres no responde.** Igual que en `RedisLockProvider`:
    `on_error="skip"` (default) loguea `critical` y devuelve `False`, o sea que el
    cron se detiene mientras la BD esté caída; `on_error="raise"` propaga para que el
    supervisor del scheduler lo vea. El log distingue "no pude decidir" (`critical`)
    de "el lock estaba tomado" (`debug`).
    """

    def __init__(
        self,
        pool: asyncpg.Pool | asyncpg.Connection,
        table_name: str = "hexcore_cron_locks",
        *,
        on_error: LockErrorPolicy = "skip",
        purge_every: int = 100,
        purge_grace_seconds: int = 3600,
    ) -> None:
        """
        Args:
            pool: Pool de conexiones o conexión simple de `asyncpg`.
            table_name: Nombre de la tabla a utilizar para los locks.
            on_error: Qué hacer si Postgres no responde. Ver el docstring de la clase.
            purge_every: Cada cuántas adquisiciones purgar lo expirado. 0 desactiva.
            purge_grace_seconds: Margen antes de borrar una fila expirada. Evita
                borrar locks que acaban de vencer y que otra réplica podría estar
                evaluando en este mismo instante.
        """
        self.pool = pool
        self.table_name = table_name
        self.on_error = on_error
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
            # `asyncpg.Pool.execute` y `.fetchrow` no vienen anotados, así que cada llamada
            # se reporta `Unknown`. Los `pyright: ignore` de este módulo son eso y sólo eso —
            # deuda de asyncpg— y van estrechados a la regla exacta para que el día que la
            # librería tipe, el gate lo diga en vez de dejarlos ahí para siempre.
            await self.pool.execute(create_table)  # pyright: ignore[reportUnknownMemberType]
            await self.pool.execute(create_index)  # pyright: ignore[reportUnknownMemberType]
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
            await self.pool.execute(query, str(self.purge_grace_seconds))  # pyright: ignore[reportUnknownMemberType]
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
            False si el lock ya estaba tomado y aún no ha expirado, o si Postgres
            falló y `on_error="skip"`.

        Raises:
            Exception: El error original de asyncpg, si `on_error="raise"`.
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
            result = await self.pool.fetchrow(query, lock_key, str(ttl_seconds))  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            acquired = result is not None
        except Exception as e:
            if self.on_error == "raise":
                logger.critical(
                    "No se pudo decidir el lock de Postgres para %s: %s. "
                    "on_error='raise': se propaga.",
                    lock_key,
                    e,
                )
                raise
            # "No pude decidir" es un incidente de infraestructura, no un lock tomado.
            logger.critical(
                "No se pudo decidir el lock de Postgres para %s: %s. "
                "on_error='skip': el job NO se ejecuta en este tick.",
                lock_key,
                e,
            )
            return False

        if not acquired:
            logger.debug("Lock de Postgres %s ya estaba tomado por otro proceso.", lock_key)

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
            await self.pool.execute(query, lock_key)  # pyright: ignore[reportUnknownMemberType]
        except Exception as e:
            logger.error(f"Error intentando liberar lock de Postgres para {lock_key}: {e}")
