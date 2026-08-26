from __future__ import annotations

import typing as t
from itertools import chain
from types import TracebackType

if t.TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession
    from hexcore.infrastructure.repositories.orms.sqlalchemy import BaseModel
else:
    # En runtime el import puede fallar y el módulo tiene que importarse igual: lo que sigue
    # se apoya en que los nombres **no existan** para saltear la clase de SQLAlchemy. Ver el
    # `except NameError` de más abajo.
    try:
        from sqlalchemy.ext.asyncio import AsyncSession
        from hexcore.infrastructure.repositories.orms.sqlalchemy import BaseModel
    except ImportError:
        pass

from hexcore.config import LazyConfig
from hexcore.domain.base import BaseEntity
from hexcore.domain.events import DomainEvent
from hexcore.domain.uow import IUnitOfWork
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


try:
    class SqlAlchemyUnitOfWork(IUnitOfWork):
        """
        Implementación concreta (Adaptador) de la Unidad de Trabajo para SQLAlchemy.
        """

        def __init__(self, session: "AsyncSession") -> None:
            self.session = session
            super().__init__()
            self.event_bus = LazyConfig.get_config().event_bus
            self._inject_repositories()

        def _inject_repositories(self) -> None:
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
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: TracebackType | None,
        ) -> None:
            # El ciclo de vida de la sesion se maneja en la dependencia/factory
            # que la crea. Evitamos rollback duplicado para no interferir con
            # el unwind del contexto externo de AsyncSession.
            if exc_type:
                self.clear_tracked_entities()

        async def commit(self) -> None:
            """
            Confirma la transacción y despacha los eventos.
            """
            await self.session.commit()
            try:
                await self.dispatch_events()
            finally:
                self.clear_tracked_entities()

        async def rollback(self) -> None:
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
                    # El `isinstance` no puede ligar el parámetro genérico —`BaseModel[T]` no
                    # es chequeable en runtime—, así que `get_domain_entity()` devolvería un
                    # `T` sin resolver. El `cast` dice lo que el `assert` de la línea siguiente
                    # comprueba de verdad: acá dentro, el modelo es de una entidad de dominio.
                    modelo = t.cast("BaseModel[BaseEntity]", model)
                    entity: BaseEntity = modelo.get_domain_entity()
                    assert isinstance(entity, BaseEntity)
                    domain_entities.add(entity)
            return domain_entities

        def collect_domain_events(self) -> t.List[DomainEvent]:
            events: t.List[DomainEvent] = []
            for entity in self.collect_domain_entities():
                events.extend(entity.pull_domain_events())
            return events

        async def dispatch_events(self) -> None:
            for event in self.collect_domain_events():
                await self.event_bus.publish(event)

        def clear_tracked_entities(self) -> None:
            # No es necesario limpiar entidades en SQL, pero se mantiene para simetría
            pass

        def collect_entity(self, entity: BaseEntity) -> None:
            # No es necesario en SQLAlchemy, pero se define para compatibilidad
            pass
except NameError:
    # `NameError` y no `ImportError`: el import de arriba ya falló y se tragó, así que lo que
    # falta acá es el **nombre**. Sin `[sql]`, `IUnitOfWork` se queda sin su implementación de
    # SQLAlchemy y este respaldo deja el símbolo definido para quien lo importe.
    #
    # El respaldo se le esconde al checker con `if not t.TYPE_CHECKING`. Sin eso, Pyright
    # analiza las dos ramas, se queda con la última —la vacía— y `SqlAlchemyUnitOfWork`
    # aparece sin `session`, sin `commit` y sin `collect_domain_events` para todo el que la
    # use. Es el mismo defecto que había en `implementations.py`, con la misma consecuencia.
    if not t.TYPE_CHECKING:

        class SqlAlchemyUnitOfWork: ...


class BeanieUnitOfWork(IUnitOfWork):
    def __init__(self) -> None:
        super().__init__()
        self.event_bus = LazyConfig.get_config().event_bus
        self._entities: set[BaseEntity] = set()
        self._inject_repositories()

    def _inject_repositories(self) -> None:
        """
        Instancia cada repositorio registrado y lo pega al UoW usando setattr.
        """
        repositories = discover_nosql_repositories()
        if not repositories:
            raise _build_discovery_runtime_error("Beanie")

        self.repositories = {}
        for name, repo_class in repositories.items():
            repo_instance = repo_class(self)
            setattr(self, name, repo_instance)
            self.repositories[name] = repo_instance

    async def __aenter__(self) -> BeanieUnitOfWork:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if exc_type:
            await self.rollback()

    async def commit(self) -> None:
        await self.dispatch_events()
        self.clear_tracked_entities()

    async def rollback(self) -> None:
        for entity in self._entities:
            entity.clear_domain_events()
        self.clear_tracked_entities()

    def collect_entity(self, entity: BaseEntity) -> None:
        self._entities.add(entity)

    def collect_domain_entities(self) -> t.Set[BaseEntity]:
        return set(self._entities)

    def collect_domain_events(self) -> t.List[DomainEvent]:
        events: t.List[DomainEvent] = []
        for entity in self.collect_domain_entities():
            events.extend(entity.pull_domain_events())
        return events

    async def dispatch_events(self) -> None:
        for event in self.collect_domain_events():
            await self.event_bus.publish(event)

    def clear_tracked_entities(self) -> None:
        self._entities.clear()




# Scopes para código fuera de FastAPI (workers, cron, scripts, seeds).
# Se importan al final: `scopes` sólo importa de este módulo de forma perezosa.
from .scopes import (  # noqa: E402
    nosql_uow_scope,
    open_uow_scope,
    session_scope,
    uow_scope,
)

__all__ = [
    "SqlAlchemyUnitOfWork",
    "BeanieUnitOfWork",
    "session_scope",
    "uow_scope",
    "open_uow_scope",
    "nosql_uow_scope",
]
