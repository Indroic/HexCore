"""
`EventSourcedRepository`: carga, guardado, snapshots y el reintento tras un conflicto.

El caso mas importante de este archivo es el ultimo de `TestSave`: tras un `ConcurrencyError`
el agregado tiene que quedar **intacto**. Si `save()` marcara los eventos como confirmados
antes de saber si el append salio bien, un conflicto dejaria el agregado con la version
avanzada y sin sus pendientes -- o sea, con la operacion de negocio perdida y sin forma de
reintentarla, porque ya no quedaria nada que reintentar.
"""
from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from hexcore.application.eventsourcing import EventSourcedRepository
from hexcore.domain.events import DomainEvent
from hexcore.domain.eventsourcing import (
    AggregateNotFoundError,
    AggregateRoot,
    ConcurrencyError,
    Snapshot,
    StoredEvent,
    when,
)
from hexcore.infrastructure.eventsourcing.memory import (
    InMemoryEventStore,
    InMemorySnapshotStore,
)

AHORA = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class PedidoCreado(DomainEvent):
    cliente: str = "acme"


class PedidoPagado(DomainEvent):
    monto: int = 0


class Pedido(AggregateRoot):
    cliente: str
    total: int

    @when(PedidoCreado)
    def _crear(self, evento: PedidoCreado) -> None:
        self.cliente = evento.cliente
        self.total = 0

    @when(PedidoPagado)
    def _pagar(self, evento: PedidoPagado) -> None:
        self.total += evento.monto

    @classmethod
    def crear(cls, cliente: str = "acme") -> "Pedido":
        pedido = cls.model_construct()
        pedido.id = uuid4()
        pedido.raise_event(PedidoCreado(cliente=cliente))
        return pedido

    def pagar(self, monto: int) -> None:
        self.raise_event(PedidoPagado(monto=monto))


@pytest.fixture
def store() -> InMemoryEventStore:
    return InMemoryEventStore()


@pytest.fixture
def snapshots() -> InMemorySnapshotStore:
    return InMemorySnapshotStore()


@pytest.fixture
def repo(store: InMemoryEventStore) -> EventSourcedRepository[Pedido]:
    return EventSourcedRepository(Pedido, store=store)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


pytestmark = pytest.mark.anyio


class TestLaConstruccion:
    def test_snapshot_every_sin_snapshot_store_falla(self, store: InMemoryEventStore):
        """
        Configurarlo asi no toma ningun snapshot y no avisa: cada get() releeria el stream
        entero, que es justo lo que se quiso evitar al ponerlo.
        """
        with pytest.raises(ValueError, match="snapshot store"):
            EventSourcedRepository(Pedido, store=store, snapshot_every=10)

    def test_snapshot_every_negativo_falla(self, store: InMemoryEventStore):
        with pytest.raises(ValueError):
            EventSourcedRepository(Pedido, store=store, snapshot_every=-1)


class TestSave:
    async def test_persiste_los_pendientes(
        self, repo: EventSourcedRepository[Pedido], store: InMemoryEventStore
    ):
        pedido = Pedido.crear()
        pedido.pagar(30)

        escritos = await repo.save(pedido)

        assert len(escritos) == 2
        assert await store.stream_version(pedido.stream_id) == 2

    async def test_marca_los_eventos_como_confirmados(
        self, repo: EventSourcedRepository[Pedido]
    ):
        pedido = Pedido.crear()

        await repo.save(pedido)

        assert pedido.uncommitted_events == ()
        assert pedido.version == 1

    async def test_un_agregado_sin_cambios_es_no_op(
        self, repo: EventSourcedRepository[Pedido], store: InMemoryEventStore
    ):
        pedido = Pedido.crear()
        await repo.save(pedido)

        assert await repo.save(pedido) == []
        assert len(store) == 1

    async def test_usa_el_stream_type_del_agregado(
        self, repo: EventSourcedRepository[Pedido], store: InMemoryEventStore
    ):
        pedido = Pedido.crear()

        escritos = await repo.save(pedido)

        assert escritos[0].stream_type == "pedido"

    async def test_guardar_dos_veces_encadena_las_versiones(
        self, repo: EventSourcedRepository[Pedido]
    ):
        pedido = Pedido.crear()
        await repo.save(pedido)

        pedido.pagar(30)
        escritos = await repo.save(pedido)

        assert escritos[0].version == 2
        assert pedido.version == 2

    async def test_un_conflicto_deja_el_agregado_intacto_y_reintentable(
        self, repo: EventSourcedRepository[Pedido], store: InMemoryEventStore
    ):
        """
        El invariante central. Si `save()` marcara antes de saber si el append salio bien, un
        conflicto dejaria el agregado con la version avanzada y sin pendientes: la operacion
        de negocio perdida, y sin nada que reintentar.
        """
        pedido = Pedido.crear()
        await repo.save(pedido)

        # Otro escritor avanza el stream por detras.
        await store.append(
            pedido.stream_id, [PedidoPagado(monto=1)], expected_version=1
        )

        pedido.pagar(30)
        pendientes_antes = pedido.uncommitted_events
        version_antes = pedido.version

        with pytest.raises(ConcurrencyError):
            await repo.save(pedido)

        assert pedido.uncommitted_events == pendientes_antes
        assert pedido.version == version_antes

    async def test_tras_un_conflicto_se_puede_recargar_y_reintentar(
        self, repo: EventSourcedRepository[Pedido], store: InMemoryEventStore
    ):
        """El ciclo completo que el invariante de arriba hace posible."""
        pedido = Pedido.crear()
        await repo.save(pedido)
        await store.append(
            pedido.stream_id, [PedidoPagado(monto=1)], expected_version=1
        )

        pedido.pagar(30)
        with pytest.raises(ConcurrencyError):
            await repo.save(pedido)

        recargado = await repo.get(pedido.id)
        recargado.pagar(30)
        await repo.save(recargado)

        assert recargado.total == 31
        assert await store.stream_version(pedido.stream_id) == 3


class TestGet:
    async def test_reconstruye_el_agregado(
        self, repo: EventSourcedRepository[Pedido]
    ):
        pedido = Pedido.crear(cliente="globex")
        pedido.pagar(30)
        pedido.pagar(12)
        await repo.save(pedido)

        recuperado = await repo.get(pedido.id)

        assert recuperado.id == pedido.id
        assert recuperado.cliente == "globex"
        assert recuperado.total == 42
        assert recuperado.version == 3

    async def test_el_recuperado_no_tiene_pendientes(
        self, repo: EventSourcedRepository[Pedido]
    ):
        """Lo que se acaba de leer ya esta escrito: reescribirlo duplicaria el historial."""
        pedido = Pedido.crear()
        await repo.save(pedido)

        assert (await repo.get(pedido.id)).uncommitted_events == ()

    async def test_el_stream_id_del_recuperado_es_el_mismo(
        self, repo: EventSourcedRepository[Pedido]
    ):
        """Sin el aggregate_id, el default_factory le pondria un id nuevo en cada carga."""
        pedido = Pedido.crear()
        await repo.save(pedido)

        assert (await repo.get(pedido.id)).stream_id == pedido.stream_id

    async def test_un_agregado_inexistente_lanza(
        self, repo: EventSourcedRepository[Pedido]
    ):
        identificador = uuid4()

        with pytest.raises(AggregateNotFoundError) as excinfo:
            await repo.get(identificador)

        assert str(identificador) in str(excinfo.value)

    async def test_acepta_el_id_como_string(self, repo: EventSourcedRepository[Pedido]):
        pedido = Pedido.crear()
        await repo.save(pedido)

        assert (await repo.get(str(pedido.id))).id == pedido.id

    async def test_find_devuelve_none_en_vez_de_lanzar(
        self, repo: EventSourcedRepository[Pedido]
    ):
        assert await repo.find(uuid4()) is None

    async def test_exists_no_reconstruye(
        self, repo: EventSourcedRepository[Pedido]
    ):
        pedido = Pedido.crear()
        await repo.save(pedido)

        assert await repo.exists(pedido.id) is True
        assert await repo.exists(uuid4()) is False


class TestConSnapshots:
    @pytest.fixture
    def repo_con_snapshots(
        self, store: InMemoryEventStore, snapshots: InMemorySnapshotStore
    ) -> EventSourcedRepository[Pedido]:
        return EventSourcedRepository(
            Pedido, store=store, snapshots=snapshots, snapshot_every=2
        )

    async def test_toma_snapshot_en_el_multiplo(
        self,
        repo_con_snapshots: EventSourcedRepository[Pedido],
        snapshots: InMemorySnapshotStore,
    ):
        pedido = Pedido.crear()
        pedido.pagar(30)  # version 2

        await repo_con_snapshots.save(pedido)

        snapshot = await snapshots.load(pedido.stream_id)
        assert snapshot is not None and snapshot.version == 2

    async def test_no_toma_snapshot_fuera_del_multiplo(
        self,
        repo_con_snapshots: EventSourcedRepository[Pedido],
        snapshots: InMemorySnapshotStore,
    ):
        pedido = Pedido.crear()  # version 1

        await repo_con_snapshots.save(pedido)

        assert await snapshots.load(pedido.stream_id) is None

    async def test_get_lee_solo_la_cola_posterior_al_snapshot(
        self,
        repo_con_snapshots: EventSourcedRepository[Pedido],
        store: InMemoryEventStore,
    ):
        """El punto entero del snapshot: no releer lo que ya esta resumido."""
        pedido = Pedido.crear()
        pedido.pagar(30)
        await repo_con_snapshots.save(pedido)  # snapshot en la 2
        pedido.pagar(12)
        await repo_con_snapshots.save(pedido)

        leidos: list[int] = []
        original = store.read_stream

        async def espiar(
            stream_id: str,
            *,
            from_version: int = 1,
            to_version: int | None = None,
            limit: int | None = None,
        ) -> list[StoredEvent]:
            leidos.append(from_version)
            return await original(
                stream_id, from_version=from_version, to_version=to_version, limit=limit
            )

        store.read_stream = espiar  # pyright: ignore[reportAttributeAccessIssue]
        recuperado = await repo_con_snapshots.get(pedido.id)

        assert leidos == [3], f"leyo desde {leidos}, tenia que arrancar despues del snapshot"
        assert recuperado.total == 42
        assert recuperado.version == 3

    async def test_el_estado_es_el_mismo_con_y_sin_snapshot(
        self,
        repo_con_snapshots: EventSourcedRepository[Pedido],
        store: InMemoryEventStore,
    ):
        """Lo que hace del snapshot una optimizacion y no otra fuente de verdad."""
        pedido = Pedido.crear()
        pedido.pagar(30)
        await repo_con_snapshots.save(pedido)
        pedido.pagar(12)
        await repo_con_snapshots.save(pedido)

        con_snapshot = await repo_con_snapshots.get(pedido.id)
        sin_snapshot = await EventSourcedRepository(Pedido, store=store).get(pedido.id)

        assert con_snapshot.model_dump() == sin_snapshot.model_dump()
        assert con_snapshot.version == sin_snapshot.version

    async def test_un_snapshot_de_otro_tipo_se_ignora(
        self,
        repo_con_snapshots: EventSourcedRepository[Pedido],
        snapshots: InMemorySnapshotStore,
    ):
        """
        El stream_id se reuso para otro agregado. Restaurar igual daria un objeto que pydantic
        acepta y que el negocio no entiende, asi que se relee el stream: mas lento y correcto.
        """
        pedido = Pedido.crear()
        pedido.pagar(30)
        await repo_con_snapshots.save(pedido)
        await snapshots.save(
            Snapshot(
                stream_id=pedido.stream_id,
                aggregate_type="otra.app.Usuario",
                version=2,
                state={"nombre": "quien sea"},
                taken_at=AHORA,
            )
        )

        recuperado = await repo_con_snapshots.get(pedido.id)

        assert recuperado.total == 30

    async def test_un_fallo_al_snapshotear_no_rompe_el_save(
        self, store: InMemoryEventStore, snapshots: InMemorySnapshotStore
    ):
        """
        Los eventos ya estan escritos. Dejar que el fallo suba convertiria un save() exitoso
        en uno fallido, y el llamador reintentaria una operacion que ya ocurrio.
        """

        async def explotar(snapshot: Snapshot) -> None:
            raise RuntimeError("el disco de snapshots se lleno")

        snapshots.save = explotar  # pyright: ignore[reportAttributeAccessIssue]
        repo = EventSourcedRepository(
            Pedido, store=store, snapshots=snapshots, snapshot_every=1
        )
        pedido = Pedido.crear()

        escritos = await repo.save(pedido)

        assert len(escritos) == 1
        assert pedido.version == 1
        assert await store.stream_version(pedido.stream_id) == 1


class TestLaHerencia:
    def test_no_hereda_del_repositorio_de_sqlalchemy(self):
        """
        Si heredara, discover_sql_repositories() lo recogeria y _inject_repositories() lo
        pegaria con setattr en todos los UoW del consumidor, sin que nadie lo pida.
        """
        sqlalchemy = pytest.importorskip("sqlalchemy")  # noqa: F841
        from hexcore.infrastructure.repositories.base import BaseSQLAlchemyRepository

        assert not issubclass(EventSourcedRepository, BaseSQLAlchemyRepository)

    def test_no_hereda_de_ibase_repository(self):
        """
        IBaseRepository esta atado a BaseEntity y su superficie es CRUD: list_all() sobre un
        event store seria recorrer todos los streams, y delete() seria reescribir el pasado.
        """
        from hexcore.domain.repositories import IBaseRepository

        assert not issubclass(EventSourcedRepository, IBaseRepository)
