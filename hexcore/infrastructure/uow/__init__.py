from __future__ import annotations
import typing as t
from itertools import chain
from sqlalchemy.ext.asyncio import AsyncSession
from hexcore.config import LazyConfig
from hexcore.domain.uow import IUnitOfWork
from hexcore.domain.base import BaseEntity
from hexcore.domain.events import DomainEvent
from hexcore.infrastructure.repositories.orms.sqlalchemy import (
    BaseModel,
)
from hexcore.infrastructure.repositories.utils import (
    discover_sql_repositories,
    discover_nosql_repositories,
)


def _build_discovery_runtime_error(backend_label: str) -> RuntimeError:
    config = LazyConfig.get_config()
    configured_paths = sorted(config.repository_discovery_paths)
    configured_paths_text = (
        ", ".join(configured_paths) if configured_paths else "ninguno"
    )
    return RuntimeError(
        f"No se descubrieron repositorios {backend_label}. "
        "HexCore v2 no usa fallback implicito: configura 'repository_discovery_paths' "
        "en tu config.py de raiz o por HEXCORE_CONFIG_MODULE(S). "
        f"Paths configurados: {configured_paths_text}."
    )


class SqlAlchemyUnitOfWork(IUnitOfWork):
    """
    Implementación concreta (Adaptador) de la Unidad de Trabajo para SQLAlchemy.
    """

    def __init__(self, session: AsyncSession):
        self.session = session
        super().__init__()
        self.events_dispatcher = LazyConfig.get_config().event_dispatcher
        self._inject_repositories()

    def _inject_repositories(self):
        """
        Instancia cada repositorio registrado y lo pega al UoW usando setattr.
        """
        repositories = discover_sql_repositories()
        if not repositories:
            raise _build_discovery_runtime_error("SQLAlchemy")

        self.repositories = {}
        for name, repo_class in repositories.items():
            repo_instance = repo_class(self)
            setattr(self, name, repo_instance)
            self.repositories[name] = repo_instance

    async def __aexit__(
        self,
        exc_type: t.Optional[type],
        exc_val: t.Optional[BaseException],
        exc_tb: t.Optional[t.Any],
    ) -> None:
        # El ciclo de vida de la sesion se maneja en la dependencia/factory
        # que la crea. Evitamos rollback duplicado para no interferir con
        # el unwind del contexto externo de AsyncSession.
        if exc_type:
            self.clear_tracked_entities()

    async def commit(self):
        """
        Confirma la transacción y despacha los eventos.
        """
        await self.session.commit()
        try:
            await self.dispatch_events()
        finally:
            self.clear_tracked_entities()

    async def rollback(self):
        if self.session.in_transaction():
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
        super().__init__()
        self.events_dispatcher = LazyConfig.get_config().event_dispatcher
        self._entities: set[BaseEntity] = set()
        self._inject_repositories()

    def _inject_repositories(self):
        """
        Instancia cada repositorio registrado y lo pega al UoW usando setattr.
        """
        repositories = discover_nosql_repositories()
        if not repositories:
            raise _build_discovery_runtime_error("NoSQL")

        self.repositories = {}
        for name, repo_class in repositories.items():
            repo_instance = repo_class(self)
            setattr(self, name, repo_instance)
            self.repositories[name] = repo_instance

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
