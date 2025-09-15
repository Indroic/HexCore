from __future__ import annotations
import typing as t
from itertools import chain
from sqlalchemy.ext.asyncio import AsyncSession
from hexcore.domain.uow import IUnitOfWork
from hexcore.domain.base import BaseEntity
from hexcore.domain.events import DomainEvent
from hexcore.infrastructure.repositories.orms.sqlalchemy import (
    BaseModel,
)


class SqlAlchemyUnitOfWork(IUnitOfWork):
    """
    Implementación concreta (Adaptador) de la Unidad de Trabajo para SQLAlchemy.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        super().__init__()

    async def __aexit__(
        self,
        exc_type: t.Optional[type],
        exc_val: t.Optional[BaseException],
        exc_tb: t.Optional[t.Any],
    ) -> None:
        # Llama al rollback de la interfaz (que a su vez llama al nuestro)
        # y cierra la sesión para liberar la conexión.
        await super().__aexit__(exc_type, exc_val, exc_tb)
        await self.session.close()

    async def commit(self):
        """
        Confirma la transacción y despacha los eventos.
        """
        await self.session.commit()
        await self.dispatch_events()
        self.clear_tracked_entities()

    async def rollback(self):
        await self.session.rollback()
        self.clear_tracked_entities()

    def collect_domain_entities(self) -> t.Set[BaseEntity]:
        """
        Recolecta todas las entidades de dominio rastreadas por la sesión de SQLAlchemy.
        """
        domain_entities: t.Set[BaseEntity] = set()
        all_tracked_models = chain(
            self.session.new, self.session.dirty, self.session.deleted
        )
        for model in all_tracked_models:
            if isinstance(model, BaseModel):
                entity: BaseEntity = model.get_domain_entity()  # type: ignore
                assert isinstance(entity, BaseEntity)
                domain_entities.add(entity)
        return domain_entities

    def collect_domain_events(self) -> t.List[DomainEvent]:
        events: t.List[DomainEvent] = []
        for entity in self.collect_domain_entities():
            events.extend(entity.pull_domain_events())
        return events

    async def dispatch_events(self):
        for event in self.collect_domain_events():
            await self.events_dispatcher.dispatch(event)

    def clear_tracked_entities(self):
        # No es necesario limpiar entidades en SQL, pero se mantiene para simetría
        pass

    def collect_entity(self, entity: BaseEntity) -> None:
        # No es necesario en SQLAlchemy, pero se define para compatibilidad
        pass


class NoSqlUnitOfWork(IUnitOfWork):
    def __init__(self):
        self._entities: set[BaseEntity] = set()

    async def __aenter__(self):
        return self

    async def __aexit__(
        self,
        exc_type: t.Optional[type],
        exc_val: t.Optional[BaseException],
        exc_tb: t.Optional[t.Any],
    ) -> None:
        if exc_type:
            await self.rollback()

    async def commit(self):
        await self.dispatch_events()
        self.clear_tracked_entities()

    async def rollback(self):
        for entity in self._entities:
            entity.clear_domain_events()
        self.clear_tracked_entities()

    def collect_entity(self, entity: BaseEntity):
        self._entities.add(entity)

    def collect_domain_entities(self) -> t.Set[BaseEntity]:
        return set(self._entities)

    def collect_domain_events(self) -> t.List[DomainEvent]:
        events: t.List[DomainEvent] = []
        for entity in self.collect_domain_entities():
            events.extend(entity.pull_domain_events())
        return events

    async def dispatch_events(self):
        for event in self.collect_domain_events():
            await self.events_dispatcher.dispatch(event)

    def clear_tracked_entities(self):
        self._entities.clear()
