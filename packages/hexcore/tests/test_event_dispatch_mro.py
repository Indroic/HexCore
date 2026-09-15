"""
`handlers_for`: el despacho de eventos recorre la jerarquia, no la clase exacta.

Los buses hacian `self._handlers.get(type(event))`. Un handler suscrito a una clase base
quedaba registrado y nunca se invocaba \u2014 sin error, sin aviso. Este archivo fija la
semantica que lo reemplaza, y vive aparte de los tests de cada bus porque los tres
(in-memory, Redis y Postgres) comparten esta funcion: la regla se prueba una vez.
"""
from __future__ import annotations

import typing as t

import pytest
from pydantic import BaseModel

from hexcore.domain.cqrs.dispatch import handlers_for, matching_types
from hexcore.domain.events import DomainEvent


class EventoDeAuth(DomainEvent):
    """Una base de familia, del estilo que hoy Darwin no puede permitirse declarar."""


class UsuarioIngreso(EventoDeAuth):
    email: str = "a@b.c"


class UsuarioSalio(EventoDeAuth):
    pass


class EventoAjeno(DomainEvent):
    pass


def _registro(**por_tipo: t.Any) -> dict[type, list[t.Any]]:
    """Arma el mapa del bus a partir de nombres de clase, para que los casos se lean."""
    tipos = {
        "auth": EventoDeAuth,
        "ingreso": UsuarioIngreso,
        "salio": UsuarioSalio,
        "ajeno": EventoAjeno,
        "base": DomainEvent,
    }
    return {tipos[clave]: list(valor) for clave, valor in por_tipo.items()}


class TestMatchingTypes:
    def test_va_de_lo_especifico_a_lo_general(self):
        assert matching_types(UsuarioIngreso) == [
            UsuarioIngreso,
            EventoDeAuth,
            DomainEvent,
        ]

    def test_corta_en_domain_event(self):
        """
        `BaseModel` y `object` estan en el MRO de todo evento pero no son puntos de
        suscripcion: un handler registrado ahi recibiria el sistema entero por accidente.
        """
        tipos = matching_types(UsuarioIngreso)
        assert BaseModel not in tipos
        assert object not in tipos

    def test_el_propio_domain_event_se_resuelve_a_si_mismo(self):
        assert matching_types(DomainEvent) == [DomainEvent]


class TestHandlersFor:
    def test_un_handler_en_la_clase_base_recibe_la_subclase(self):
        """El caso que antes fallaba en silencio."""
        handlers = handlers_for(UsuarioIngreso(), _registro(auth=["h"]))
        assert handlers == ["h"], (
            "un handler suscrito a la clase base no recibio la subclase: es exactamente el "
            "defecto que obligo a Darwin a declarar sus catorce eventos sin base comun"
        )

    def test_la_clase_exacta_sigue_recibiendo(self):
        assert handlers_for(UsuarioIngreso(), _registro(ingreso=["h"])) == ["h"]

    def test_domain_event_es_catch_all(self):
        """Lo que necesitan el relay del event store y el Projector."""
        registro = _registro(base=["catch_all"])
        assert handlers_for(UsuarioIngreso(), registro) == ["catch_all"]
        assert handlers_for(EventoAjeno(), registro) == ["catch_all"]

    def test_lo_especifico_corre_antes_que_lo_general(self):
        handlers = handlers_for(
            UsuarioIngreso(),
            _registro(base=["general"], auth=["familia"], ingreso=["exacto"]),
        )
        assert handlers == ["exacto", "familia", "general"], (
            "el orden importa: un catch-all de auditoria espera correr despues del handler "
            "que hace el trabajo, no antes"
        )

    def test_suscrito_a_la_exacta_y_a_la_base_corre_una_sola_vez(self):
        handler = object()
        handlers = handlers_for(
            UsuarioIngreso(), _registro(ingreso=[handler], auth=[handler])
        )
        assert handlers == [handler], (
            "un handler registrado en dos niveles corrio dos veces: uno no idempotente "
            "haria dano real"
        )

    def test_deduplica_por_identidad_y_no_por_igualdad(self):
        """
        Dos suscripciones distintas pueden compararse iguales (un `functools.partial`, un
        metodo ligado, un doble de test con `__eq__`). Colapsarlas perderia un handler.
        """

        class HandlerComparable:
            def __eq__(self, otro: object) -> bool:
                return isinstance(otro, HandlerComparable)

            def __hash__(self) -> int:
                return 0

        primero, segundo = HandlerComparable(), HandlerComparable()
        assert primero == segundo

        handlers = handlers_for(
            UsuarioIngreso(), _registro(ingreso=[primero], auth=[segundo])
        )
        assert len(handlers) == 2, "se descarto un handler distinto por comparar igual"

    def test_preserva_el_orden_de_suscripcion_dentro_de_un_tipo(self):
        handlers = handlers_for(UsuarioIngreso(), _registro(ingreso=["a", "b", "c"]))
        assert handlers == ["a", "b", "c"]

    def test_una_familia_hermana_no_recibe(self):
        assert handlers_for(EventoAjeno(), _registro(auth=["h"])) == []

    def test_sin_handlers_no_falla(self):
        assert handlers_for(UsuarioIngreso(), {}) == []
        assert handlers_for(UsuarioIngreso(), _registro(ajeno=["h"])) == []


# ── Los buses, ahora que despachan por jerarquia ──────────────────────────────
@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class TestElBusEnMemoria:
    @pytest.mark.anyio
    async def test_un_handler_en_la_base_recibe_la_subclase(self):
        """
        El comportamiento que cambia en 9.0. Antes el handler quedaba suscrito y no se
        invocaba nunca -- sin error y sin aviso.
        """
        from hexcore.application.cqrs.in_memory_buses import InMemoryEventBus

        recibidos: list[DomainEvent] = []

        async def handler(evento: DomainEvent) -> None:
            recibidos.append(evento)

        bus = InMemoryEventBus()
        bus.subscribe(EventoDeAuth, handler)

        await bus.publish(UsuarioIngreso())

        assert len(recibidos) == 1

    @pytest.mark.anyio
    async def test_domain_event_es_catch_all(self):
        """Lo que necesita el relay del event store para republicar todo lo que se persistio."""
        from hexcore.application.cqrs.in_memory_buses import InMemoryEventBus

        recibidos: list[DomainEvent] = []

        async def catch_all(evento: DomainEvent) -> None:
            recibidos.append(evento)

        bus = InMemoryEventBus()
        bus.subscribe(DomainEvent, catch_all)

        await bus.publish(UsuarioIngreso())
        await bus.publish(EventoAjeno())

        assert len(recibidos) == 2

    @pytest.mark.anyio
    async def test_suscrito_a_la_exacta_y_a_la_base_corre_una_vez(self):
        from hexcore.application.cqrs.in_memory_buses import InMemoryEventBus

        llamadas: list[int] = []

        async def handler(evento: DomainEvent) -> None:
            llamadas.append(1)

        bus = InMemoryEventBus()
        bus.subscribe(UsuarioIngreso, handler)
        bus.subscribe(EventoDeAuth, handler)

        await bus.publish(UsuarioIngreso())

        assert len(llamadas) == 1

    @pytest.mark.anyio
    async def test_la_clase_exacta_sigue_funcionando(self):
        """El comportamiento de siempre no cambia: el cambio es aditivo."""
        from hexcore.application.cqrs.in_memory_buses import InMemoryEventBus

        recibidos: list[DomainEvent] = []

        async def handler(evento: DomainEvent) -> None:
            recibidos.append(evento)

        bus = InMemoryEventBus()
        bus.subscribe(UsuarioIngreso, handler)

        await bus.publish(UsuarioIngreso())
        await bus.publish(UsuarioSalio())

        assert len(recibidos) == 1


class TestElBusPorDefectoDeLaConfig:
    def test_cada_server_config_estrena_bus(self):
        """
        El default era una instancia evaluada al definir la clase, asi que todo ServerConfig
        del proceso compartia el mismo diccionario de handlers: una suscripcion hecha en un
        test la veia el siguiente, y el orden de ejecucion pasaba a importar.
        """
        from hexcore.config import ServerConfig

        una, otra = ServerConfig(), ServerConfig()

        assert una.event_bus is not otra.event_bus

    def test_es_el_puerto_unificado(self):
        from hexcore.config import ServerConfig
        from hexcore.domain.cqrs.buses import AbstractEventBus

        assert isinstance(ServerConfig().event_bus, AbstractEventBus)

    def test_una_suscripcion_no_se_filtra_a_otra_config(self):
        from hexcore.config import ServerConfig

        async def handler(evento: DomainEvent) -> None: ...

        una = ServerConfig()
        una.event_bus.subscribe(UsuarioIngreso, handler)

        otra = ServerConfig()

        assert otra.event_bus._handlers == {}  # pyright: ignore[reportAttributeAccessIssue]


class TestElNombreDelEvento:
    def test_removesuffix_y_no_replace(self):
        """
        `replace` quitaba todas las apariciones: EventLogCreatedEvent daba "LOGCREATED".
        Afectaba a cualquier evento con "Event" en el medio del nombre.
        """

        class EventLogCreatedEvent(DomainEvent):
            pass

        assert EventLogCreatedEvent().event_name == "EVENTLOGCREATED"

    def test_el_caso_normal_no_cambia(self):
        assert UsuarioIngreso().event_name == "USUARIOINGRESO"

    def test_el_bus_de_rabbitmq_usa_la_misma_regla(self):
        """
        Las dos implementaciones tienen que coincidir. Si divergen, el publisher rutea con
        una clave y el binding del consumidor esta armado con la otra: AMQP entrega a las
        colas que matchean y descarta el resto **sin ningun error**.
        """
        pytest.importorskip("aio_pika")
        import inspect

        from hexcore.infrastructure.cqrs import rabbitmq

        fuente = inspect.getsource(rabbitmq.RabbitMQEventBus.subscribe)

        assert 'removesuffix("Event")' in fuente
        assert 'replace("Event"' not in fuente
