"""
Los puertos del Event Sourcing: lo que se puede fijar sin ningun adaptador.

El contrato de comportamiento de `AbstractEventStore` se prueba contra implementaciones
reales en `test_eventsourcing_store_contract.py`. Aca queda lo que es del puerto en si: la
forma de `StoredEvent`, los metodos concretos que el puerto resuelve por su cuenta, y que las
excepciones lleven la informacion que hace falta para actuar sobre ellas.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime

import pytest

from hexcore.domain.cqrs.exceptions import CQRSError, DeserializationError
from hexcore.domain.events import DomainEvent
from hexcore.domain.eventsourcing import (
    EXPECTED_VERSION_ANY,
    EXPECTED_VERSION_NO_STREAM,
    AbstractEventStore,
    AggregateNotFoundError,
    ConcurrencyError,
    EventSourcingError,
    Snapshot,
    StoredEvent,
    UnhandledEventError,
)
from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer

AHORA = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


class PedidoPagado(DomainEvent):
    monto: int = 100


class _StoreDeJuguete(AbstractEventStore):
    """El minimo que hace falta para ejercitar los metodos concretos del puerto."""

    def __init__(self, versiones: dict[str, int] | None = None) -> None:
        super().__init__(PydanticSerializer())
        self._versiones = versiones or {}

    async def append(
        self,
        stream_id: str,
        events: t.Sequence[DomainEvent],
        *,
        expected_version: int,
        stream_type: str | None = None,
        metadata: t.Mapping[str, t.Any] | None = None,
    ) -> list[StoredEvent]:
        raise NotImplementedError

    async def read_stream(
        self,
        stream_id: str,
        *,
        from_version: int = 1,
        to_version: int | None = None,
        limit: int | None = None,
    ) -> list[StoredEvent]:
        raise NotImplementedError

    async def read_all(
        self,
        *,
        from_position: int = 0,
        limit: int = 500,
        stream_types: t.Sequence[str] | None = None,
    ) -> list[StoredEvent]:
        raise NotImplementedError

    async def stream_version(self, stream_id: str) -> int:
        return self._versiones.get(stream_id, 0)


def _persistido(evento: DomainEvent, **extra: t.Any) -> StoredEvent:
    datos: dict[str, t.Any] = {
        "stream_id": "pedido-1",
        "stream_type": "pedido",
        "version": 1,
        "global_position": 1,
        "event_id": evento.event_id,
        "event_type": "tests.test_eventsourcing_ports.PedidoPagado",
        "payload": PydanticSerializer().serialize_envelope(evento),
        "occurred_on": evento.occurred_on,
        "recorded_at": AHORA,
    }
    datos.update(extra)
    return StoredEvent(**datos)


class TestStoredEvent:
    def test_es_inmutable(self):
        """Un event store es append-only: el pasado se corrige agregando, no editando."""
        persistido = _persistido(PedidoPagado())

        with pytest.raises(Exception):
            persistido.version = 99  # pyright: ignore[reportAttributeAccessIssue]

    def test_el_stream_id_es_str_y_no_uuid(self):
        """No todo stream es un agregado: hay sagas, categorias y streams de integracion."""
        persistido = _persistido(PedidoPagado(), stream_id="integracion:facturacion")

        assert persistido.stream_id == "integracion:facturacion"

    def test_guarda_el_sobre_entero_y_no_solo_los_datos(self):
        persistido = _persistido(PedidoPagado())

        assert "__type__" in persistido.payload
        assert "__data__" in persistido.payload

    def test_las_constantes_de_version_no_colisionan_con_una_version_real(self):
        """Las versiones son 1-based, asi que 0 y -1 quedan libres para los sentinelas."""
        assert EXPECTED_VERSION_NO_STREAM == 0
        assert EXPECTED_VERSION_ANY == -1


class TestLosMetodosConcretosDelPuerto:
    def test_rehydrate_reconstruye_el_evento_original(self):
        evento = PedidoPagado(monto=4200)
        store = _StoreDeJuguete()

        recuperado = store.rehydrate(_persistido(evento))

        assert isinstance(recuperado, PedidoPagado)
        assert recuperado.monto == 4200
        assert recuperado.event_id == evento.event_id

    def test_rehydrate_with_metadata_devuelve_el_sobre_aparte(self):
        """
        El relay republica con el sobre puesto: sin el, el actor y el request_id que sello el
        productor se pierden y los handlers ven un evento anonimo.
        """
        evento = PedidoPagado()
        payload = PydanticSerializer().serialize_envelope(evento, {"actor_id": "u-1"})
        store = _StoreDeJuguete()

        recuperado, sobre = store.rehydrate_with_metadata(
            _persistido(evento, payload=payload)
        )

        assert isinstance(recuperado, PedidoPagado)
        assert sobre == {"actor_id": "u-1"}

    def test_un_payload_sin_sobre_da_un_sobre_vacio(self):
        """Los eventos escritos antes de que hubiera proveedores registrados se releen igual."""
        evento = PedidoPagado()
        crudo = PydanticSerializer().serialize(evento)
        store = _StoreDeJuguete()

        _, sobre = store.rehydrate_with_metadata(_persistido(evento, payload=crudo))

        assert sobre == {}

    def test_un_tipo_que_ya_no_existe_falla_nombrandolo(self):
        """
        Caso real en un almacen con anos de historia: la clase se renombro o se borro. El
        error tiene que decir cual, o un rebuild que falla no dice sobre que.
        """
        store = _StoreDeJuguete()
        persistido = _persistido(
            PedidoPagado(),
            payload={"__type__": "app.borrado.EventoFantasma", "__data__": {}},
        )

        with pytest.raises(DeserializationError) as excinfo:
            store.rehydrate(persistido)

        assert "app.borrado.EventoFantasma" in str(excinfo.value)

    @pytest.mark.anyio
    async def test_stream_exists_se_apoya_en_stream_version(self):
        store = _StoreDeJuguete({"pedido-1": 3})

        assert await store.stream_exists("pedido-1") is True
        assert await store.stream_exists("pedido-inexistente") is False

    def test_el_serializador_por_defecto_es_el_de_pydantic(self):
        """
        Se importa tarde a proposito: vive en infrastructure/ y este es dominio puro.
        Hacerlo arriba invertiria la dependencia entre el puerto y su adaptador.
        """

        class SinSerializador(_StoreDeJuguete):
            def __init__(self) -> None:
                AbstractEventStore.__init__(self)

        assert isinstance(SinSerializador().serializer, PydanticSerializer)


class TestLasExcepciones:
    def test_no_cuelgan_de_la_jerarquia_de_cqrs(self):
        """
        Un `except CQRSError` escrito para tolerar "no hay handler" no debe comerse un
        conflicto de concurrencia: eso es una escritura perdida sin rastro.
        """
        assert not issubclass(EventSourcingError, CQRSError)
        assert not issubclass(ConcurrencyError, CQRSError)

    def test_concurrency_error_lleva_lo_que_hace_falta_para_reintentar(self):
        error = ConcurrencyError("pedido-1", expected=4, actual=7)

        assert error.stream_id == "pedido-1"
        assert error.expected_version == 4
        assert error.actual_version == 7
        assert "recarga" in str(error).lower()

    def test_aggregate_not_found_nombra_el_tipo_y_el_stream(self):
        error = AggregateNotFoundError("Pedido", "pedido-1")

        assert error.aggregate_type == "Pedido"
        assert error.stream_id == "pedido-1"
        assert "pedido-1" in str(error)

    def test_unhandled_event_explica_las_dos_salidas(self):
        """El mensaje tiene que decir como arreglarlo, porque el fallo es de diseno."""
        error = UnhandledEventError("Pedido", "PedidoPagado")

        mensaje = str(error)
        assert "@when" in mensaje
        assert "__strict_mutators__" in mensaje

    def test_todas_comparten_la_base(self):
        for excepcion in (ConcurrencyError, AggregateNotFoundError, UnhandledEventError):
            assert issubclass(excepcion, EventSourcingError)


class TestSnapshot:
    def test_es_inmutable(self):
        snapshot = Snapshot(
            stream_id="pedido-1",
            aggregate_type="app.Pedido",
            version=40,
            state={"total": 10},
            taken_at=AHORA,
        )

        with pytest.raises(Exception):
            snapshot.version = 41  # pyright: ignore[reportAttributeAccessIssue]

    def test_guarda_el_tipo_para_detectar_un_stream_reusado(self):
        """Restaurar un Pedido desde el snapshot de un Usuario da un objeto valido y basura."""
        snapshot = Snapshot(
            stream_id="x-1",
            aggregate_type="app.Pedido",
            version=1,
            state={},
            taken_at=AHORA,
        )

        assert snapshot.aggregate_type == "app.Pedido"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"
