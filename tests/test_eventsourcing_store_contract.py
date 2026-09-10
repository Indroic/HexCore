"""
El contrato de `AbstractEventStore`, parametrizado sobre cada adaptador.

Un event store con dos implementaciones que se comportan distinto no es un puerto, son dos
almacenes con la misma firma. Y la diferencia aparece tarde: el consumidor desarrolla contra
el de memoria, pasa a producción con el de SQL, y descubre que `read_all` era exclusivo en
uno e inclusivo en el otro cuando una proyección se saltea el primer evento.

Por eso el contrato se escribe **una vez** y se corre contra todos. Cada adaptador nuevo
agrega una entrada a `ADAPTADORES` y hereda la suite entera; si no la pasa, el que está mal
es el adaptador, no el test.

La concurrencia optimista tiene sección propia porque es lo único de acá que no se puede
verificar leyendo el código: hay que provocar la carrera.
"""
from __future__ import annotations

import asyncio
import typing as t

import pytest

from hexcore.domain.events import DomainEvent
from hexcore.domain.eventsourcing import (
    EXPECTED_VERSION_ANY,
    EXPECTED_VERSION_NO_STREAM,
    AbstractEventStore,
    ConcurrencyError,
)
from hexcore.infrastructure.eventsourcing.memory import InMemoryEventStore


class Creado(DomainEvent):
    quien: str = "acme"


class Pagado(DomainEvent):
    monto: int = 0


ConstructorDeStore = t.Callable[[], t.AsyncContextManager[AbstractEventStore]]


class _StoreEnMemoria:
    """Envuelve el constructor en un context manager, para que SQL pueda cerrar su motor."""

    async def __aenter__(self) -> AbstractEventStore:
        return InMemoryEventStore()

    async def __aexit__(self, *_: object) -> None:
        return None


#: Cada adaptador que quiera cumplir el puerto se agrega acá y hereda la suite completa.
ADAPTADORES: dict[str, t.Callable[[], t.Any]] = {
    "memoria": _StoreEnMemoria,
}


@pytest.fixture(params=sorted(ADAPTADORES), ids=sorted(ADAPTADORES))
async def store(request: pytest.FixtureRequest) -> t.AsyncIterator[AbstractEventStore]:
    async with ADAPTADORES[request.param]() as adaptador:
        yield adaptador


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


pytestmark = pytest.mark.anyio


class TestAppend:
    async def test_asigna_versiones_consecutivas_desde_uno(self, store: AbstractEventStore):
        """1-based, para que 0 pueda significar "el stream no existe"."""
        escritos = await store.append(
            "pedido-1",
            [Creado(), Pagado(monto=1), Pagado(monto=2)],
            expected_version=EXPECTED_VERSION_NO_STREAM,
        )

        assert [persistido.version for persistido in escritos] == [1, 2, 3]

    async def test_asigna_posiciones_globales_crecientes(self, store: AbstractEventStore):
        primeros = await store.append(
            "pedido-1", [Creado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )
        segundos = await store.append(
            "pedido-2", [Creado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        assert segundos[0].global_position > primeros[0].global_position

    async def test_las_posiciones_globales_arrancan_en_uno(self, store: AbstractEventStore):
        """0 queda reservado para "no se leyó nada" en un checkpoint."""
        escritos = await store.append(
            "pedido-1", [Creado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        assert escritos[0].global_position >= 1

    async def test_una_lista_vacia_es_no_op(self, store: AbstractEventStore):
        assert await store.append("pedido-1", [], expected_version=0) == []
        assert await store.stream_version("pedido-1") == 0

    async def test_una_lista_vacia_no_valida_la_version(self, store: AbstractEventStore):
        """Guardar un agregado que no cambió no tiene por qué fallar por concurrencia."""
        await store.append(
            "pedido-1", [Creado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        assert await store.append("pedido-1", [], expected_version=999) == []

    async def test_deriva_el_stream_type_del_stream_id(self, store: AbstractEventStore):
        """El id suele ser un UUID, que lleva guiones: se corta en el primero."""
        escritos = await store.append(
            "pedido-8dcde138-c8b3-4379-89b3-a1150b3d516f",
            [Creado()],
            expected_version=EXPECTED_VERSION_NO_STREAM,
        )

        assert escritos[0].stream_type == "pedido"

    async def test_el_stream_type_explicito_gana(self, store: AbstractEventStore):
        escritos = await store.append(
            "cosa-1",
            [Creado()],
            expected_version=EXPECTED_VERSION_NO_STREAM,
            stream_type="categoria_propia",
        )

        assert escritos[0].stream_type == "categoria_propia"

    async def test_guarda_el_sobre_con_la_metadata(self, store: AbstractEventStore):
        """El actor y el request_id tienen que sobrevivir al viaje de ida y vuelta."""
        escritos = await store.append(
            "pedido-1",
            [Creado()],
            expected_version=EXPECTED_VERSION_NO_STREAM,
            metadata={"actor_id": "u-1"},
        )

        _, sobre = store.rehydrate_with_metadata(escritos[0])
        assert sobre == {"actor_id": "u-1"}

    async def test_el_event_type_es_el_fqn(self, store: AbstractEventStore):
        escritos = await store.append(
            "pedido-1", [Pagado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        assert escritos[0].event_type.endswith(".Pagado")
        assert "." in escritos[0].event_type, "es el FQN, no el nombre pelado"


class TestLaConcurrenciaOptimista:
    async def test_la_version_esperada_correcta_pasa(self, store: AbstractEventStore):
        await store.append(
            "pedido-1", [Creado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        escritos = await store.append("pedido-1", [Pagado()], expected_version=1)

        assert escritos[0].version == 2

    async def test_la_version_esperada_incorrecta_falla(self, store: AbstractEventStore):
        await store.append(
            "pedido-1", [Creado(), Pagado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        with pytest.raises(ConcurrencyError) as excinfo:
            await store.append("pedido-1", [Pagado()], expected_version=1)

        assert excinfo.value.expected_version == 1
        assert excinfo.value.actual_version == 2
        assert excinfo.value.stream_id == "pedido-1"

    async def test_no_stream_sobre_un_stream_existente_falla(self, store: AbstractEventStore):
        """Es lo que hace segura la creación: dos procesos con el mismo id, uno gana."""
        await store.append(
            "pedido-1", [Creado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        with pytest.raises(ConcurrencyError):
            await store.append(
                "pedido-1", [Creado()], expected_version=EXPECTED_VERSION_NO_STREAM
            )

    async def test_expected_version_any_no_comprueba(self, store: AbstractEventStore):
        """El modo event log: el UoW persistiendo eventos de entidades clásicas."""
        await store.append(
            "pedido-1", [Creado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        escritos = await store.append(
            "pedido-1", [Pagado()], expected_version=EXPECTED_VERSION_ANY
        )

        assert escritos[0].version == 2

    async def test_un_append_fallido_no_deja_nada_escrito(self, store: AbstractEventStore):
        """Atómico: o entran los N eventos, o ninguno."""
        await store.append(
            "pedido-1", [Creado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        with pytest.raises(ConcurrencyError):
            await store.append(
                "pedido-1", [Pagado(), Pagado()], expected_version=99
            )

        assert await store.stream_version("pedido-1") == 1

    async def test_de_dos_escritores_simultaneos_gana_exactamente_uno(
        self, store: AbstractEventStore
    ):
        """
        La carrera que `expected_version` existe para perder.

        Sin sincronización, las dos corrutinas pasan la comprobación de versión antes de que
        cualquiera escriba, y las dos escriben la versión 2. Es lo único de esta suite que no
        se puede verificar leyendo el código: hay que provocarlo.
        """
        await store.append(
            "pedido-1", [Creado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        resultados = await asyncio.gather(
            store.append("pedido-1", [Pagado(monto=1)], expected_version=1),
            store.append("pedido-1", [Pagado(monto=2)], expected_version=1),
            return_exceptions=True,
        )

        conflictos = [r for r in resultados if isinstance(r, ConcurrencyError)]
        exitos = [r for r in resultados if isinstance(r, list)]

        assert len(exitos) == 1, f"ganaron {len(exitos)} escritores, tiene que ganar uno"
        assert len(conflictos) == 1
        assert await store.stream_version("pedido-1") == 2


class TestReadStream:
    async def test_devuelve_el_historial_en_orden(self, store: AbstractEventStore):
        await store.append(
            "pedido-1",
            [Creado(), Pagado(monto=1), Pagado(monto=2)],
            expected_version=EXPECTED_VERSION_NO_STREAM,
        )

        leidos = await store.read_stream("pedido-1")

        assert [persistido.version for persistido in leidos] == [1, 2, 3]

    async def test_from_version_es_inclusivo(self, store: AbstractEventStore):
        """Con un snapshot en la 40, la cola se pide con from_version=41."""
        await store.append(
            "pedido-1",
            [Creado(), Pagado(), Pagado()],
            expected_version=EXPECTED_VERSION_NO_STREAM,
        )

        leidos = await store.read_stream("pedido-1", from_version=2)

        assert [persistido.version for persistido in leidos] == [2, 3]

    async def test_to_version_es_inclusivo(self, store: AbstractEventStore):
        await store.append(
            "pedido-1",
            [Creado(), Pagado(), Pagado()],
            expected_version=EXPECTED_VERSION_NO_STREAM,
        )

        leidos = await store.read_stream("pedido-1", to_version=2)

        assert [persistido.version for persistido in leidos] == [1, 2]

    async def test_limit_acota(self, store: AbstractEventStore):
        await store.append(
            "pedido-1",
            [Creado(), Pagado(), Pagado()],
            expected_version=EXPECTED_VERSION_NO_STREAM,
        )

        assert len(await store.read_stream("pedido-1", limit=2)) == 2

    async def test_un_stream_inexistente_da_lista_vacia(self, store: AbstractEventStore):
        """No lanza: en un event store, "no existe" y "está vacío" son lo mismo."""
        assert await store.read_stream("no-existe") == []

    async def test_no_mezcla_streams(self, store: AbstractEventStore):
        await store.append(
            "pedido-1", [Creado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )
        await store.append(
            "pedido-2", [Creado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        assert len(await store.read_stream("pedido-1")) == 1


class TestReadAll:
    async def _sembrar(self, store: AbstractEventStore) -> None:
        await store.append(
            "pedido-1", [Creado(), Pagado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )
        await store.append(
            "usuario-1", [Creado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )
        await store.append("pedido-1", [Pagado()], expected_version=2)

    async def test_devuelve_todo_ordenado_por_posicion_global(
        self, store: AbstractEventStore
    ):
        await self._sembrar(store)

        leidos = await store.read_all()

        posiciones = [persistido.global_position for persistido in leidos]
        assert posiciones == sorted(posiciones)
        assert len(leidos) == 4

    async def test_from_position_es_exclusivo(self, store: AbstractEventStore):
        """
        Estrictamente mayor, así un checkpoint se pasa tal cual sin sumarle uno. Si fuera
        inclusivo, todo consumidor reprocesaría un evento en cada arranque.
        """
        await self._sembrar(store)
        todos = await store.read_all()
        corte = todos[1].global_position

        leidos = await store.read_all(from_position=corte)

        assert all(persistido.global_position > corte for persistido in leidos)
        assert len(leidos) == 2

    async def test_from_position_cero_lee_desde_el_principio(
        self, store: AbstractEventStore
    ):
        await self._sembrar(store)

        assert len(await store.read_all(from_position=0)) == 4

    async def test_limit_acota(self, store: AbstractEventStore):
        await self._sembrar(store)

        assert len(await store.read_all(limit=2)) == 2

    async def test_filtra_por_categoria(self, store: AbstractEventStore):
        await self._sembrar(store)

        leidos = await store.read_all(stream_types=["usuario"])

        assert len(leidos) == 1
        assert leidos[0].stream_type == "usuario"

    async def test_paginar_recorre_todo_sin_repetir(self, store: AbstractEventStore):
        """La propiedad de la que depende el relay para avanzar."""
        await self._sembrar(store)

        vistos: list[int] = []
        posicion = 0
        while True:
            lote = await store.read_all(from_position=posicion, limit=2)
            if not lote:
                break
            vistos.extend(persistido.global_position for persistido in lote)
            posicion = lote[-1].global_position

        assert len(vistos) == len(set(vistos)) == 4

    async def test_un_almacen_vacio_da_lista_vacia(self, store: AbstractEventStore):
        assert await store.read_all() == []


class TestStreamVersion:
    async def test_un_stream_inexistente_es_cero(self, store: AbstractEventStore):
        """0 es lo mismo que `EXPECTED_VERSION_NO_STREAM`, y eso no es casualidad."""
        assert await store.stream_version("no-existe") == 0

    async def test_refleja_la_cantidad_de_eventos(self, store: AbstractEventStore):
        await store.append(
            "pedido-1", [Creado(), Pagado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        assert await store.stream_version("pedido-1") == 2

    async def test_stream_exists_es_coherente(self, store: AbstractEventStore):
        await store.append(
            "pedido-1", [Creado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        assert await store.stream_exists("pedido-1") is True
        assert await store.stream_exists("pedido-2") is False


class TestElRoundTrip:
    async def test_lo_que_entra_es_lo_que_sale(self, store: AbstractEventStore):
        evento = Pagado(monto=4200)

        escritos = await store.append(
            "pedido-1", [evento], expected_version=EXPECTED_VERSION_NO_STREAM
        )
        leidos = await store.read_stream("pedido-1")
        recuperado = store.rehydrate(leidos[0])

        assert isinstance(recuperado, Pagado)
        assert recuperado.monto == 4200
        assert recuperado.event_id == evento.event_id
        assert escritos[0].event_id == evento.event_id

    async def test_occurred_on_es_el_del_evento_y_recorded_at_el_de_la_escritura(
        self, store: AbstractEventStore
    ):
        evento = Pagado()

        escritos = await store.append(
            "pedido-1", [evento], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        assert escritos[0].occurred_on == evento.occurred_on
        assert escritos[0].recorded_at >= evento.occurred_on
