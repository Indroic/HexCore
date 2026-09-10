"""
El event store enganchado al Unit of Work, contra SQLite de verdad.

Los mocks de `test_uow_session_regression.py` fijan el **orden** de las llamadas. Aca se fija
lo que ese orden compra: que el evento y el cambio de negocio entren en la misma transaccion,
y que un rollback se lleve los dos. Eso no se puede probar con dobles -- hace falta una base
que confirme o descarte.
"""
from __future__ import annotations

import typing as t
from unittest.mock import patch

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

from sqlalchemy import String  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.orm import Mapped, mapped_column  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from hexcore.domain.base import BaseEntity  # noqa: E402
from hexcore.domain.cqrs.buses import AbstractEventBus  # noqa: E402
from hexcore.domain.events import DomainEvent  # noqa: E402
from hexcore.infrastructure.eventsourcing.sqlalchemy_models import (  # noqa: E402
    create_eventstore_tables,
)
from hexcore.infrastructure.eventsourcing.sqlalchemy_store import (  # noqa: E402
    SqlAlchemyEventStore,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base, BaseModel  # noqa: E402
from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork  # noqa: E402


class CosaCreada(DomainEvent):
    nombre: str = "x"


class CosaRenombrada(DomainEvent):
    nombre: str = "y"


class Cosa(BaseEntity):
    nombre: str = "x"


class CosaModel(BaseModel[Cosa]):
    __tablename__ = "cosas_de_prueba_es"

    nombre: Mapped[str] = mapped_column(String(50), default="x")


class BusQueGraba(AbstractEventBus):
    def __init__(self) -> None:
        self.publicados: list[DomainEvent] = []

    def subscribe(
        self,
        event_type: type[DomainEvent],
        handler: t.Callable[[DomainEvent], t.Awaitable[None]],
    ) -> None:
        return None

    async def publish(self, event: DomainEvent) -> None:
        self.publicados.append(event)


class _ConfigConBus:
    def __init__(self, bus: AbstractEventBus) -> None:
        self.event_bus = bus


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def motor() -> t.AsyncIterator[t.Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conexion:
        await conexion.run_sync(Base.metadata.create_all, tables=[CosaModel.__table__])
    await create_eventstore_tables(engine)
    yield engine
    await engine.dispose()


@pytest.fixture
def bus() -> BusQueGraba:
    return BusQueGraba()


def armar_uow(
    session: t.Any, bus: AbstractEventBus, **opciones: t.Any
) -> SqlAlchemyUnitOfWork:
    """El UoW real, con el discovery de repositorios anulado."""
    with (
        patch(
            "hexcore.infrastructure.uow.LazyConfig.get_config",
            return_value=_ConfigConBus(bus),
        ),
        patch(
            "hexcore.infrastructure.uow.discover_sql_repositories",
            return_value={"dummy": lambda uow: object()},
        ),
    ):
        return SqlAlchemyUnitOfWork(session=session, **opciones)


async def guardar_cosa(session: t.Any, *eventos: DomainEvent) -> Cosa:
    entidad = Cosa()
    for evento in eventos:
        entidad.register_event(evento)
    fila = CosaModel(id=entidad.id, nombre=entidad.nombre)
    fila.set_domain_entity(entidad)
    session.add(fila)
    return entidad


pytestmark = pytest.mark.anyio


class TestSinEventStore:
    async def test_los_eventos_se_publican(self, motor: t.Any, bus: BusQueGraba):
        """
        La regresion mas importante de 9.0.

        Antes `commit()` comiteaba y recien despues recolectaba, cuando `session.new` ya
        estaba vacia: **no se publicaba ningun evento**, sin error y sin log.
        """
        fabrica = async_sessionmaker(motor, expire_on_commit=False)

        async with fabrica() as session:
            uow = armar_uow(session, bus)
            await guardar_cosa(session, CosaCreada())
            await uow.commit()

        assert len(bus.publicados) == 1, (
            "el UoW no publico el evento: es el defecto que se arregla recolectando antes "
            "de comitear"
        )

    async def test_el_cambio_se_persiste(self, motor: t.Any, bus: BusQueGraba):
        fabrica = async_sessionmaker(motor, expire_on_commit=False)

        async with fabrica() as session:
            uow = armar_uow(session, bus)
            entidad = await guardar_cosa(session, CosaCreada())
            await uow.commit()

        async with fabrica() as session:
            assert await session.get(CosaModel, entidad.id) is not None


class TestConEventStore:
    async def test_el_evento_queda_en_el_almacen(self, motor: t.Any, bus: BusQueGraba):
        fabrica = async_sessionmaker(motor, expire_on_commit=False)

        async with fabrica() as session:
            store = SqlAlchemyEventStore(session=session)
            uow = armar_uow(session, bus, event_store=store)
            await guardar_cosa(session, CosaCreada())
            await uow.commit()

        lector = SqlAlchemyEventStore(session_scope=fabrica)
        assert len(await lector.read_all()) == 1

    async def test_el_evento_y_el_cambio_entran_en_la_misma_transaccion(
        self, motor: t.Any, bus: BusQueGraba
    ):
        """
        La garantia central del enganche. Si el evento se escribiera en otra transaccion,
        habria una ventana en la que el cambio existe y el hecho no -- o al reves.
        """
        fabrica = async_sessionmaker(motor, expire_on_commit=False)

        async with fabrica() as session:
            store = SqlAlchemyEventStore(session=session)
            uow = armar_uow(session, bus, event_store=store)
            entidad = await guardar_cosa(session, CosaCreada())

            # Antes del commit no hay nada visible desde fuera, ni el cambio ni el evento.
            lector = SqlAlchemyEventStore(session_scope=fabrica)
            async with fabrica() as otra:
                assert await otra.get(CosaModel, entidad.id) is None

            await uow.commit()

        assert len(await lector.read_all()) == 1
        async with fabrica() as otra:
            assert await otra.get(CosaModel, entidad.id) is not None

    async def test_un_rollback_no_deja_ni_el_cambio_ni_el_evento(
        self, motor: t.Any, bus: BusQueGraba
    ):
        """La otra mitad de la atomicidad: si no entra el cambio, tampoco el hecho."""
        fabrica = async_sessionmaker(motor, expire_on_commit=False)

        async with fabrica() as session:
            store = SqlAlchemyEventStore(session=session)
            armar_uow(session, bus, event_store=store)
            entidad = await guardar_cosa(session, CosaCreada())
            await store.append(
                f"cosa-{entidad.id}", [CosaCreada()], expected_version=-1
            )
            await session.rollback()

        lector = SqlAlchemyEventStore(session_scope=fabrica)
        assert await lector.read_all() == []
        async with fabrica() as otra:
            assert await otra.get(CosaModel, entidad.id) is None

    async def test_cada_entidad_va_a_su_propio_stream(
        self, motor: t.Any, bus: BusQueGraba
    ):
        fabrica = async_sessionmaker(motor, expire_on_commit=False)

        async with fabrica() as session:
            store = SqlAlchemyEventStore(session=session)
            uow = armar_uow(session, bus, event_store=store)
            una = await guardar_cosa(session, CosaCreada())
            otra = await guardar_cosa(session, CosaCreada())
            await uow.commit()

        lector = SqlAlchemyEventStore(session_scope=fabrica)
        assert len(await lector.read_stream(f"cosa-{una.id}")) == 1
        assert len(await lector.read_stream(f"cosa-{otra.id}")) == 1

    async def test_los_eventos_de_una_entidad_conservan_su_orden(
        self, motor: t.Any, bus: BusQueGraba
    ):
        fabrica = async_sessionmaker(motor, expire_on_commit=False)

        async with fabrica() as session:
            store = SqlAlchemyEventStore(session=session)
            uow = armar_uow(session, bus, event_store=store)
            entidad = await guardar_cosa(
                session, CosaCreada(), CosaRenombrada(nombre="z")
            )
            await uow.commit()

        lector = SqlAlchemyEventStore(session_scope=fabrica)
        leidos = await lector.read_stream(f"cosa-{entidad.id}")

        assert [p.version for p in leidos] == [1, 2]
        assert leidos[0].event_type.endswith("CosaCreada")
        assert leidos[1].event_type.endswith("CosaRenombrada")

    async def test_se_publica_ademas_de_persistir(self, motor: t.Any, bus: BusQueGraba):
        fabrica = async_sessionmaker(motor, expire_on_commit=False)

        async with fabrica() as session:
            store = SqlAlchemyEventStore(session=session)
            uow = armar_uow(session, bus, event_store=store)
            await guardar_cosa(session, CosaCreada())
            await uow.commit()

        assert len(bus.publicados) == 1


class TestLaDoblePublicacion:
    async def test_publish_after_commit_false_persiste_pero_no_publica(
        self, motor: t.Any, bus: BusQueGraba
    ):
        """
        Lo que hay que poner cuando corre un relay: el relay lee del almacen y publica, asi
        que con los dos activos cada evento saldria dos veces, en silencio.
        """
        fabrica = async_sessionmaker(motor, expire_on_commit=False)

        async with fabrica() as session:
            store = SqlAlchemyEventStore(session=session)
            uow = armar_uow(
                session, bus, event_store=store, publish_after_commit=False
            )
            await guardar_cosa(session, CosaCreada())
            await uow.commit()

        lector = SqlAlchemyEventStore(session_scope=fabrica)
        assert len(await lector.read_all()) == 1
        assert bus.publicados == []
