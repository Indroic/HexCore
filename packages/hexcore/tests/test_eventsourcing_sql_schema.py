"""
El esquema SQL del event store: las reglas que se rompen de una linea y se pagan caro.

Tres cosas, y ninguna se nota hasta que es tarde:

1. **Que los modelos no hereden `BaseModel[T]`.** Si heredaran, el UoW les pediria una
   entidad de dominio que no tienen, y lo haria durante el propio `commit()`.
2. **Que las tablas esten en `Base.metadata` tras `ensure_framework_models_loaded()`.** Si no,
   `alembic revision --autogenerate` les emite `op.drop_table` sin avisar -- y el event store
   no es una cache que se reconstruya, es lo que paso.
3. **Que `global_position` autoincremente en SQLite.** `BigInteger` no lo hace ahi, y la
   suite entera corre sobre SQLite.
"""
from __future__ import annotations

import typing as t

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from hexcore.domain.events import DomainEvent  # noqa: E402
from hexcore.domain.eventsourcing import EXPECTED_VERSION_NO_STREAM  # noqa: E402
from hexcore.infrastructure.eventsourcing.sqlalchemy_models import (  # noqa: E402
    DEFAULT_CHECKPOINT_TABLE,
    DEFAULT_EVENT_TABLE,
    DEFAULT_SNAPSHOT_TABLE,
    EVENTSTORE_MODELS,
    EventStoreModel,
    ProjectionCheckpointModel,
    SnapshotModel,
    create_eventstore_tables,
)
from hexcore.infrastructure.eventsourcing.sqlalchemy_models_mixins import (  # noqa: E402
    BIGINT_PORTABLE,
    EventStoreMixin,
    JSON_PORTABLE,
)
from hexcore.infrastructure.eventsourcing.sqlalchemy_store import (  # noqa: E402
    SqlAlchemyEventStore,
)
from hexcore.infrastructure.repositories.orms.sqlalchemy import Base  # noqa: E402


class Pagado(DomainEvent):
    monto: int = 0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def motor() -> t.AsyncIterator[t.Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    await create_eventstore_tables(engine)
    yield engine
    await engine.dispose()


class TestLaReglaDeBaseModel:
    def test_ningun_modelo_hereda_de_base_model(self):
        """
        La regla critica. `SqlAlchemyUnitOfWork.collect_domain_entities()` recorre
        `session.new | dirty | deleted`, filtra por `isinstance(model, BaseModel)` y le pide
        `get_domain_entity()` a cada fila. Una fila de eventos no tiene entidad detras, asi
        que heredar de ahi da un AttributeError -- y con el store enganchado al UoW esas
        filas estan en `session.new` **durante el commit**, con la transaccion a medias.

        Es un error de una linea, y este test son tres.
        """
        from hexcore.infrastructure.repositories.orms.sqlalchemy import BaseModel

        for modelo in EVENTSTORE_MODELS:
            assert not issubclass(modelo, BaseModel), (
                f"{modelo.__name__} hereda de BaseModel[T]: el UoW le va a pedir una entidad "
                f"de dominio que no tiene, y va a fallar dentro del commit()"
            )

    def test_los_modelos_si_heredan_de_base(self):
        """Sin `Base` no entrarian en el metadata, que es el otro modo de falla."""
        for modelo in EVENTSTORE_MODELS:
            assert issubclass(modelo, Base)

    def test_importar_los_mixins_no_registra_ninguna_tabla(self):
        """
        Es lo que permite componer un modelo propio con otro nombre de tabla sin que el
        default entre igual en el metadata.
        """
        assert not hasattr(EventStoreMixin, "__tablename__") or not isinstance(
            getattr(EventStoreMixin, "__tablename__", None), str
        )
        assert not issubclass(EventStoreMixin, Base)


class TestElRegistroEnElMetadata:
    def test_las_tres_tablas_entran_tras_ensure_framework_models_loaded(self):
        """
        Sin esto, el primer `alembic revision --autogenerate` de un consumidor le emite
        `op.drop_table` al event store. No es perder una cache: es perder lo que paso.
        """
        from hexcore.infrastructure.repositories.orms.sqlalchemy.utils import (
            ensure_framework_models_loaded,
        )

        ensure_framework_models_loaded()

        faltan = [
            nombre
            for nombre in (
                DEFAULT_EVENT_TABLE,
                DEFAULT_SNAPSHOT_TABLE,
                DEFAULT_CHECKPOINT_TABLE,
            )
            if nombre not in Base.metadata.tables
        ]

        assert faltan == [], (
            f"Estas tablas no estan en Base.metadata: {faltan}. --autogenerate les va a "
            f"emitir op.drop_table."
        )

    def test_el_modulo_de_modelos_esta_en_los_candidatos(self):
        """Fija el enganche, no solo su efecto: el efecto podria venir de otro import."""
        from hexcore.infrastructure.repositories.orms.sqlalchemy.utils import (
            ensure_framework_models_loaded,
        )

        assert (
            "hexcore.infrastructure.eventsourcing.sqlalchemy_models"
            in ensure_framework_models_loaded()
        )


class TestLosIndicesYConstraints:
    def test_el_unique_de_stream_y_version_existe(self):
        """
        La garantia real de la concurrencia optimista. El SELECT MAX(version) previo es un
        TOCTOU: entre la lectura y el INSERT cabe otra transaccion.
        """
        constraints = {
            c.name for c in EventStoreModel.__table__.constraints if c.name is not None
        }

        assert f"uq_{DEFAULT_EVENT_TABLE}_stream_version" in constraints

    def test_el_event_id_es_unico(self):
        """La clave de idempotencia del relay y de las proyecciones."""
        columna = EventStoreModel.__table__.c.event_id

        assert columna.unique is True

    @pytest.mark.parametrize(
        "indice",
        [
            f"ix_{DEFAULT_EVENT_TABLE}_stream",
            f"ix_{DEFAULT_EVENT_TABLE}_type_position",
            f"ix_{DEFAULT_EVENT_TABLE}_event_type",
        ],
    )
    def test_los_indices_de_lectura_existen(self, indice: str):
        nombres = {i.name for i in EventStoreModel.__table__.indexes}

        assert indice in nombres

    def test_global_position_es_la_clave_primaria(self):
        """
        Un event store se lee casi siempre en orden global: el relay y las proyecciones no
        hacen otra cosa. Como PK, ese recorrido usa el indice agrupado.
        """
        pk = [c.name for c in EventStoreModel.__table__.primary_key.columns]

        assert pk == ["global_position"]

    def test_los_snapshots_guardan_historial_por_version(self):
        constraints = {
            c.name for c in SnapshotModel.__table__.constraints if c.name is not None
        }

        assert f"uq_{DEFAULT_SNAPSHOT_TABLE}_stream_version" in constraints

    def test_el_checkpoint_es_una_fila_por_suscripcion(self):
        pk = [c.name for c in ProjectionCheckpointModel.__table__.primary_key.columns]

        assert pk == ["subscription"]

    def test_ninguna_columna_se_llama_metadata(self):
        """`metadata` pisa `Base.metadata` y rompe el registro declarativo entero."""
        for modelo in EVENTSTORE_MODELS:
            assert "metadata" not in modelo.__table__.c, (
                f"{modelo.__name__} tiene una columna 'metadata', que pisa Base.metadata"
            )


class TestLaPortabilidad:
    def test_json_portable_usa_jsonb_solo_en_postgres(self):
        """`JSONB` a secas rompe en SQLite, y la suite corre sobre SQLite."""
        from sqlalchemy.dialects import postgresql, sqlite

        assert "JSONB" in JSON_PORTABLE.compile(dialect=postgresql.dialect())
        assert "JSON" in JSON_PORTABLE.compile(dialect=sqlite.dialect())

    def test_bigint_portable_baja_a_integer_en_sqlite(self):
        from sqlalchemy.dialects import postgresql, sqlite

        assert "BIGINT" in BIGINT_PORTABLE.compile(dialect=postgresql.dialect())
        assert BIGINT_PORTABLE.compile(dialect=sqlite.dialect()) == "INTEGER"

    @pytest.mark.anyio
    async def test_global_position_autoincrementa_en_sqlite(self, motor: t.Any):
        """
        El motivo de `BIGINT_PORTABLE`. SQLite solo autoincrementa una `INTEGER PRIMARY KEY`:
        una columna `BIGINT PRIMARY KEY` no recibe rowid implicito y el INSERT sin valor falla
        con NOT NULL.
        """
        fabrica = async_sessionmaker(motor, expire_on_commit=False)
        store = SqlAlchemyEventStore(session_scope=fabrica)

        primeros = await store.append(
            "pedido-1", [Pagado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )
        segundos = await store.append(
            "pedido-2", [Pagado()], expected_version=EXPECTED_VERSION_NO_STREAM
        )

        assert primeros[0].global_position == 1
        assert segundos[0].global_position == 2


class TestElOrdenSerializado:
    @pytest.mark.anyio
    async def test_falla_al_usarlo_fuera_de_postgres(self, motor: t.Any):
        """
        Se apoya en `pg_advisory_xact_lock`, que no tiene equivalente portable. Falla en vez
        de aceptar la opcion y no cumplirla: un almacen que promete orden estricto y no lo da
        es peor que uno que declara el hueco.
        """
        fabrica = async_sessionmaker(motor, expire_on_commit=False)
        store = SqlAlchemyEventStore(session_scope=fabrica, ordering="serialized")

        with pytest.raises(RuntimeError, match="PostgreSQL"):
            await store.append(
                "pedido-1", [Pagado()], expected_version=EXPECTED_VERSION_NO_STREAM
            )

    def test_un_ordering_desconocido_falla_al_construir(self):
        with pytest.raises(ValueError, match="ordering"):
            SqlAlchemyEventStore(ordering="lo-que-sea")  # pyright: ignore[reportArgumentType]


class TestLaSesion:
    def test_pasar_session_y_session_scope_a_la_vez_falla(self):
        """
        Con una sesion inyectada el adaptador no comitea, y con un scope abre y cierra la
        suya. Cual de los dos comportamientos se espera no se puede adivinar.
        """
        with pytest.raises(ValueError, match="no los dos"):
            SqlAlchemyEventStore(session=object(), session_scope=lambda: None)  # pyright: ignore[reportArgumentType]

    @pytest.mark.anyio
    async def test_con_sesion_prestada_no_comitea(self, motor: t.Any):
        """
        Es lo que permite que el evento y el cambio de negocio entren o no entren juntos: el
        commit lo da el llamador.
        """
        fabrica = async_sessionmaker(motor, expire_on_commit=False)

        async with fabrica() as session:
            store = SqlAlchemyEventStore(session=session)
            await store.append(
                "pedido-1", [Pagado()], expected_version=EXPECTED_VERSION_NO_STREAM
            )
            await session.rollback()

        otra = SqlAlchemyEventStore(session_scope=fabrica)
        assert await otra.stream_version("pedido-1") == 0, (
            "el adaptador comiteo por su cuenta: el rollback del llamador tendria que "
            "haberse llevado el evento"
        )
