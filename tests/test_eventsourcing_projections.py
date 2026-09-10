"""
`Projector`: avance del checkpoint, filtrado, rebuild y politica de error.

Los dos casos que mas importan estan en `TestLaPoliticaDeError`. Un proyector que avanza el
checkpoint por delante de lo que aplico convierte un fallo transitorio en datos incorrectos
para siempre: al reintentar, el checkpoint dice que el evento que fallo ya se proceso, y el
modelo de lectura queda mal sin que nada lo indique. Que el checkpoint nunca se adelante al
trabajo hecho es lo unico que hace recuperable un fallo.
"""
from __future__ import annotations

import typing as t

import pytest

from hexcore.application.eventsourcing import Projector
from hexcore.domain.cqrs.exceptions import DeserializationError
from hexcore.domain.events import DomainEvent
from hexcore.domain.eventsourcing import (
    EXPECTED_VERSION_ANY,
    AbstractProjection,
    StoredEvent,
)
from hexcore.infrastructure.eventsourcing.memory import (
    InMemoryCheckpointStore,
    InMemoryEventStore,
)


class PedidoEvent(DomainEvent):
    pass


class PedidoCreado(PedidoEvent):
    cliente: str = "acme"


class PedidoPagado(PedidoEvent):
    monto: int = 0


class EventoAjeno(DomainEvent):
    pass


class Grabadora(AbstractProjection):
    """Graba lo que recibe. El equivalente de `RecordedEnqueue` para proyecciones."""

    name = "grabadora"

    def __init__(self, handles: tuple[type[DomainEvent], ...] = ()) -> None:
        self.handles = handles  # pyright: ignore[reportIncompatibleVariableOverride]
        self.recibidos: list[tuple[DomainEvent, StoredEvent]] = []
        self.reseteos = 0

    async def apply(self, event: DomainEvent, stored: StoredEvent) -> None:
        self.recibidos.append((event, stored))

    async def reset(self) -> None:
        self.reseteos += 1
        self.recibidos.clear()

    @property
    def tipos(self) -> list[str]:
        return [type(evento).__name__ for evento, _ in self.recibidos]


class Explosiva(AbstractProjection):
    """Falla al aplicar el evento que ocupa la posicion global indicada."""

    name = "explosiva"

    def __init__(self, falla_en: int) -> None:
        self.falla_en = falla_en
        self.aplicados: list[int] = []

    async def apply(self, event: DomainEvent, stored: StoredEvent) -> None:
        if stored.global_position == self.falla_en:
            raise RuntimeError(f"la proyeccion no pudo con la posicion {self.falla_en}")
        self.aplicados.append(stored.global_position)


@pytest.fixture
def store() -> InMemoryEventStore:
    return InMemoryEventStore()


@pytest.fixture
def checkpoints() -> InMemoryCheckpointStore:
    return InMemoryCheckpointStore()


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
    checkpoints: InMemoryCheckpointStore,
    *proyecciones: AbstractProjection,
    **opciones: t.Any,
) -> Projector:
    return Projector(
        store=store,
        checkpoints=checkpoints,
        projections=list(proyecciones),
        **opciones,
    )


class TestLaConstruccion:
    def test_batch_size_invalido_falla(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        with pytest.raises(ValueError):
            armar(store, checkpoints, batch_size=0)

    def test_safety_window_negativo_falla(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        with pytest.raises(ValueError):
            armar(store, checkpoints, safety_window=-1)


class TestCatchUp:
    async def test_aplica_todos_los_eventos(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 3)
        grabadora = Grabadora()

        procesados = await armar(store, checkpoints, grabadora).catch_up()

        assert procesados == 3
        assert len(grabadora.recibidos) == 3

    async def test_avanza_el_checkpoint(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 3)

        await armar(store, checkpoints, Grabadora()).catch_up()

        assert await checkpoints.load("default") == 3

    async def test_cada_evento_se_aplica_una_sola_vez(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 3)
        grabadora = Grabadora()
        proyector = armar(store, checkpoints, grabadora)

        await proyector.catch_up()
        await proyector.catch_up()

        assert len(grabadora.recibidos) == 3

    async def test_una_segunda_pasada_solo_toma_lo_nuevo(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 2)
        grabadora = Grabadora()
        proyector = armar(store, checkpoints, grabadora)
        await proyector.catch_up()

        await store.append(
            "pedido-2", [PedidoCreado()], expected_version=EXPECTED_VERSION_ANY
        )
        procesados = await proyector.catch_up()

        assert procesados == 1
        assert len(grabadora.recibidos) == 3

    async def test_pagina_cuando_hay_mas_que_el_lote(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 7)
        grabadora = Grabadora()

        procesados = await armar(
            store, checkpoints, grabadora, batch_size=2
        ).catch_up()

        assert procesados == 7
        posiciones = [persistido.global_position for _, persistido in grabadora.recibidos]
        assert posiciones == sorted(posiciones), "el orden global no se respeto"

    async def test_un_almacen_vacio_no_hace_nada(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        assert await armar(store, checkpoints, Grabadora()).catch_up() == 0
        assert await checkpoints.load("default") == 0

    async def test_la_proyeccion_recibe_el_evento_y_su_persistido(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        """
        El StoredEvent va aparte porque el evento de dominio no conoce su stream_id ni su
        posicion, y una proyeccion casi siempre necesita al menos el primero.
        """
        await sembrar(store, 1)
        grabadora = Grabadora()

        await armar(store, checkpoints, grabadora).catch_up()

        evento, persistido = grabadora.recibidos[0]
        assert isinstance(evento, PedidoCreado)
        assert persistido.stream_id == "pedido-1"
        assert persistido.global_position == 1

    async def test_varias_proyecciones_avanzan_juntas(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        """Compartir checkpoint es lo que las mantiene consistentes entre si."""
        await sembrar(store, 3)
        una, otra = Grabadora(), Grabadora()

        await armar(store, checkpoints, una, otra).catch_up()

        assert len(una.recibidos) == len(otra.recibidos) == 3


class TestElFiltradoPorHandles:
    async def test_sin_handles_recibe_todo(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        """Vacio significa todos: una proyeccion que filtra de mas se queda callada."""
        await sembrar(store, 2)
        await store.append(
            "otro-1", [EventoAjeno()], expected_version=EXPECTED_VERSION_ANY
        )
        grabadora = Grabadora()

        await armar(store, checkpoints, grabadora).catch_up()

        assert len(grabadora.recibidos) == 3

    async def test_filtra_por_el_tipo_declarado(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 3)
        grabadora = Grabadora(handles=(PedidoPagado,))

        await armar(store, checkpoints, grabadora).catch_up()

        assert grabadora.tipos == ["PedidoPagado", "PedidoPagado"]

    async def test_declarar_la_base_alcanza_a_las_subclases(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 2)
        await store.append(
            "otro-1", [EventoAjeno()], expected_version=EXPECTED_VERSION_ANY
        )
        grabadora = Grabadora(handles=(PedidoEvent,))

        await armar(store, checkpoints, grabadora).catch_up()

        assert grabadora.tipos == ["PedidoCreado", "PedidoPagado"]

    async def test_el_checkpoint_avanza_aunque_la_proyeccion_filtre(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        """
        Si no avanzara, un proyector que solo mira un tipo raro releeria el almacen entero en
        cada arranque.
        """
        await sembrar(store, 3)

        await armar(
            store, checkpoints, Grabadora(handles=(EventoAjeno,))
        ).catch_up()

        assert await checkpoints.load("default") == 3


class TestLaPoliticaDeError:
    async def test_stop_no_avanza_el_checkpoint_del_evento_que_fallo(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        """
        El caso central. Si avanzara, al reintentar el checkpoint diria que el evento que
        fallo ya se proceso y el modelo de lectura quedaria mal para siempre.
        """
        await sembrar(store, 4)
        explosiva = Explosiva(falla_en=3)

        with pytest.raises(RuntimeError):
            await armar(store, checkpoints, explosiva).catch_up()

        assert await checkpoints.load("default") == 2, (
            "el checkpoint se adelanto al trabajo hecho: el evento que fallo quedaria "
            "marcado como procesado"
        )
        assert explosiva.aplicados == [1, 2]

    async def test_stop_permite_retomar_exactamente_donde_fallo(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 4)
        explosiva = Explosiva(falla_en=3)
        proyector = armar(store, checkpoints, explosiva)
        with pytest.raises(RuntimeError):
            await proyector.catch_up()

        explosiva.falla_en = -1  # el problema transitorio se resolvio
        procesados = await proyector.catch_up()

        assert procesados == 2
        assert explosiva.aplicados == [1, 2, 3, 4]

    async def test_stop_propaga_el_error_original(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 2)

        with pytest.raises(RuntimeError, match="no pudo con la posicion"):
            await armar(store, checkpoints, Explosiva(falla_en=1)).catch_up()

    async def test_skip_avanza_y_sigue(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 4)
        explosiva = Explosiva(falla_en=3)

        procesados = await armar(
            store, checkpoints, explosiva, on_error="skip"
        ).catch_up()

        assert procesados == 3, "el que fallo no cuenta como aplicado"
        assert explosiva.aplicados == [1, 2, 4]
        assert await checkpoints.load("default") == 4

    async def test_un_tipo_irresoluble_falla_nombrando_la_posicion(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        """
        Caso real: la clase se renombro o se borro. Sin la posicion y el tipo en el mensaje,
        un rebuild que falla no dice sobre que y hay que buscarlo a mano en la tabla.
        """
        await sembrar(store, 1)
        store._todos[0] = store._todos[0].model_copy(
            update={"payload": {"__type__": "app.borrado.Fantasma", "__data__": {}}}
        )
        store._streams["pedido-1"][0] = store._todos[0]

        with pytest.raises(DeserializationError) as excinfo:
            await armar(store, checkpoints, Grabadora()).catch_up()

        mensaje = str(excinfo.value)
        assert "app.borrado.Fantasma" in mensaje
        assert "posicion 1" in mensaje


class TestRebuild:
    async def test_resetea_cada_proyeccion(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 3)
        una, otra = Grabadora(), Grabadora()
        proyector = armar(store, checkpoints, una, otra)
        await proyector.catch_up()

        await proyector.rebuild()

        assert una.reseteos == otra.reseteos == 1

    async def test_reprocesa_desde_el_principio(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 3)
        grabadora = Grabadora()
        proyector = armar(store, checkpoints, grabadora)
        await proyector.catch_up()

        procesados = await proyector.rebuild()

        assert procesados == 3
        assert len(grabadora.recibidos) == 3, "quedaron datos del pase anterior"

    async def test_deja_el_mismo_estado_que_la_primera_pasada(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        """La propiedad que hace innecesaria una migracion de modelo de lectura."""
        await sembrar(store, 4)
        grabadora = Grabadora()
        proyector = armar(store, checkpoints, grabadora)
        await proyector.catch_up()
        primera = list(grabadora.tipos)

        await proyector.rebuild()

        assert grabadora.tipos == primera

    async def test_deja_el_checkpoint_al_dia(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 3)
        proyector = armar(store, checkpoints, Grabadora())

        await proyector.rebuild()

        assert await checkpoints.load("default") == 3


class TestLaVentanaDeSeguridad:
    async def test_relee_por_detras_del_checkpoint(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        """
        Le da la chance de aparecer a una escritura rezagada: la transaccion que reservo la
        posicion 10 puede comitear despues de la que reservo la 11.
        """
        await sembrar(store, 4)
        await checkpoints.save("default", 4)
        grabadora = Grabadora()

        await armar(store, checkpoints, grabadora, safety_window=2).catch_up()

        assert len(grabadora.recibidos) == 2, "la ventana no se releyo"

    async def test_no_duplica_dentro_de_la_misma_corrida(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 4)
        grabadora = Grabadora()
        proyector = armar(store, checkpoints, grabadora, safety_window=2)

        await proyector.catch_up()
        await proyector.catch_up()

        assert len(grabadora.recibidos) == 4, "la ventana reprocesó lo que ya habia aplicado"

    async def test_sin_ventana_no_relee_nada(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 4)
        await checkpoints.save("default", 4)
        grabadora = Grabadora()

        await armar(store, checkpoints, grabadora, safety_window=0).catch_up()

        assert grabadora.recibidos == []


class TestLasSuscripciones:
    async def test_dos_proyectores_con_claves_distintas_avanzan_por_separado(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        await sembrar(store, 3)
        rapida, lenta = Grabadora(), Grabadora()

        await armar(store, checkpoints, rapida, subscription="rapida").catch_up()

        assert await checkpoints.load("rapida") == 3
        assert await checkpoints.load("lenta") == 0

        await armar(store, checkpoints, lenta, subscription="lenta").catch_up()
        assert len(lenta.recibidos) == 3


class TestStop:
    async def test_run_forever_sale_cuando_se_lo_detiene(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        import asyncio

        await sembrar(store, 2)
        grabadora = Grabadora()
        proyector = armar(store, checkpoints, grabadora, poll_interval=0.01)

        tarea = asyncio.create_task(proyector.run_forever())
        await asyncio.sleep(0.05)
        proyector.stop()
        await asyncio.wait_for(tarea, timeout=1)

        assert len(grabadora.recibidos) == 2

    async def test_run_forever_toma_lo_que_llega_despues(
        self, store: InMemoryEventStore, checkpoints: InMemoryCheckpointStore
    ):
        import asyncio

        grabadora = Grabadora()
        proyector = armar(store, checkpoints, grabadora, poll_interval=0.01)
        tarea = asyncio.create_task(proyector.run_forever())

        await asyncio.sleep(0.03)
        await store.append(
            "pedido-1", [PedidoCreado()], expected_version=EXPECTED_VERSION_ANY
        )
        await asyncio.sleep(0.05)
        proyector.stop()
        await asyncio.wait_for(tarea, timeout=1)

        assert len(grabadora.recibidos) == 1
