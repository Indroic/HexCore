"""
Configuracion declarativa, factory y contenedor del Event Store.

Lo que mas importa de este archivo es que el serializador del almacen sea el mismo que el del
bus. Si divergen, un evento escrito por el store y otro publicado por el bus no tienen el
mismo formato -- y el dia que haya que releer el historial con el consumidor del bus, el
payload no se va a poder reconstruir. Es un fallo que no aparece hasta mucho despues.
"""
from __future__ import annotations

import typing as t

import pytest

from hexcore.application.eventsourcing.config import (
    EventStoreConfig,
    ProjectionsConfig,
    SnapshotConfig,
)
from hexcore.application.eventsourcing.factory import EventStoreFactory
from hexcore.config import ServerConfig
from hexcore.domain.events import DomainEvent
from hexcore.domain.eventsourcing import AbstractProjection, StoredEvent
from hexcore.infrastructure.api.eventsourcing import (
    configure_event_store,
    get_event_store_container,
    reset_event_store,
)
from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer
from hexcore.infrastructure.eventsourcing.memory import (
    InMemoryCheckpointStore,
    InMemoryEventStore,
    InMemorySnapshotStore,
)


class ProyeccionDePrueba(AbstractProjection):
    name = "de_prueba"

    async def apply(self, event: DomainEvent, stored: StoredEvent) -> None:
        return None


class ProyeccionConDependencias(AbstractProjection):
    name = "con_dependencias"

    def __init__(self, repositorio: object) -> None:
        self.repositorio = repositorio

    async def apply(self, event: DomainEvent, stored: StoredEvent) -> None:
        return None


@pytest.fixture(autouse=True)
def _limpiar() -> t.Iterator[None]:
    reset_event_store()
    yield
    reset_event_store()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestLaConfiguracion:
    def test_los_tres_modelos_son_inmutables(self):
        """Congelados: una config que cambia a mitad de ejecucion es un bug de arranque."""
        for config in (EventStoreConfig(), SnapshotConfig(), ProjectionsConfig()):
            with pytest.raises(Exception):
                config.enabled = False  # pyright: ignore[reportAttributeAccessIssue]

    def test_server_config_lo_trae_apagado_por_defecto(self):
        assert ServerConfig().event_store is None

    def test_server_config_lo_acepta(self):
        config = ServerConfig(event_store=EventStoreConfig(relay_enabled=True))

        assert config.event_store.relay_enabled is True

    def test_no_vive_dentro_de_cqrs(self):
        """
        CQRSConfig esta congelado y modela tres buses. Meter el event store ahi haria que
        CQRSConfig(enabled=False) apagara el almacen de la verdad.
        """
        from hexcore.application.cqrs.config import CQRSConfig

        assert "event_store" not in CQRSConfig.model_fields
        assert "event_store" in ServerConfig.model_fields

    def test_los_defaults_no_exigen_ningun_extra(self):
        """
        El default es el almacen en memoria, que **no persiste nada**. Es asi porque no exige
        extras, no porque sirva en produccion.
        """
        assert EventStoreConfig().backend is None
        assert EventStoreConfig().snapshots.backend is None


class TestLaFactory:
    def test_sin_backend_construye_el_de_memoria(self):
        store = EventStoreFactory(EventStoreConfig()).create_store()

        assert isinstance(store, InMemoryEventStore)

    def test_cachea_el_almacen(self):
        """
        Es lo que hace que el proyector y el relay lean del mismo sitio sin que el llamador
        tenga que pasarselo a mano.
        """
        factory = EventStoreFactory(EventStoreConfig())

        assert factory.create_store() is factory.create_store()

    def test_resuelve_el_backend_por_dotted_path(self):
        config = EventStoreConfig(
            backend="hexcore.infrastructure.eventsourcing.memory.InMemoryEventStore"
        )

        assert isinstance(EventStoreFactory(config).create_store(), InMemoryEventStore)

    def test_un_backend_inexistente_falla_nombrandolo(self):
        """
        Falla al construir y no en la primera escritura: un dotted path mal escrito tiene que
        romper el arranque, no el primer evento de produccion.
        """
        config = EventStoreConfig(backend="app.que.no.existe.Store")

        with pytest.raises(ValueError, match="app.que.no.existe.Store"):
            EventStoreFactory(config).create_store()

    def test_sin_snapshots_configurados_devuelve_none(self):
        assert EventStoreFactory(EventStoreConfig()).create_snapshot_store() is None

    def test_construye_el_snapshot_store(self):
        config = EventStoreConfig(
            snapshots=SnapshotConfig(
                backend="hexcore.infrastructure.eventsourcing.memory.InMemorySnapshotStore",
                every=10,
            )
        )

        assert isinstance(
            EventStoreFactory(config).create_snapshot_store(), InMemorySnapshotStore
        )

    def test_sin_checkpoint_backend_usa_el_de_memoria(self):
        assert isinstance(
            EventStoreFactory(EventStoreConfig()).create_checkpoint_store(),
            InMemoryCheckpointStore,
        )

    def test_instancia_las_proyecciones_de_la_config(self):
        # El dotted path no lleva el prefijo `tests.`: ese directorio no es un paquete
        # importable -- no tiene __init__.py -- y pytest carga los modulos por su nombre
        # simple, agregando el directorio a sys.path.
        config = EventStoreConfig(
            projections=ProjectionsConfig(
                projections=["test_eventsourcing_config.ProyeccionDePrueba"]
            )
        )

        proyecciones = EventStoreFactory(config).create_projections()

        assert len(proyecciones) == 1
        assert isinstance(proyecciones[0], ProyeccionDePrueba)

    def test_una_proyeccion_que_necesita_argumentos_falla_con_remediacion(self):
        """
        Mismo limite que BusConfig.middlewares: solo lo construible sin argumentos. El
        mensaje tiene que decir cual es la salida.
        """
        config = EventStoreConfig(
            projections=ProjectionsConfig(
                projections=["test_eventsourcing_config.ProyeccionConDependencias"]
            )
        )

        with pytest.raises(ValueError, match="Projector"):
            EventStoreFactory(config).create_projections()

    def test_el_proyector_recibe_lo_de_la_config(self):
        config = EventStoreConfig(
            projections=ProjectionsConfig(
                subscription="resumen", batch_size=10, safety_window=5
            )
        )

        proyector = EventStoreFactory(config).create_projector()

        assert proyector._subscription == "resumen"  # pyright: ignore[reportPrivateUsage]
        assert proyector._batch_size == 10  # pyright: ignore[reportPrivateUsage]
        assert proyector._safety_window == 5  # pyright: ignore[reportPrivateUsage]

    def test_el_relay_y_el_proyector_no_comparten_suscripcion(self):
        """
        Avanzan a ritmos distintos: compartir clave haria que se pisaran el progreso, y el
        mas lento no volveria a ver lo que el otro ya marco.
        """
        from unittest.mock import MagicMock

        factory = EventStoreFactory(EventStoreConfig())

        proyector = factory.create_projector()
        relay = factory.create_relay(MagicMock())

        assert relay._subscription != proyector._subscription  # pyright: ignore[reportPrivateUsage]

    def test_el_relay_lee_del_mismo_almacen_que_el_proyector(self):
        from unittest.mock import MagicMock

        factory = EventStoreFactory(EventStoreConfig())

        assert (
            factory.create_relay(MagicMock())._store  # pyright: ignore[reportPrivateUsage]
            is factory.create_projector()._store  # pyright: ignore[reportPrivateUsage]
        )


class TestElSerializador:
    def test_usa_el_inyectado(self):
        """
        El caso que importa: `configure_event_store` le pasa el de CQRS para que no diverjan.
        """
        compartido = PydanticSerializer()

        factory = EventStoreFactory(EventStoreConfig(), compartido)

        assert factory.create_serializer() is compartido

    def test_el_almacen_recibe_el_mismo_serializador(self):
        compartido = PydanticSerializer()

        store = EventStoreFactory(EventStoreConfig(), compartido).create_store()

        assert store.serializer is compartido

    def test_sin_nada_configurado_cae_al_de_pydantic(self):
        assert isinstance(
            EventStoreFactory(EventStoreConfig()).create_serializer(),
            PydanticSerializer,
        )

    def test_lo_cachea(self):
        factory = EventStoreFactory(EventStoreConfig())

        assert factory.create_serializer() is factory.create_serializer()


class TestElContenedor:
    def test_sin_configurar_falla_con_remediacion(self):
        """
        Explicito y no un contenedor por defecto: un almacen en memoria creado en silencio
        parece funcionar y no persiste nada.
        """
        with pytest.raises(RuntimeError, match="configure_event_store"):
            get_event_store_container()

    def test_configure_devuelve_el_contenedor(self):
        contenedor = configure_event_store(config=EventStoreConfig())

        assert get_event_store_container() is contenedor

    def test_cachea_el_almacen_entre_llamadas(self):
        contenedor = configure_event_store(config=EventStoreConfig())

        assert contenedor.store() is contenedor.store()

    def test_cachea_el_proyector(self):
        contenedor = configure_event_store(config=EventStoreConfig())

        assert contenedor.projector() is contenedor.projector()

    def test_el_relay_no_se_cachea(self):
        """
        Toma el bus como argumento: cachearlo ligado a uno concreto haria que pedirlo con
        otro devolviera el primero, publicando en el lugar equivocado sin ningun error.
        """
        from unittest.mock import MagicMock

        contenedor = configure_event_store(config=EventStoreConfig())

        assert contenedor.relay(MagicMock()) is not contenedor.relay(MagicMock())

    def test_lee_la_config_de_server_config(self, monkeypatch: pytest.MonkeyPatch):
        declarada = EventStoreConfig(relay_enabled=True)
        monkeypatch.setattr(
            "hexcore.config.LazyConfig.get_config",
            lambda: ServerConfig(event_store=declarada),
        )

        contenedor = configure_event_store()

        assert contenedor.config is declarada

    def test_sin_config_declarada_usa_los_defaults(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "hexcore.config.LazyConfig.get_config", lambda: ServerConfig()
        )

        contenedor = configure_event_store()

        assert isinstance(contenedor.store(), InMemoryEventStore)

    def test_reset_descarta_el_contenedor(self):
        configure_event_store(config=EventStoreConfig())

        reset_event_store()

        with pytest.raises(RuntimeError):
            get_event_store_container()


class TestLaDoblePublicacion:
    def test_sin_relay_el_uow_publica(self):
        contenedor = configure_event_store(config=EventStoreConfig())

        assert contenedor.publish_after_commit_recomendado is True

    def test_con_relay_el_uow_no_publica(self):
        """
        La regla que evita que cada evento salga dos veces. Existe como propiedad para que no
        dependa de que alguien la recuerde al construir el UoW.
        """
        contenedor = configure_event_store(
            config=EventStoreConfig(relay_enabled=True)
        )

        assert contenedor.publish_after_commit_recomendado is False


class TestLosProviders:
    def test_devuelven_lo_del_contenedor(self):
        from hexcore.infrastructure.api.eventsourcing import (
            provide_checkpoint_store,
            provide_event_store,
            provide_projector,
            provide_snapshot_store,
        )

        contenedor = configure_event_store(config=EventStoreConfig())

        assert provide_event_store() is contenedor.store()
        assert provide_snapshot_store() is None
        assert provide_checkpoint_store() is contenedor.checkpoints()
        assert provide_projector() is contenedor.projector()

    def test_son_funciones_para_poder_sobreescribirlos(self):
        """
        `app.dependency_overrides[provide_event_store] = ...` en los tests. Si el endpoint
        tocara el singleton, no habria nada que sobreescribir.
        """
        from hexcore.infrastructure.api import eventsourcing

        for nombre in (
            "provide_event_store",
            "provide_snapshot_store",
            "provide_checkpoint_store",
            "provide_projector",
        ):
            assert callable(getattr(eventsourcing, nombre))


class TestLaFachada:
    def test_expone_los_adaptadores_sin_exigir_sus_extras_al_importar(self):
        """
        La pereza del PEP 562 es lo que hace que convivan adaptadores con dependencias
        distintas en el mismo modulo.
        """
        import hexcore.eventsourcing as fachada

        assert "SqlAlchemyEventStore" in fachada.__all__
        assert "BeanieEventStore" in fachada.__all__
        assert "RedisEventStore" in fachada.__all__

    def test_resuelve_lo_que_no_necesita_extras(self):
        from hexcore.eventsourcing import AggregateRoot, InMemoryEventStore, when

        assert AggregateRoot is not None
        assert when is not None
        assert InMemoryEventStore is not None

    def test_un_nombre_desconocido_da_attribute_error(self):
        import hexcore.eventsourcing as fachada

        with pytest.raises(AttributeError, match="has no attribute"):
            fachada.NoExiste  # pyright: ignore[reportAttributeAccessIssue]
