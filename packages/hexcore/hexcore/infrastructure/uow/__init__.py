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

        def __init__(
            self,
            session: "AsyncSession",
            *,
            event_store: t.Any = None,
            publish_after_commit: bool = True,
        ) -> None:
            """
            Args:
                session: La sesión de SQLAlchemy.
                event_store: Opcional. Si está, los eventos se persisten en la **misma
                    transacción** que el cambio, antes del commit.
                publish_after_commit: Si el UoW publica al bus después de comitear.
                    **Ponelo en `False` cuando corra un `EventStoreRelay`**: el relay lee del
                    almacén y publica, así que con los dos activos cada evento sale dos veces
                    — en silencio, y con handlers no idempotentes eso hace daño real.
            """
            self.session = session
            super().__init__()
            self.event_bus = LazyConfig.get_config().event_bus
            self.event_store = event_store
            self._publish_after_commit = publish_after_commit
            self._entidades_del_commit: list[BaseEntity] = []
            self._eventos_por_entidad: dict[int, list[DomainEvent]] = {}
            # Registro explícito, independiente de session.new/dirty/deleted. Ver los
            # docstrings de collect_entity() y collect_domain_entities() para el porqué.
            self._entidades_registradas: list[BaseEntity] = []
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
            Persiste, confirma y despacha, **en ese orden**.

            El orden cambió en 9.0, y el anterior era un defecto silencioso: se comiteaba
            primero y recién después se llamaba a `collect_domain_events()`, que recorre
            `session.new | dirty | deleted`. Post-commit esas tres colecciones **están
            vacías** — SQLAlchemy las limpia al comitear —, así que el UoW no recogía ningún
            evento y no publicaba nada. Sin error y sin log: los eventos de dominio de toda
            aplicación que usara este UoW simplemente no llegaban.

            Recolectar antes lo arregla, y además es lo único que permite escribir el evento
            en la misma transacción que el cambio, que es la garantía que hace del event
            store algo más que un log paralelo que a veces coincide.

            Si el `commit()` falla, la excepción se propaga y los eventos se descartan. Es lo
            correcto: el cambio no ocurrió, así que el hecho tampoco.
            """
            pendientes = self.collect_domain_events()

            if self.event_store is not None and pendientes:
                await self._append_to_store(pendientes)

            await self.session.commit()

            try:
                if self._publish_after_commit:
                    await self._publish(pendientes)
            finally:
                self.clear_tracked_entities()

        async def _append_to_store(self, events: t.List[DomainEvent]) -> None:
            """
            Escribe los eventos en el almacén, dentro de la transacción en curso.

            **Es el modo "event log", no event sourcing.** Los eventos que salen del UoW los
            emitieron entidades clásicas, cuyo estado vive en sus propias tablas: no hay un
            agregado que haya leído una versión y quiera defenderla, así que se escribe con
            `EXPECTED_VERSION_ANY` y el stream sólo sirve para conservar el hecho y su orden.
            Reconstruir estado desde estos streams no funciona, y confundir los dos modos es
            donde esto se rompe.
            """
            from hexcore.domain.eventsourcing.stored import EXPECTED_VERSION_ANY

            for stream_id, del_stream in self._agrupar_por_stream(events).items():
                await self.event_store.append(
                    stream_id, del_stream, expected_version=EXPECTED_VERSION_ANY
                )

        def _agrupar_por_stream(
            self, events: t.List[DomainEvent]
        ) -> dict[str, list[DomainEvent]]:
            """
            `{stream_id: eventos}`, preservando el orden dentro de cada stream.

            El `stream_id` sale de la entidad que registró el evento. Uno que no se pueda
            atribuir a ninguna va a un stream por tipo de evento: perder la agrupación es
            mejor que perder el hecho.
            """
            por_stream: dict[str, list[DomainEvent]] = {}
            atribuidos: set[int] = set()

            for entidad in self._entidades_del_commit:
                tipo = type(entidad).__name__.lower()
                stream_id = f"{tipo}-{entidad.id}"
                for evento in self._eventos_por_entidad.get(id(entidad), ()):
                    por_stream.setdefault(stream_id, []).append(evento)
                    atribuidos.add(id(evento))

            for evento in events:
                if id(evento) in atribuidos:
                    continue
                tipo = type(evento).__name__.lower()
                por_stream.setdefault(f"evento-{tipo}", []).append(evento)

            return por_stream

        async def _publish(self, events: t.List[DomainEvent]) -> None:
            for event in events:
                await self.event_bus.publish(event)

        async def rollback(self) -> None:
            if self.session.in_transaction():
                await self.session.rollback()
            self.clear_tracked_entities()

        def collect_domain_entities(self) -> t.List[BaseEntity]:
            """
            Las entidades de dominio con eventos por recoger, **deduplicadas por identidad**.

            Junta dos fuentes:

            1. `self._entidades_registradas` — lo que llegó por `collect_entity()`.
               `SqlAlchemyRepository.save()` la llama explícitamente (vía el decorador
               `@register_entity_on_uow`) en el momento en que la entidad todavía tiene el
               link vivo en la pila, sin importar qué le pase después a la sesión.
            2. `session.new | dirty | deleted` — para quien haga `session.add()` a mano con
               `set_domain_entity()`, sin pasar por un repositorio.

            La (2) sola —que fue el único mecanismo hasta acá— alcanza sólo si la entidad
            sigue en esas tres colecciones **en el instante exacto del commit**, y dos cosas
            se lo comen sin avisar ni tirar excepción:

            - `save_entity()` (el `save()` genérico) hace `session.merge()` seguido de un
              `flush()` propio. El objeto que `db_save()` devuelve queda persistente y
              limpio —afuera de `new` y de `dirty`— apenas termina ese `flush()`, mucho antes
              de que el `commit()` del UoW llegue a mirar la sesión. Ni siquiera hace falta
              una segunda escritura: un solo `repo.save()` ya alcanza para perder el evento.
            - Cualquier segunda escritura en el mismo `uow` (una auditoría, otro
              `repo.save()`) puede disparar un flush de sesión completa que evict-ea a la
              primera de esas colecciones, aunque el link a la entidad de dominio esté hecho
              correctamente.

            Por eso (1) no es una comodidad, es lo que hace confiable a un `uow` con más de
            una escritura antes del commit.

            Devuelve una lista y no un `set`, aunque el nombre de la operación pida un
            conjunto: `BaseEntity` es un modelo de pydantic mutable, así que no define
            `__hash__` y **no se puede meter en un set** — `set.add()` levanta
            `TypeError: unhashable type`. Se deduplica por `id()` y no por igualdad porque dos
            entidades distintas con los mismos campos son iguales para pydantic, y drenarle
            los eventos a una sola perdería los de la otra.
            """
            entidades: t.List[BaseEntity] = []
            vistas: set[int] = set()

            for entity in self._entidades_registradas:
                if id(entity) in vistas:
                    continue
                vistas.add(id(entity))
                entidades.append(entity)

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
                    entity = modelo.get_domain_entity()
                    assert isinstance(entity, BaseEntity)
                    if id(entity) in vistas:
                        continue
                    vistas.add(id(entity))
                    entidades.append(entity)
            return entidades

        def collect_domain_events(self) -> t.List[DomainEvent]:
            """
            Drena los eventos de las entidades trackeadas.

            De paso recuerda de qué entidad vino cada uno: `_append_to_store` lo necesita para
            mandar cada evento a su propio stream, y después del drenado esa relación ya no se
            puede reconstruir porque las listas quedaron vacías.
            """
            events: t.List[DomainEvent] = []
            self._entidades_del_commit = []
            self._eventos_por_entidad = {}

            for entity in self.collect_domain_entities():
                propios = entity.pull_domain_events()
                if not propios:
                    continue
                self._entidades_del_commit.append(entity)
                self._eventos_por_entidad[id(entity)] = propios
                events.extend(propios)
            return events

        async def dispatch_events(self) -> None:
            """
            Recolecta y publica.

            Queda para quien la llame a mano; `commit()` ya no la usa, porque tiene que
            recolectar **antes** de comitear y publicar después.
            """
            await self._publish(self.collect_domain_events())

        def clear_tracked_entities(self) -> None:
            self._entidades_registradas.clear()

        def collect_entity(self, entity: BaseEntity) -> None:
            """
            Registra la entidad para que `collect_domain_entities()` la recoja pase lo que
            pase con el estado de la sesión. Ver el docstring de `collect_domain_entities()`
            para el porqué hace falta esto además del escaneo de la sesión.

            Registrarla dos veces no la duplica: `SqlAlchemyRepository.save()` pasa por acá
            en cada `save()`, así que una entidad guardada más de una vez en el mismo `uow`
            (por ejemplo, un `update` después de un `create`) no se acumula.
            """
            if any(trackeada is entity for trackeada in self._entidades_registradas):
                return
            self._entidades_registradas.append(entity)
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
    """
    Unit of Work para Beanie/MongoDB.

    **Sin replica set, MongoDB no da transacciones multi-documento**, así que la garantía que
    el UoW de SQLAlchemy sí ofrece —que el evento y el cambio entran o no entran juntos— acá
    no existe: son dos escrituras independientes, y un fallo entre ellas deja el cambio hecho
    sin su evento, o al revés. Con replica set configurado, el camino correcto es pasarle al
    store la sesión de la transacción de Mongo.

    Por eso, en Mongo el modo sano es Event Sourcing puro —el `append` *es* el cambio, y lo
    hace `EventSourcedRepository`— en vez de este híbrido.
    """

    def __init__(
        self,
        *,
        event_store: t.Any = None,
        publish_after_commit: bool = True,
    ) -> None:
        super().__init__()
        self.event_bus = LazyConfig.get_config().event_bus
        self.event_store = event_store
        self._publish_after_commit = publish_after_commit
        # Lista y no `set`: `BaseEntity` es un modelo de pydantic mutable, así que no es
        # hashable y `set.add()` levanta `TypeError`. Se deduplica por identidad en
        # `collect_entity`.
        self._entities: list[BaseEntity] = []
        self._entidades_del_commit: list[BaseEntity] = []
        self._eventos_por_entidad: dict[int, list[DomainEvent]] = {}
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
        """
        Persiste los eventos en el almacén y los publica.

        Mismo orden que el UoW de SQLAlchemy —primero al almacén, después al bus—, pero sin
        la atomicidad: ver el docstring de la clase.
        """
        pendientes = self.collect_domain_events()

        if self.event_store is not None and pendientes:
            await self._append_to_store(pendientes)

        try:
            if self._publish_after_commit:
                await self._publish(pendientes)
        finally:
            self.clear_tracked_entities()

    async def _append_to_store(self, events: t.List[DomainEvent]) -> None:
        """Modo event log, igual que en el UoW de SQLAlchemy."""
        from hexcore.domain.eventsourcing.stored import EXPECTED_VERSION_ANY

        for stream_id, del_stream in self._agrupar_por_stream(events).items():
            await self.event_store.append(
                stream_id, del_stream, expected_version=EXPECTED_VERSION_ANY
            )

    def _agrupar_por_stream(
        self, events: t.List[DomainEvent]
    ) -> dict[str, list[DomainEvent]]:
        por_stream: dict[str, list[DomainEvent]] = {}
        atribuidos: set[int] = set()

        for entidad in self._entidades_del_commit:
            tipo = type(entidad).__name__.lower()
            stream_id = f"{tipo}-{entidad.id}"
            for evento in self._eventos_por_entidad.get(id(entidad), ()):
                por_stream.setdefault(stream_id, []).append(evento)
                atribuidos.add(id(evento))

        for evento in events:
            if id(evento) in atribuidos:
                continue
            tipo = type(evento).__name__.lower()
            por_stream.setdefault(f"evento-{tipo}", []).append(evento)

        return por_stream

    async def _publish(self, events: t.List[DomainEvent]) -> None:
        for event in events:
            await self.event_bus.publish(event)

    async def rollback(self) -> None:
        for entity in self._entities:
            entity.clear_domain_events()
        self.clear_tracked_entities()

    def collect_entity(self, entity: BaseEntity) -> None:
        """Trackea la entidad. Registrarla dos veces no la duplica."""
        if any(trackeada is entity for trackeada in self._entities):
            return
        self._entities.append(entity)

    def collect_domain_entities(self) -> t.List[BaseEntity]:
        return list(self._entities)

    def collect_domain_events(self) -> t.List[DomainEvent]:
        """Drena los eventos, recordando de qué entidad vino cada uno."""
        events: t.List[DomainEvent] = []
        self._entidades_del_commit = []
        self._eventos_por_entidad = {}

        for entity in self.collect_domain_entities():
            propios = entity.pull_domain_events()
            if not propios:
                continue
            self._entidades_del_commit.append(entity)
            self._eventos_por_entidad[id(entity)] = propios
            events.extend(propios)
        return events

    async def dispatch_events(self) -> None:
        await self._publish(self.collect_domain_events())

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
