"""
Implementación SQL de `ICronJobRepository`: modelo, repositorio y seed.

HexCore define `ICronJobRepository` y `CronJobDefinition` pero no traía ninguna
implementación, así que **todo el mundo escribe la misma tabla**. Esto es esa tabla.

Dos detalles que hay que descubrir a base de golpes y que este módulo encapsula:

- `CronJobModel` **no** hereda de `BaseModel[T]`. No tiene entidad de dominio detrás, y
  si heredara, el `collect_domain_entities()` del UoW intentaría sacarle una entidad
  (`isinstance(model, BaseModel)`) y explotaría.
- `SqlAlchemyCronJobRepository` **no** hereda de `BaseSQLAlchemyRepository`. Si heredara,
  el auto-discovery lo inyectaría en todos los UoW como si fuera un repositorio de
  dominio.

Requiere el extra ``[sql]``.
"""
from __future__ import annotations

import logging
import typing as t
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    JSON,
    String,
    select,
    update,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from hexcore.domain.cqrs.cron import CronJobDefinition, ICronJobRepository
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base

logger = logging.getLogger("hexcore.cqrs.cron_sql")

__all__ = [
    "CronJobModelMixin",
    "CronJobModel",
    "SqlAlchemyCronJobRepository",
    "seed_cron_jobs",
    "create_cron_tables",
    "cron_job",
]

DEFAULT_TABLE_NAME = "hexcore_cron_jobs"


class CronJobModelMixin:
    """
    Columnas de la tabla de cronjobs, sin `__tablename__`.

    Para usar otro nombre de tabla, componé tu propio modelo::

        class CronJob(CronJobModelMixin, Base):
            __tablename__ = "cron_jobs"

    y pasáselo al repositorio: ``SqlAlchemyCronJobRepository(model=CronJob)``.
    """

    job_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_name: Mapped[str] = mapped_column(String(255), nullable=False)
    cron_expression: Mapped[str] = mapped_column(String(128), nullable=False)
    queue: Mapped[str] = mapped_column(String(64), nullable=False, default="default")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, default=None
    )
    # Nullable: las tablas que ya existen no tienen la columna, y una migración que la
    # añadiera NOT NULL exigiría inventarle una descripción a cada job ya sembrado.
    description: Mapped[str | None] = mapped_column(
        String(512), nullable=True, default=None
    )

    @declared_attr
    def payload(cls) -> Mapped[dict[str, t.Any]]:  # noqa: N805
        # `declared_attr` porque JSON con default mutable tiene que crearse por clase.
        return mapped_column(JSON, nullable=False, default=dict)

    def to_definition(self) -> CronJobDefinition:
        """Convierte la fila en el `CronJobDefinition` que consume el scheduler."""
        return CronJobDefinition(
            job_id=self.job_id,
            task_name=self.task_name,
            cron_expression=self.cron_expression,
            payload=dict(self.payload or {}),
            queue=self.queue,
            is_active=self.is_active,
            last_run_at=self.last_run_at,
            description=self.description,
        )


class CronJobModel(CronJobModelMixin, Base):
    """Tabla de cronjobs por defecto (`hexcore_cron_jobs`)."""

    __tablename__ = DEFAULT_TABLE_NAME


AnyCronJobModel = t.Type[t.Any]


class SqlAlchemyCronJobRepository(ICronJobRepository):
    """
    `ICronJobRepository` sobre SQLAlchemy.

    Abre una sesión por operación con `session_scope()` en vez de guardar una: el
    scheduler es un proceso de vida larga y mantener una sesión abierta durante horas
    es exactamente cómo se acumulan transacciones idle-in-transaction. `session_scope`
    tampoco paga el auto-discovery de repositorios de dominio, que para leer esta tabla
    no aporta nada.
    """

    def __init__(
        self,
        model: AnyCronJobModel = CronJobModel,
        session_scope: t.Callable[[], t.AsyncContextManager[AsyncSession]] | None = None,
    ) -> None:
        """
        Args:
            model: El modelo de la tabla. Por defecto `CronJobModel`.
            session_scope: Factory de sesiones. Por defecto, el `session_scope` de
                HexCore. Inyectable para tests o para apuntar a otro engine.
        """
        self._model = model
        self._session_scope = session_scope or _default_session_scope

    async def get_active_jobs(self) -> list[CronJobDefinition]:
        async with self._session_scope() as session:
            result = await session.execute(
                select(self._model).where(self._model.is_active.is_(True))
            )
            return [row.to_definition() for row in result.scalars().all()]

    async def get_all_jobs(self) -> list[CronJobDefinition]:
        """Todos los jobs, activos o no. Útil para un endpoint de administración."""
        async with self._session_scope() as session:
            result = await session.execute(select(self._model))
            return [row.to_definition() for row in result.scalars().all()]

    async def update_last_run(self, job_id: str, run_time: datetime) -> None:
        async with self._session_scope() as session:
            await session.execute(
                update(self._model)
                .where(self._model.job_id == job_id)
                .values(last_run_at=_as_utc(run_time))
            )
            await session.commit()

    async def set_active(self, job_id: str, is_active: bool) -> None:
        """Activa o desactiva un job en caliente."""
        async with self._session_scope() as session:
            await session.execute(
                update(self._model)
                .where(self._model.job_id == job_id)
                .values(is_active=is_active)
            )
            await session.commit()


async def seed_cron_jobs(
    jobs: t.Sequence[CronJobDefinition],
    *,
    model: AnyCronJobModel = CronJobModel,
    session_scope: t.Callable[[], t.AsyncContextManager[AsyncSession]] | None = None,
) -> int:
    """
    Inserta las definiciones que falten. **Idempotente y no destructivo.**

    No pisa lo que ya está en la BD: el sentido de esta tabla es poder editar el cron en
    caliente, así que un seed que sobrescribiera revertiría en cada deploy los cambios
    que alguien hizo a propósito. Se insertan sólo los `job_id` que no existen.

    Returns:
        Cuántas filas se insertaron **de verdad**. Si otra réplica ganó la carrera, esas
        filas no se cuentan aquí.
    """
    if not jobs:
        return 0

    scope = session_scope or _default_session_scope

    async with scope() as session:
        existing = set(
            (
                await session.execute(
                    select(model.job_id).where(
                        model.job_id.in_([job.job_id for job in jobs])
                    )
                )
            )
            .scalars()
            .all()
        )

        missing = [job for job in jobs if job.job_id not in existing]
        if not missing:
            logger.debug("seed_cron_jobs: nada que insertar (%d ya existían)", len(existing))
            return 0

        statement = _insert_ignore(session, model)
        # Un modelo propio anterior a la columna `description` no la tiene, y un INSERT de
        # Core con una clave que no es columna revienta con `Unconsumed column names`. Se
        # siembra lo que la tabla admita en vez de tirar el arranque entero.
        columns = set(model.__table__.c.keys())
        inserted = 0
        # Fila a fila para que `rowcount` sea fiable en todos los dialectos: con
        # executemany varios drivers devuelven -1 y el contador mentiría. Son un puñado
        # de filas y esto corre una vez al arrancar.
        for job in missing:
            values = {
                "job_id": job.job_id,
                "task_name": job.task_name,
                "cron_expression": job.cron_expression,
                "payload": job.payload,
                "queue": job.queue,
                "is_active": job.is_active,
                "last_run_at": _as_utc(job.last_run_at) if job.last_run_at else None,
                "description": job.description,
            }
            result = await session.execute(
                statement,
                {key: value for key, value in values.items() if key in columns},
            )
            if result.rowcount != 0:
                inserted += 1
        await session.commit()

    logger.info("seed_cron_jobs: %d cronjobs insertados", inserted)
    return inserted


def _insert_ignore(session: AsyncSession, model: AnyCronJobModel) -> t.Any:
    """
    INSERT que ignora los conflictos de clave primaria.

    La comprobación previa de `seed_cron_jobs` cubre el caso normal; esto cubre la
    carrera entre dos réplicas sembrando a la vez en el mismo arranque. `ON CONFLICT`
    existe en PostgreSQL y SQLite; en el resto se cae al INSERT normal, donde la
    comprobación previa es la única defensa.
    """
    dialect = session.get_bind().dialect.name

    # Se construye sobre `__table__` (Core) y no sobre la entidad ORM: el INSERT de Core
    # devuelve un CursorResult con `rowcount` fiable, que es lo que necesitamos para
    # informar cuántas filas se crearon de verdad.
    table = model.__table__

    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        return pg_insert(table).on_conflict_do_nothing(index_elements=["job_id"])
    if dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        return sqlite_insert(table).on_conflict_do_nothing(index_elements=["job_id"])

    from sqlalchemy import insert

    return insert(table)


async def create_cron_tables(
    engine: AsyncEngine | None = None,
    *,
    model: AnyCronJobModel = CronJobModel,
) -> None:
    """
    Crea la tabla de cronjobs si no existe.

    Atajo para entornos sin migraciones (tests, desarrollo, un worker de un script). En
    producción con Alembic, la migración equivalente es::

        op.create_table(
            "hexcore_cron_jobs",
            sa.Column("job_id", sa.String(128), primary_key=True),
            sa.Column("task_name", sa.String(255), nullable=False),
            sa.Column("cron_expression", sa.String(128), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.Column("queue", sa.String(64), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("description", sa.String(512), nullable=True),
        )

    Si la tabla es anterior a la columna `description`, la migración de actualización es::

        op.add_column(
            "hexcore_cron_jobs",
            sa.Column("description", sa.String(512), nullable=True),
        )
    """
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import get_engine

    target = engine or get_engine()
    async with target.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all, tables=[model.__table__]
        )


def cron_job(
    task: t.Callable[..., t.Any],
    cron_expression: str,
    *,
    job_id: str | None = None,
    payload: dict[str, t.Any] | None = None,
    queue: str | None = None,
    is_active: bool = True,
    description: str | None = None,
) -> CronJobDefinition:
    """
    Construye un `CronJobDefinition` a partir de una función decorada con
    `@background_task`.

    El `task_name` se deriva de `__cqrs_task_name__` y la cola de `__cqrs_queue__`:
    escribir el nombre a mano es cómo se acaba con un cron que encola una tarea que ya
    se renombró, y el fallo aparece en el worker, no aquí.

    `description` no la lee el scheduler: viaja a la tabla para que el panel que muestra
    los crons pueda decir **qué hace** cada uno antes de que alguien lo desactive. Si no
    se pasa, se usa la primera línea del docstring de la tarea, que suele ser justo eso.

    Uso::

        seed = [
            cron_job(cerrar_caja, "0 3 * * *"),
            cron_job(purgar_logs, "0 4 * * 0", payload={"days": 30}),
            cron_job(facturar, "0 6 1 * *", description="Emite las facturas del mes."),
        ]

    Raises:
        ValueError: Si `task` no está decorada con `@background_task`.
    """
    task_name = getattr(task, "__cqrs_task_name__", None)
    if not task_name:
        raise ValueError(
            f"'{getattr(task, '__qualname__', task)}' no está decorada con "
            "@background_task, así que no tiene '__cqrs_task_name__' y el worker no "
            "podría resolverla. Decorala, o construí el CronJobDefinition a mano si "
            "sabés lo que hacés."
        )

    return CronJobDefinition(
        job_id=job_id or task_name,
        task_name=task_name,
        cron_expression=cron_expression,
        payload=payload or {},
        queue=queue or getattr(task, "__cqrs_queue__", "default"),
        is_active=is_active,
        description=description or _first_docstring_line(task),
    )


def _first_docstring_line(task: t.Callable[..., t.Any]) -> str | None:
    """
    La primera línea del docstring de la tarea, o `None`.

    Que el default salga del docstring y no quede vacío es lo que hace que la columna
    sirva sin trabajo extra: la descripción que el operador necesita casi siempre ya está
    escrita justo encima de la función.
    """
    doc = getattr(task, "__doc__", None)
    if not doc:
        return None

    for line in doc.strip().splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return None


def _default_session_scope() -> t.AsyncContextManager[AsyncSession]:
    from hexcore.infrastructure.uow.scopes import session_scope

    return session_scope()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
