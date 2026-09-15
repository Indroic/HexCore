"""
`RedisEventStore`: el contrato del adaptador, con un cliente simulado.

Se prueba con `AsyncMock`, como `test_redis_bus.py`: lo que hay que fijar es **como el
adaptador usa Redis**, no que Redis funcione. Y hay dos cosas ahi que son el diseno entero y
que se romperian sin que ningun otro test lo notara: que el `XADD` del stream del agregado
lleve un id explicito `<version>-0`, y que el error de Redis por ese id se traduzca a
`ConcurrencyError`.

Sin el id explicito, el adaptador seguiria funcionando en las pruebas de un solo escritor y
perderia la concurrencia optimista en produccion, en silencio.
"""
from __future__ import annotations

import json
import typing as t
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("redis")

from hexcore.domain.events import DomainEvent  # noqa: E402
from hexcore.domain.eventsourcing import (  # noqa: E402
    EXPECTED_VERSION_ANY,
    EXPECTED_VERSION_NO_STREAM,
    ConcurrencyError,
)
from hexcore.infrastructure.eventsourcing.redis_store import (  # noqa: E402
    SCRIPT_DE_APPEND,
    RedisCheckpointStore,
    RedisEventStore,
)


class Pagado(DomainEvent):
    monto: int = 0


class ScriptSimulado:
    """
    Sustituye al callable que devuelve `register_script`.

    Ejecuta la parte del script que importa para el test -- asignar versiones y posiciones --
    y graba con que argumentos se lo llamo, para poder afirmar sobre ellos.
    """

    def __init__(self, version_inicial: int = 0, error: Exception | None = None) -> None:
        self.version_inicial = version_inicial
        self.error = error
        self.llamadas: list[dict[str, t.Any]] = []

    async def __call__(self, *, keys: list[str], args: list[t.Any]) -> str:
        self.llamadas.append({"keys": keys, "args": args})
        if self.error is not None:
            raise self.error

        esperada, cualquiera, cuantos = int(args[0]), int(args[1]), int(args[2])
        if esperada != cualquiera and self.version_inicial != esperada:
            raise RuntimeError(f"HEXCORE_CONCURRENCY:{self.version_inicial}")

        return json.dumps(
            [
                [self.version_inicial + i, self.version_inicial + i]
                for i in range(1, cuantos + 1)
            ]
        )


@pytest.fixture
def cliente() -> MagicMock:
    redis = MagicMock()
    redis.register_script = MagicMock(return_value=ScriptSimulado())
    redis.xrange = AsyncMock(return_value=[])
    redis.xrevrange = AsyncMock(return_value=[])
    redis.hget = AsyncMock(return_value=None)
    redis.hset = AsyncMock()
    redis.hdel = AsyncMock()
    return redis


@pytest.fixture
def store(cliente: MagicMock) -> RedisEventStore:
    return RedisEventStore(cliente)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


pytestmark = pytest.mark.anyio


class TestElScript:
    def test_usa_un_id_explicito_por_version(self):
        """
        El corazon del diseno. Con un id explicito, Redis rechaza cualquier id menor o igual
        al ultimo del stream: la version del agregado **es** la posicion en el stream, y el
        segundo escritor recibe el error del propio Redis. Sin `WATCH` y sin locks.
        """
        assert "version .. '-0'" in SCRIPT_DE_APPEND

    def test_nunca_recorta_el_stream(self):
        """`MAXLEN` en un log de eventos es destruirlo."""
        assert "MAXLEN" not in SCRIPT_DE_APPEND.upper()

    def test_reserva_el_bloque_de_posiciones_de_una_vez(self):
        """Un `INCRBY` por append, no uno por evento."""
        assert SCRIPT_DE_APPEND.count("INCRBY") == 1

    def test_escribe_en_los_dos_streams(self):
        """El del agregado da el orden por stream; el global, el orden para el relay."""
        assert SCRIPT_DE_APPEND.count("redis.call('XADD'") == 2

    def test_la_version_viaja_como_campo(self):
        """
        El id del stream global es autogenerado, asi que la version no se puede derivar de
        el: tiene que ir como campo en las dos escrituras.
        """
        assert "'v', version" in SCRIPT_DE_APPEND


class TestElRegistroDelScript:
    async def test_se_registra_una_sola_vez(
        self, store: RedisEventStore, cliente: MagicMock
    ):
        """
        `register_script` devuelve un callable que usa EVALSHA; cachearlo evita reenviar el
        cuerpo del script en cada append.
        """
        await store.append(
            "pedido-1", [Pagado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )
        await store.append("pedido-1", [Pagado()], expected_version=EXPECTED_VERSION_ANY)

        assert cliente.register_script.call_count == 1

    async def test_se_le_pasan_las_tres_claves(
        self, store: RedisEventStore, cliente: MagicMock
    ):
        script = cliente.register_script.return_value

        await store.append(
            "pedido-1", [Pagado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        keys = script.llamadas[0]["keys"]
        assert keys[0] == "es:stream:pedido-1"
        assert keys[1] == "es:$all"
        assert keys[2] == "es:position"


class TestAppend:
    async def test_devuelve_las_versiones_y_posiciones_asignadas(
        self, store: RedisEventStore
    ):
        escritos = await store.append(
            "pedido-1",
            [Pagado(monto=1), Pagado(monto=2)],
            expected_version=EXPECTED_VERSION_NO_STREAM,
        )

        assert [e.version for e in escritos] == [1, 2]
        assert [e.global_position for e in escritos] == [1, 2]

    async def test_una_lista_vacia_no_llama_al_script(
        self, store: RedisEventStore, cliente: MagicMock
    ):
        assert await store.append("pedido-1", [], expected_version=0) == []
        cliente.register_script.assert_not_called()

    async def test_deriva_el_stream_type(self, store: RedisEventStore):
        escritos = await store.append(
            "pedido-8dcde138-c8b3-4379-89b3-a1150b3d516f",
            [Pagado()],
            expected_version=EXPECTED_VERSION_NO_STREAM,
        )

        assert escritos[0].stream_type == "pedido"

    async def test_el_payload_lleva_el_sobre(self, store: RedisEventStore):
        escritos = await store.append(
            "pedido-1",
            [Pagado()],
            expected_version=EXPECTED_VERSION_NO_STREAM,
            metadata={"actor_id": "u-1"},
        )

        _, sobre = store.rehydrate_with_metadata(escritos[0])
        assert sobre == {"actor_id": "u-1"}

    async def test_el_evento_se_puede_reconstruir(self, store: RedisEventStore):
        original = Pagado(monto=4200)

        escritos = await store.append(
            "pedido-1", [original], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        recuperado = store.rehydrate(escritos[0])
        assert isinstance(recuperado, Pagado)
        assert recuperado.monto == 4200
        assert recuperado.event_id == original.event_id

    async def test_expected_version_any_pasa_el_centinela_al_script(
        self, store: RedisEventStore, cliente: MagicMock
    ):
        """El script necesita saber cual es el valor centinela para poder compararlo."""
        script = cliente.register_script.return_value

        await store.append(
            "pedido-1", [Pagado()], expected_version=EXPECTED_VERSION_ANY
        )

        args = script.llamadas[0]["args"]
        assert int(args[0]) == EXPECTED_VERSION_ANY
        assert int(args[1]) == EXPECTED_VERSION_ANY


class TestLaTraduccionDeErrores:
    async def test_el_conflicto_del_script_se_traduce(self, cliente: MagicMock):
        cliente.register_script.return_value = ScriptSimulado(version_inicial=7)
        store = RedisEventStore(cliente)

        with pytest.raises(ConcurrencyError) as excinfo:
            await store.append("pedido-1", [Pagado()], expected_version=3)

        assert excinfo.value.expected_version == 3
        assert excinfo.value.actual_version == 7

    async def test_el_rechazo_del_id_por_redis_se_traduce(self, cliente: MagicMock):
        """
        La carrera que la comprobacion explicita no puede cubrir: dos escritores que pasan el
        XREVRANGE antes de que cualquiera escriba. Redis rechaza el segundo XADD con este
        mensaje, y eso **es** un conflicto de concurrencia.
        """
        cliente.register_script.return_value = ScriptSimulado(
            error=RuntimeError(
                "ERR The ID specified in XADD is equal or smaller than the target "
                "stream top item"
            )
        )
        store = RedisEventStore(cliente)

        with pytest.raises(ConcurrencyError):
            await store.append("pedido-1", [Pagado()], expected_version=1)

    async def test_otro_error_se_propaga_sin_traducir(self, cliente: MagicMock):
        """
        Traducir cualquier error a ConcurrencyError haria que el llamador reintentara para
        siempre algo que no se arregla reintentando.
        """
        cliente.register_script.return_value = ScriptSimulado(
            error=RuntimeError("LOADING Redis is loading the dataset in memory")
        )
        store = RedisEventStore(cliente)

        with pytest.raises(RuntimeError, match="LOADING"):
            await store.append("pedido-1", [Pagado()], expected_version=1)


class TestLectura:
    def _entrada(
        self, version: int, posicion: int, *, stream_type: str = "pedido"
    ) -> tuple[bytes, dict[bytes, bytes]]:
        datos = {
            "event_id": "8dcde138-c8b3-4379-89b3-a1150b3d516f",
            "stream_id": "pedido-1",
            "stream_type": stream_type,
            "event_type": "tests.test_eventsourcing_redis.Pagado",
            "payload": {"__type__": "x", "__data__": {}},
            "occurred_on": "2026-01-01T12:00:00+00:00",
            "recorded_at": "2026-01-01T12:00:00+00:00",
        }
        return (
            f"{version}-0".encode(),
            {
                b"p": json.dumps(datos).encode(),
                b"g": str(posicion).encode(),
                b"v": str(version).encode(),
            },
        )

    async def test_read_stream_pide_el_rango_por_version(
        self, store: RedisEventStore, cliente: MagicMock
    ):
        """El id de la entrada es `<version>-0`, asi que el rango se expresa en versiones."""
        cliente.xrange.return_value = [self._entrada(2, 5)]

        leidos = await store.read_stream("pedido-1", from_version=2, to_version=4)

        cliente.xrange.assert_awaited_once()
        _, kwargs = cliente.xrange.call_args
        assert kwargs["min"] == "2-0"
        assert kwargs["max"] == "4-0"
        assert leidos[0].version == 2

    async def test_read_stream_lee_la_version_del_campo(
        self, store: RedisEventStore, cliente: MagicMock
    ):
        cliente.xrange.return_value = [self._entrada(3, 9)]

        leidos = await store.read_stream("pedido-1")

        assert leidos[0].version == 3
        assert leidos[0].global_position == 9

    async def test_read_all_descarta_lo_anterior_al_corte(
        self, store: RedisEventStore, cliente: MagicMock
    ):
        """
        `from_position` es exclusivo, igual que en los otros adaptadores.

        El `side_effect` simula la paginacion: la segunda vuelta del bucle no encuentra nada
        mas. Con un `return_value` fijo, el mock devolveria el mismo bloque para siempre y el
        adaptador acumularia duplicados hasta llenar el `limit` -- un artefacto del doble, no
        del codigo.
        """
        cliente.xrange.side_effect = [
            [self._entrada(1, 1), self._entrada(2, 2), self._entrada(3, 3)],
            [],
        ]

        leidos = await store.read_all(from_position=2, limit=10)

        assert [p.global_position for p in leidos] == [3]

    async def test_read_all_filtra_por_categoria_en_el_cliente(
        self, store: RedisEventStore, cliente: MagicMock
    ):
        """Un stream de Redis no tiene indices secundarios: la categoria se filtra despues."""
        cliente.xrange.side_effect = [
            [
                self._entrada(1, 1, stream_type="pedido"),
                self._entrada(1, 2, stream_type="usuario"),
            ],
            [],
        ]

        leidos = await store.read_all(stream_types=["usuario"], limit=10)

        assert len(leidos) == 1
        assert leidos[0].stream_type == "usuario"

    async def test_stream_version_lee_el_ultimo_id(
        self, store: RedisEventStore, cliente: MagicMock
    ):
        cliente.xrevrange.return_value = [self._entrada(7, 7)]

        assert await store.stream_version("pedido-1") == 7

    async def test_un_stream_inexistente_es_version_cero(
        self, store: RedisEventStore, cliente: MagicMock
    ):
        cliente.xrevrange.return_value = []

        assert await store.stream_version("no-existe") == 0

    async def test_acepta_respuestas_decodificadas(
        self, store: RedisEventStore, cliente: MagicMock
    ):
        """
        `redis.asyncio` devuelve bytes salvo con `decode_responses=True`. Un adaptador que
        asuma una de las dos formas funciona con la mitad de las configuraciones.
        """
        _id, campos = self._entrada(1, 1)
        cliente.xrange.return_value = [
            ("1-0", {k.decode(): v.decode() for k, v in campos.items()})
        ]

        leidos = await store.read_stream("pedido-1")

        assert leidos[0].version == 1


class TestElCheckpointStore:
    async def test_una_suscripcion_nueva_es_cero(self, cliente: MagicMock):
        checkpoints = RedisCheckpointStore(cliente)

        assert await checkpoints.load("nueva") == 0

    async def test_round_trip(self, cliente: MagicMock):
        checkpoints = RedisCheckpointStore(cliente)
        cliente.hget.return_value = b"42"

        await checkpoints.save("proyeccion", 42)

        cliente.hset.assert_awaited_once_with("es:checkpoints", "proyeccion", "42")
        assert await checkpoints.load("proyeccion") == 42

    async def test_reset_borra_la_entrada(self, cliente: MagicMock):
        checkpoints = RedisCheckpointStore(cliente)

        await checkpoints.reset("proyeccion")

        cliente.hdel.assert_awaited_once_with("es:checkpoints", "proyeccion")
