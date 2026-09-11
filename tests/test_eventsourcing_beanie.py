"""
`BeanieEventStore`: lo que se puede fijar sin un MongoDB corriendo.

La forma de los documentos, sus indices y la traduccion de errores se verifican sin servidor.
Lo que necesita uno real -- el round-trip, el `$inc` atomico del contador, la unicidad de
`(stream_id, version)` -- va marcado con `@pytest.mark.mongo`, que `addopts` **deselecciona**.
No es lo mismo que saltarlo: el gate de CI exige cero SKIPPED, y esa marca existe justamente
para que un test que necesita infraestructura no cuente como salteado.

Para correr esos: `uv run pytest -m mongo` con un MongoDB en `mongo_uri`.
"""
from __future__ import annotations

import typing as t
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("beanie")

from beanie import Document  # noqa: E402
from pymongo import ReturnDocument  # noqa: E402
from pymongo.errors import DuplicateKeyError  # noqa: E402

from hexcore.domain.events import DomainEvent  # noqa: E402
from hexcore.domain.eventsourcing import (  # noqa: E402
    EXPECTED_VERSION_NO_STREAM,
    AbstractCheckpointStore,
    AbstractEventStore,
    AbstractSnapshotStore,
    ConcurrencyError,
)
from hexcore.infrastructure.eventsourcing.beanie_documents import (  # noqa: E402
    DEFAULT_CHECKPOINT_COLLECTION,
    DEFAULT_EVENT_COLLECTION,
    DEFAULT_SNAPSHOT_COLLECTION,
    EVENTSTORE_DOCUMENTS,
    ProjectionCheckpointDocument,
    SnapshotDocument,
    StoredEventDocument,
)
from hexcore.infrastructure.eventsourcing.beanie_store import (  # noqa: E402
    BeanieCheckpointStore,
    BeanieEventStore,
    BeanieSnapshotStore,
)


class Pagado(DomainEvent):
    monto: int = 0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class DocumentoFalso:
    """
    Un doble del documento de Beanie con atributos de verdad.

    `MagicMock` no sirve aca: `_a_stored` lee `documento.stream_id` y compania para armar el
    `StoredEvent`, y un mock devuelve otro mock -- que pydantic rechaza. El doble tiene que
    guardar los valores que le pasan, no fingir que existen.
    """

    coleccion_del_contador: t.ClassVar[t.Any] = None
    insertados: t.ClassVar[list[t.Any]] = []

    def __init__(self, **campos: t.Any) -> None:
        for nombre, valor in campos.items():
            setattr(self, nombre, valor)

    @classmethod
    def get_motor_collection(cls) -> t.Any:
        coleccion = MagicMock()
        coleccion.database = {"hexcore_eventstore_counters": cls.coleccion_del_contador}
        return coleccion

    @classmethod
    async def insert_many(cls, documentos: t.Sequence[t.Any]) -> None:
        cls.insertados = list(documentos)


class TestLosDocumentos:
    def test_no_heredan_de_base_document(self):
        """
        `BaseDocument` trae `is_root = True` -- herencia de una sola coleccion, con eventos,
        snapshots y checkpoints mezclados -- y `use_cache = True`, que sobre un log de
        eventos daria una version rancia del stream justo antes de decidir si un append entra
        o se rechaza por concurrencia.
        """
        from hexcore.infrastructure.repositories.orms.beanie import BaseDocument

        for documento in EVENTSTORE_DOCUMENTS:
            assert issubclass(documento, Document)
            assert not issubclass(documento, BaseDocument), (
                f"{documento.__name__} hereda BaseDocument: terminaria compartiendo "
                f"coleccion con los demas y leyendo de cache"
            )

    @pytest.mark.parametrize("documento", EVENTSTORE_DOCUMENTS)
    def test_desactivan_la_cache_explicitamente(self, documento: type[Document]):
        """Explicito y no por default: que quede escrito en el codigo, no heredado."""
        assert documento.Settings.use_cache is False  # pyright: ignore[reportAttributeAccessIssue]

    def test_cada_uno_tiene_su_propia_coleccion(self):
        nombres = {
            documento.Settings.name  # pyright: ignore[reportAttributeAccessIssue]
            for documento in EVENTSTORE_DOCUMENTS
        }

        assert nombres == {
            DEFAULT_EVENT_COLLECTION,
            DEFAULT_SNAPSHOT_COLLECTION,
            DEFAULT_CHECKPOINT_COLLECTION,
        }

    def test_el_indice_de_stream_y_version_es_unico(self):
        """
        La garantia real de la concurrencia optimista. Sin el, el `count` previo del
        adaptador es un TOCTOU y dos escritores pasan los dos.
        """
        indices = {
            indice.document["name"]: indice.document
            for indice in StoredEventDocument.Settings.indexes  # pyright: ignore[reportAttributeAccessIssue]
        }

        assert indices["uq_stream_version"]["unique"] is True

    def test_el_event_id_es_unico(self):
        """La clave de idempotencia del relay y de las proyecciones."""
        indices = {
            indice.document["name"]: indice.document
            for indice in StoredEventDocument.Settings.indexes  # pyright: ignore[reportAttributeAccessIssue]
        }

        assert indices["uq_event_id"]["unique"] is True

    def test_la_posicion_global_es_unica(self):
        indices = {
            indice.document["name"]: indice.document
            for indice in StoredEventDocument.Settings.indexes  # pyright: ignore[reportAttributeAccessIssue]
        }

        assert indices["uq_global_position"]["unique"] is True

    def test_los_snapshots_guardan_historial_por_version(self):
        """La clave es (stream_id, version), no stream_id solo: se acumula, no se pisa."""
        indices = {
            indice.document["name"]: indice.document
            for indice in SnapshotDocument.Settings.indexes  # pyright: ignore[reportAttributeAccessIssue]
        }

        assert indices["uq_stream_version"]["unique"] is True

    def test_los_checkpoints_son_uno_por_suscripcion(self):
        indices = {
            indice.document["name"]: indice.document
            for indice in ProjectionCheckpointDocument.Settings.indexes  # pyright: ignore[reportAttributeAccessIssue]
        }

        assert indices["uq_subscription"]["unique"] is True


class TestLosAdaptadores:
    def test_cumplen_los_puertos(self):
        assert issubclass(BeanieEventStore, AbstractEventStore)
        assert issubclass(BeanieSnapshotStore, AbstractSnapshotStore)
        assert issubclass(BeanieCheckpointStore, AbstractCheckpointStore)

    @pytest.mark.anyio
    async def test_un_append_vacio_no_toca_la_base(self):
        documento = MagicMock()
        store = BeanieEventStore(document=documento)

        assert await store.append("pedido-1", [], expected_version=0) == []

        documento.insert_many.assert_not_called()

    @pytest.mark.anyio
    async def test_la_version_esperada_incorrecta_falla_antes_de_escribir(self):
        documento = MagicMock()
        store = BeanieEventStore(document=documento)
        store.stream_version = AsyncMock(return_value=7)  # pyright: ignore[reportAttributeAccessIssue]

        with pytest.raises(ConcurrencyError) as excinfo:
            await store.append("pedido-1", [Pagado()], expected_version=3)

        assert excinfo.value.actual_version == 7
        documento.insert_many.assert_not_called()

    @pytest.mark.anyio
    async def test_el_duplicate_key_se_traduce_a_conflicto(self):
        """
        El chequeo de version es un TOCTOU: entre el `count` y el `insert_many` cabe otro
        escritor. Quien de verdad impide el duplicado es el indice unico, y su
        `DuplicateKeyError` **es** un conflicto de concurrencia.
        """
        documento = MagicMock()
        documento.insert_many = AsyncMock(
            side_effect=DuplicateKeyError("E11000 duplicate key error")
        )
        store = BeanieEventStore(document=documento)
        store.stream_version = AsyncMock(return_value=0)  # pyright: ignore[reportAttributeAccessIssue]
        store._reservar_posiciones = AsyncMock(return_value=1)  # pyright: ignore[reportAttributeAccessIssue]

        with pytest.raises(ConcurrencyError):
            await store.append(
                "pedido-1", [Pagado()], expected_version=EXPECTED_VERSION_NO_STREAM
            )

    @pytest.mark.anyio
    async def test_reserva_el_bloque_de_posiciones_de_una_vez(self):
        """
        Un round-trip por append, no uno por evento. Y con `$inc` atomico y
        `ReturnDocument.AFTER`: con BEFORE se leeria el valor anterior y dos appends
        simultaneos se pisarian las posiciones.
        """
        coleccion = MagicMock()
        coleccion.find_one_and_update = AsyncMock(return_value={"seq": 5})
        DocumentoFalso.coleccion_del_contador = coleccion
        DocumentoFalso.insertados = []

        store = BeanieEventStore(document=DocumentoFalso)
        store.stream_version = AsyncMock(return_value=0)  # pyright: ignore[reportAttributeAccessIssue]

        escritos = await store.append(
            "pedido-1",
            [Pagado(monto=1), Pagado(monto=2)],
            expected_version=EXPECTED_VERSION_NO_STREAM,
        )

        coleccion.find_one_and_update.assert_awaited_once()
        _, kwargs = coleccion.find_one_and_update.call_args
        assert kwargs["upsert"] is True
        assert kwargs["return_document"] is ReturnDocument.AFTER

        # Un solo $inc, del tamano del bloque entero.
        criterio, actualizacion = coleccion.find_one_and_update.call_args[0]
        assert actualizacion == {"$inc": {"seq": 2}}
        assert criterio == {"_id": "event_store"}

        # El bloque devuelto es [ultima - N + 1, ultima].
        assert [e.global_position for e in escritos] == [4, 5]
        assert [e.version for e in escritos] == [1, 2]


@pytest.mark.mongo
class TestContraMongoDeVerdad:
    """
    Lo que necesita un servidor. Deseleccionado por `addopts`; correr con `-m mongo`.

    Estos casos existen porque las tres propiedades que verifican -- el round-trip, la
    atomicidad del contador y la unicidad de la version -- dependen del servidor y no del
    codigo, asi que un doble que las simule no probaria nada.
    """

    @pytest.fixture
    async def store(self) -> t.AsyncIterator[BeanieEventStore]:
        from pymongo import AsyncMongoClient

        from hexcore.config import LazyConfig
        from hexcore.infrastructure.eventsourcing.beanie_documents import (
            init_eventstore_documents,
        )

        cliente = AsyncMongoClient(LazyConfig.get_config().mongo_uri)
        base = cliente.get_default_database()
        await init_eventstore_documents(base)
        yield BeanieEventStore()
        for documento in EVENTSTORE_DOCUMENTS:
            await documento.find_all().delete()  # pyright: ignore[reportAttributeAccessIssue]

    @pytest.mark.anyio
    async def test_round_trip(self, store: BeanieEventStore):
        original = Pagado(monto=4200)

        await store.append(
            "pedido-1", [original], expected_version=EXPECTED_VERSION_NO_STREAM
        )
        leidos = await store.read_stream("pedido-1")

        recuperado = store.rehydrate(leidos[0])
        assert isinstance(recuperado, Pagado)
        assert recuperado.monto == 4200

    @pytest.mark.anyio
    async def test_dos_escritores_simultaneos(self, store: BeanieEventStore):
        import asyncio

        await store.append(
            "pedido-1", [Pagado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        resultados = await asyncio.gather(
            store.append("pedido-1", [Pagado(monto=1)], expected_version=1),
            store.append("pedido-1", [Pagado(monto=2)], expected_version=1),
            return_exceptions=True,
        )

        exitos = [r for r in resultados if isinstance(r, list)]
        assert len(exitos) == 1, "el indice unico de (stream_id, version) no esta creado"
