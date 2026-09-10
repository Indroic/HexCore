"""
El contrato de `AbstractSnapshotStore` y `AbstractCheckpointStore`.

Igual que la suite del event store: se escribe una vez y cada adaptador se agrega a la
parametrizacion. Son puertos chicos, pero los dos tienen una regla que un adaptador ingenuo
rompe sin notarlo -- guardar historial en vez de pisar, y devolver 0 y no None para una
suscripcion nueva -- y esas reglas tienen consecuencias: la primera es la unica via de
recuperacion ante un snapshot corrupto, y la segunda es lo que hace que un proyector nuevo
arranque desde el principio en vez de saltarse el almacen entero.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime

import pytest

from hexcore.domain.eventsourcing import (
    AbstractCheckpointStore,
    AbstractSnapshotStore,
    Snapshot,
)
from hexcore.infrastructure.eventsourcing.memory import (
    InMemoryCheckpointStore,
    InMemorySnapshotStore,
)

AHORA = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

SNAPSHOT_STORES: dict[str, t.Callable[[], AbstractSnapshotStore]] = {
    "memoria": InMemorySnapshotStore,
}
CHECKPOINT_STORES: dict[str, t.Callable[[], AbstractCheckpointStore]] = {
    "memoria": InMemoryCheckpointStore,
}


@pytest.fixture(params=sorted(SNAPSHOT_STORES), ids=sorted(SNAPSHOT_STORES))
def snapshots(request: pytest.FixtureRequest) -> AbstractSnapshotStore:
    return SNAPSHOT_STORES[request.param]()


@pytest.fixture(params=sorted(CHECKPOINT_STORES), ids=sorted(CHECKPOINT_STORES))
def checkpoints(request: pytest.FixtureRequest) -> AbstractCheckpointStore:
    return CHECKPOINT_STORES[request.param]()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


pytestmark = pytest.mark.anyio


def _snapshot(version: int, *, stream_id: str = "pedido-1", **estado: t.Any) -> Snapshot:
    return Snapshot(
        stream_id=stream_id,
        aggregate_type="tests.Pedido",
        version=version,
        state=estado or {"total": version},
        taken_at=AHORA,
    )


class TestElAlmacenDeSnapshots:
    async def test_un_stream_sin_snapshots_da_none(self, snapshots: AbstractSnapshotStore):
        """None y no una excepcion: no tener snapshot es lo normal, no un error."""
        assert await snapshots.load("pedido-1") is None

    async def test_round_trip(self, snapshots: AbstractSnapshotStore):
        await snapshots.save(_snapshot(40, total=42))

        recuperado = await snapshots.load("pedido-1")

        assert recuperado is not None
        assert recuperado.version == 40
        assert recuperado.state == {"total": 42}
        assert recuperado.aggregate_type == "tests.Pedido"

    async def test_load_devuelve_el_mas_reciente(self, snapshots: AbstractSnapshotStore):
        await snapshots.save(_snapshot(10))
        await snapshots.save(_snapshot(40))
        await snapshots.save(_snapshot(25))

        recuperado = await snapshots.load("pedido-1")

        assert recuperado is not None and recuperado.version == 40

    async def test_guarda_historial_en_vez_de_pisar(self, snapshots: AbstractSnapshotStore):
        """
        La unica via de recuperacion ante un snapshot corrupto: borrar el ultimo y volver a
        uno anterior. Pisando, ese anterior no existiria.
        """
        await snapshots.save(_snapshot(10))
        await snapshots.save(_snapshot(40))

        anterior = await snapshots.load("pedido-1", max_version=20)

        assert anterior is not None and anterior.version == 10

    async def test_max_version_es_inclusivo(self, snapshots: AbstractSnapshotStore):
        await snapshots.save(_snapshot(10))
        await snapshots.save(_snapshot(20))

        recuperado = await snapshots.load("pedido-1", max_version=20)

        assert recuperado is not None and recuperado.version == 20

    async def test_max_version_por_debajo_de_todo_da_none(
        self, snapshots: AbstractSnapshotStore
    ):
        await snapshots.save(_snapshot(40))

        assert await snapshots.load("pedido-1", max_version=10) is None

    async def test_guardar_dos_veces_la_misma_version_es_idempotente(
        self, snapshots: AbstractSnapshotStore
    ):
        """Acumular duplicados haria crecer el historial sin aportar un punto de retroceso."""
        await snapshots.save(_snapshot(40, total=1))
        await snapshots.save(_snapshot(40, total=2))

        recuperado = await snapshots.load("pedido-1")

        assert recuperado is not None
        assert recuperado.state == {"total": 2}, "la segunda escritura tiene que ganar"
        assert await snapshots.load("pedido-1", max_version=39) is None

    async def test_delete_borra_todo_el_historial_del_stream(
        self, snapshots: AbstractSnapshotStore
    ):
        """
        Es la operacion de recuperacion: despues de esto el agregado se reconstruye desde el
        evento 1, que siempre es correcto porque el historial de eventos no se toco.
        """
        await snapshots.save(_snapshot(10))
        await snapshots.save(_snapshot(40))

        await snapshots.delete("pedido-1")

        assert await snapshots.load("pedido-1") is None

    async def test_delete_de_un_stream_sin_snapshots_no_falla(
        self, snapshots: AbstractSnapshotStore
    ):
        await snapshots.delete("no-existe")

    async def test_no_mezcla_streams(self, snapshots: AbstractSnapshotStore):
        await snapshots.save(_snapshot(40, stream_id="pedido-1"))
        await snapshots.save(_snapshot(10, stream_id="pedido-2"))

        recuperado = await snapshots.load("pedido-2")

        assert recuperado is not None and recuperado.version == 10


class TestElAlmacenDeCheckpoints:
    async def test_una_suscripcion_nueva_arranca_en_cero(
        self, checkpoints: AbstractCheckpointStore
    ):
        """
        0 y no None: `read_all(from_position=0)` lee desde el principio, asi que un proyector
        nuevo procesa el almacen entero. Devolver la ultima posicion se lo saltearia.
        """
        assert await checkpoints.load("nueva") == 0

    async def test_round_trip(self, checkpoints: AbstractCheckpointStore):
        await checkpoints.save("proyeccion", 42)

        assert await checkpoints.load("proyeccion") == 42

    async def test_guardar_de_nuevo_reemplaza(self, checkpoints: AbstractCheckpointStore):
        await checkpoints.save("proyeccion", 42)
        await checkpoints.save("proyeccion", 100)

        assert await checkpoints.load("proyeccion") == 100

    async def test_las_suscripciones_son_independientes(
        self, checkpoints: AbstractCheckpointStore
    ):
        """
        Es lo que permite que un relay y varias proyecciones avancen a ritmos distintos sobre
        el mismo almacen.
        """
        await checkpoints.save("relay", 100)
        await checkpoints.save("proyeccion", 20)

        assert await checkpoints.load("relay") == 100
        assert await checkpoints.load("proyeccion") == 20

    async def test_reset_vuelve_a_cero(self, checkpoints: AbstractCheckpointStore):
        """Lo que llama `rebuild()` antes de reprocesar desde el principio."""
        await checkpoints.save("proyeccion", 42)

        await checkpoints.reset("proyeccion")

        assert await checkpoints.load("proyeccion") == 0

    async def test_reset_de_una_suscripcion_inexistente_no_falla(
        self, checkpoints: AbstractCheckpointStore
    ):
        await checkpoints.reset("no-existe")
