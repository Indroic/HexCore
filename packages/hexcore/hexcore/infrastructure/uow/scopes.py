"""
Scopes de sesión y Unit of Work para código que **no** es un request de FastAPI.

`hexcore.infrastructure.api.utils` sólo ofrece dependencias FastAPI (`get_session`,
`get_sql_uow`). Todo lo demás —workers, tasks de background, cron, scripts, seeds,
migraciones de datos— se queda sin nada y termina reescribiendo estos dos context
managers.

`session_scope` no es un lujo: construir un `SqlAlchemyUnitOfWork` corre el
auto-discovery e **instancia todos los repositorios de dominio**, un coste absurdo para
leer una tabla de infraestructura como `cron_jobs`.

Convención de `uow_scope`: **no** entra al UoW. Cede el UoW sin abrir la transacción,
para que el use case controle su propio ``async with uow:``, que es la convención de
los ejemplos de use case. Si querés el UoW ya abierto, usá `open_uow_scope`.
"""
from __future__ import annotations

import typing as t
from contextlib import asynccontextmanager

if t.TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from hexcore.domain.uow import IUnitOfWork

__all__ = ["session_scope", "uow_scope", "open_uow_scope", "nosql_uow_scope"]


@asynccontextmanager
async def session_scope() -> t.AsyncIterator["AsyncSession"]:
    """
    Cede una `AsyncSession` con su ciclo de vida gestionado.

    No abre transacción ni comitea: eso lo decide quien la usa. Sirve para leer o
    escribir tablas de infraestructura sin pagar el auto-discovery de repositorios.

    Uso::

        async with session_scope() as session:
            rows = (await session.execute(select(CronJobModel))).scalars().all()
    """
    from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
        get_session_factory,
    )

    factory = get_session_factory()
    async with factory() as session:
        yield session


@asynccontextmanager
async def uow_scope() -> t.AsyncIterator["IUnitOfWork"]:
    """
    Cede un `SqlAlchemyUnitOfWork` **sin entrar** en él.

    Es la convención de los ejemplos de use case: el use case hace su propio
    ``async with self.uow:`` y controla el commit. Entrar aquí y además allí anida
    contextos.

    Uso::

        async with uow_scope() as uow:
            await CerrarTicketUseCase(uow).execute(request)
    """
    from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork

    async with session_scope() as session:
        yield SqlAlchemyUnitOfWork(session=session)


@asynccontextmanager
async def open_uow_scope() -> t.AsyncIterator["IUnitOfWork"]:
    """
    Como `uow_scope`, pero **entra** al UoW (abre la transacción) y hace rollback si
    el bloque lanza.

    Para código que no delega en un use case y quiere el UoW listo para usar. No
    comitea: el commit sigue siendo explícito.
    """
    async with uow_scope() as uow:
        async with uow:
            yield uow


@asynccontextmanager
async def nosql_uow_scope() -> t.AsyncIterator["IUnitOfWork"]:
    """Equivalente de `open_uow_scope` para el UoW de Beanie/MongoDB."""
    from hexcore.infrastructure.uow import BeanieUnitOfWork

    uow: "IUnitOfWork" = BeanieUnitOfWork()
    async with uow:
        yield uow
