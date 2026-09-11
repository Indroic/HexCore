"""
`EventStoreRelay`: idempotencia, avance del checkpoint y el caso de la caida.

Lo que se prueba aca es la mitad de salida del patron outbox. La mitad de entrada -- que el
evento se escriba en la misma transaccion que el cambio -- la cubre
`test_eventsourcing_uow_integration.py`.

El caso que mas importa es `test_una_caida_entre_publicar_y_guardar_republica`: es la
justificacion de que la entrega sea at-least-once y de que los handlers tengan que ser
idempotentes. Guardar el checkpoint antes de publicar seria peor -- perderia eventos para
siempre --, asi que republicar es la falla que se elige tener.
"""
from __future__ import annotations

import asyncio
import typing as t

import pytest

from hexcore.application.eventsourcing import EventStoreRelay
from hexcore.domain.cqrs.buses import AbstractEventBus
from hexcore.domain.cqrs.exceptions import DeserializationError
from hexcore.domain.events import DomainEvent
from hexcore.domain.eventsourcing import EXPECTED_VERSION_ANY
from hexcore.infrastructure.eventsourcing.memory import (
    InMemoryCheckpointStore,
    InMemoryEventStore,
)


class PedidoCreado(DomainEvent):
    cliente: str = "acme"


class PedidoPagado(DomainEvent):
    monto: int = 0


class BusQueGraba(AbstractEventBus):
    """Un bus que anota lo que recibe, y opcionalmente falla en el n-esimo evento."""

    def __init__(self, falla_en: int | None = None) -> None:
        self.publicados: list[DomainEvent] = []
        self.intentos = 0
        #: El numero de **intento** que falla, no el de publicacion exitosa. Contar exitos
        #: haria que tras un skip el siguiente evento volviera a fallar, y eso probaria algo
        #: distinto de lo que dice el nombre del test.
        self.falla_en = falla_en

    def subscribe(
        self,
        event_type: type[DomainEvent],
        handler: t.Callable[[DomainEvent], t.Awaitable[None]],
    ) -> None:
        return None

    async def publish(self, event: DomainEvent) -> None:
        self.intentos += 1
        if self.falla_en is not None and self.intentos == self.falla_en:
            raise RuntimeError("el broker no responde")
        self.publicados.append(event)


@pytest.fixture
def store() -> InMemoryEventStore:
    return InMemoryEventStore()


@pytest.fixture
def checkpoints() -> InMemoryCheckpointStore:
    return InMemoryCheckpointStore()


@pytest.fixture
def bus() -> BusQueGraba:
    return BusQueGraba()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


pytestmark = pytest.mark.anyio


async def sembrar(store: InMemoryEventStore, cuantos: int = 3) -> None:
    await store.append(
        "pedido-1", [PedidoCreado()], expected_version=EXPECTED_VERSION_ANY
    )
    for indice in range(cuantos - 1):
        await store.append(
            "pedido-1",
            [PedidoPagado(monto=indice + 1)],
            expected_version=EXPECTED_VERSION_ANY,
        )


def armar(
    store: InMemoryEventStore,
    bus: AbstractEventBus,
    checkpoints: InMemoryCheckpointStore,
    **opciones: t.Any,
) -> EventStoreRelay:
    return EventStoreRelay(
        store=store, bus=bus, checkpoints=checkpoints, **opciones
    )


class TestRunOnce:
    async def test_publica_lo_que_hay(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        await sembrar(store, 3)

        publicados = await armar(store, bus, checkpoints).run_once()

        assert publicados == 3
        assert len(bus.publicados) == 3

    async def test_avanza_el_checkpoint(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        await sembrar(store, 3)

        await armar(store, bus, checkpoints).run_once()

        assert await checkpoints.load("relay") == 3

    async def test_un_segundo_run_no_republica(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        """La idempotencia del camino feliz: el checkpoint marca lo ya entregado."""
        await sembrar(store, 3)
        relay = armar(store, bus, checkpoints)

        await relay.run_once()
        segundos = await relay.run_once()

        assert segundos == 0
        assert len(bus.publicados) == 3

    async def test_un_almacen_vacio_devuelve_cero(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        """Es la senal con la que run_forever() decide dormir."""
        assert await armar(store, bus, checkpoints).run_once() == 0

    async def test_respeta_el_tamano_del_lote(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        await sembrar(store, 5)

        publicados = await armar(store, bus, checkpoints, batch_size=2).run_once()

        assert publicados == 2

    async def test_publica_en_orden_global(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        await store.append(
            "pedido-1", [PedidoPagado(monto=1)], expected_version=EXPECTED_VERSION_ANY
        )
        await store.append(
            "pedido-2", [PedidoPagado(monto=2)], expected_version=EXPECTED_VERSION_ANY
        )
        await store.append(
            "pedido-1", [PedidoPagado(monto=3)], expected_version=EXPECTED_VERSION_ANY
        )

        await armar(store, bus, checkpoints).run_once()

        montos = [e.monto for e in bus.publicados if isinstance(e, PedidoPagado)]
        assert montos == [1, 2, 3]

    async def test_filtra_por_categoria(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        await sembrar(store, 2)
        await store.append(
            "usuario-1", [PedidoCreado()], expected_version=EXPECTED_VERSION_ANY
        )

        publicados = await armar(
            store, bus, checkpoints, stream_types=["usuario"]
        ).run_once()

        assert publicados == 1


class TestElSobre:
    async def test_republica_con_el_contexto_original(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        """
        La razon de que StoredEvent.payload guarde el sobre entero: el actor y el request_id
        que sello el productor tienen que sobrevivir al viaje por el almacen.
        """
        await store.append(
            "pedido-1",
            [PedidoCreado()],
            expected_version=EXPECTED_VERSION_ANY,
            metadata={"actor_id": "u-1", "request_id": "r-9"},
        )

        await armar(store, bus, checkpoints).run_once()

        persistido = (await store.read_all())[0]
        _, sobre = store.rehydrate_with_metadata(persistido)
        assert sobre == {"actor_id": "u-1", "request_id": "r-9"}
        assert len(bus.publicados) == 1

    async def test_el_evento_publicado_es_el_original(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        original = PedidoPagado(monto=4200)
        await store.append(
            "pedido-1", [original], expected_version=EXPECTED_VERSION_ANY
        )

        await armar(store, bus, checkpoints).run_once()

        publicado = bus.publicados[0]
        assert isinstance(publicado, PedidoPagado)
        assert publicado.monto == 4200
        assert publicado.event_id == original.event_id


class TestLaCaida:
    async def test_un_fallo_no_avanza_el_checkpoint_del_evento_que_fallo(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 4)
        bus = BusQueGraba(falla_en=3)

        with pytest.raises(RuntimeError):
            await armar(store, bus, checkpoints).run_once()

        assert await checkpoints.load("relay") == 2
        assert len(bus.publicados) == 2

    async def test_tras_el_fallo_se_retoma_donde_quedo(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 4)
        bus = BusQueGraba(falla_en=3)
        relay = armar(store, bus, checkpoints)
        with pytest.raises(RuntimeError):
            await relay.run_once()

        bus.falla_en = None
        publicados = await relay.run_once()

        assert publicados == 2
        assert len(bus.publicados) == 4

    async def test_una_caida_entre_publicar_y_guardar_republica(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        """
        La justificacion de que la entrega sea at-least-once.

        Se simula el proceso muriendo despues de publicar y antes de guardar el checkpoint.
        Al reiniciar, el lote sale otra vez. El orden inverso -- guardar antes de publicar --
        perderia esos eventos para siempre, porque al reiniciar el checkpoint diria que ya
        se entregaron.
        """
        await sembrar(store, 2)
        bus = BusQueGraba()

        async def morir(_suscripcion: str, _posicion: int) -> None:
            raise RuntimeError("el proceso murio antes de guardar el checkpoint")

        checkpoints.save = morir  # pyright: ignore[reportAttributeAccessIssue]
        with pytest.raises(RuntimeError):
            await armar(store, bus, checkpoints).run_once()

        assert len(bus.publicados) == 2

        # El proceso reinicia: el checkpoint sigue en 0.
        checkpoints_nuevo = InMemoryCheckpointStore()
        bus_nuevo = BusQueGraba()
        await armar(store, bus_nuevo, checkpoints_nuevo).run_once()

        assert len(bus_nuevo.publicados) == 2, (
            "el lote no se republico: guardar el checkpoint antes de publicar perderia "
            "estos eventos para siempre"
        )

    async def test_skip_avanza_y_sigue(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 4)
        bus = BusQueGraba(falla_en=3)

        publicados = await armar(store, bus, checkpoints, on_error="skip").run_once()

        assert publicados == 3
        assert await checkpoints.load("relay") == 4

    async def test_un_tipo_irresoluble_nombra_la_posicion(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        await sembrar(store, 1)
        store._todos[0] = store._todos[0].model_copy(
            update={"payload": {"__type__": "app.borrado.Fantasma", "__data__": {}}}
        )

        with pytest.raises(DeserializationError) as excinfo:
            await armar(store, bus, checkpoints).run_once()

        assert "posicion 1" in str(excinfo.value)


class TestLaVentanaDeSeguridad:
    async def test_relee_por_detras_del_checkpoint(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        """Le da la chance de aparecer a una escritura que comiteo fuera de orden."""
        await sembrar(store, 4)
        await checkpoints.save("relay", 4)

        publicados = await armar(store, bus, checkpoints, safety_window=2).run_once()

        assert publicados == 2

    async def test_no_duplica_dentro_de_la_misma_corrida(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        await sembrar(store, 4)
        relay = armar(store, bus, checkpoints, safety_window=2)

        await relay.run_once()
        await relay.run_once()

        assert len(bus.publicados) == 4


class TestLaSuscripcion:
    async def test_el_relay_y_un_proyector_avanzan_por_separado(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        """
        Comparten almacen y no checkpoint: avanzan a ritmos distintos, y compartir clave
        haria que se pisaran el progreso.
        """
        await sembrar(store, 3)

        await armar(store, bus, checkpoints).run_once()

        assert await checkpoints.load("relay") == 3
        assert await checkpoints.load("default") == 0


class TestLaConstruccion:
    def test_batch_size_invalido_falla(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        with pytest.raises(ValueError):
            armar(store, bus, checkpoints, batch_size=0)

    def test_safety_window_negativo_falla(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        with pytest.raises(ValueError):
            armar(store, bus, checkpoints, safety_window=-1)


class TestStop:
    async def test_run_forever_sale_cuando_se_lo_detiene(
        self,
        store: InMemoryEventStore,
        bus: BusQueGraba,
        checkpoints: InMemoryCheckpointStore,
    ):
        await sembrar(store, 2)
        relay = armar(store, bus, checkpoints, poll_interval=0.01)

        tarea = asyncio.create_task(relay.run_forever())
        await asyncio.sleep(0.05)
        relay.stop()
        await asyncio.wait_for(tarea, timeout=1)

        assert len(bus.publicados) == 2
