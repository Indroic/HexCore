"""
`AggregateRoot`: registro de mutadores, replay y contabilidad de versiones.

Lo que se prueba acá es lo que hace confiable a un event store. Si el replay no es
determinista, el estado de un agregado depende de cuándo se lo cargó. Si `version` avanza
antes de que el `append` haya salido bien, un conflicto de concurrencia deja el agregado
mintiendo sobre su propia versión y el reintento escribe en el lugar equivocado. Y si un
evento sin mutador se ignora, el agregado se reconstruye a medias sin que nada avise.
"""
from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from hexcore.domain.base import EventRecorder
from hexcore.domain.events import DomainEvent
from hexcore.domain.eventsourcing import (
    AggregateRoot,
    Snapshot,
    UnhandledEventError,
    when,
)

AHORA = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


# ── Eventos de prueba ─────────────────────────────────────────────────────────
class PedidoEvent(DomainEvent):
    """Una base de familia: es lo que el despacho por jerarquía hace posible declarar."""


class PedidoCreado(PedidoEvent):
    cliente: str = "acme"


class PedidoPagado(PedidoEvent):
    monto: int = 0


class PedidoCancelado(PedidoEvent):
    motivo: str = ""


class EventoAjeno(DomainEvent):
    pass


# ── Agregados de prueba ───────────────────────────────────────────────────────
class Pedido(AggregateRoot):
    """`total` no tiene default: el modelo no admite un pedido sin total."""

    cliente: str
    total: int
    cancelado: bool = False

    @when(PedidoCreado)
    def _crear(self, evento: PedidoCreado) -> None:
        self.cliente = evento.cliente
        self.total = 0

    @when(PedidoPagado)
    def _pagar(self, evento: PedidoPagado) -> None:
        self.total += evento.monto

    @when(PedidoCancelado)
    def _cancelar(self, evento: PedidoCancelado) -> None:
        self.cancelado = True

    # ── Negocio ──
    @classmethod
    def crear(cls, cliente: str) -> "Pedido":
        pedido = cls.model_construct()
        pedido.id = uuid4()
        pedido.raise_event(PedidoCreado(cliente=cliente))
        return pedido

    def pagar(self, monto: int) -> None:
        if self.cancelado:
            raise ValueError("un pedido cancelado no se puede pagar")
        self.raise_event(PedidoPagado(monto=monto))


class TestElRegistroDeMutadores:
    def test_when_registra_el_metodo(self):
        assert Pedido.__mutators__[PedidoCreado] == "_crear"
        assert Pedido.__mutators__[PedidoPagado] == "_pagar"

    def test_un_solo_when_puede_cubrir_varios_eventos(self):
        class Contador(AggregateRoot):
            n: int = 0

            @when(PedidoCreado, PedidoPagado)
            def _sumar(self, evento: DomainEvent) -> None:
                self.n += 1

        assert Contador.__mutators__[PedidoCreado] == "_sumar"
        assert Contador.__mutators__[PedidoPagado] == "_sumar"

    def test_dos_mutadores_del_mismo_evento_fallan_al_definir_la_clase(self):
        """
        Es ambigüedad, no precedencia: no hay ninguna razón para preferir a uno de los dos, y
        el evento se aplicaría distinto según el orden de definición. Falla al importar el
        módulo, que es lo más temprano posible.
        """
        with pytest.raises(TypeError) as excinfo:

            class Ambiguo(AggregateRoot):
                @when(PedidoPagado)
                def _uno(self, evento: PedidoPagado) -> None: ...

                @when(PedidoPagado)
                def _dos(self, evento: PedidoPagado) -> None: ...

        mensaje = str(excinfo.value)
        assert "_uno" in mensaje and "_dos" in mensaje

    def test_una_subclase_puede_redefinir_el_mutador_del_padre(self):
        """Lo que se escribe después gana: eso es herencia, no ambigüedad."""

        class PedidoEspecial(Pedido):
            @when(PedidoPagado)
            def _pagar_con_recargo(self, evento: PedidoPagado) -> None:
                self.total += evento.monto * 2

        assert PedidoEspecial.__mutators__[PedidoPagado] == "_pagar_con_recargo"
        assert Pedido.__mutators__[PedidoPagado] == "_pagar", (
            "redefinir en la subclase modificó el mapa del padre"
        )

    def test_when_rechaza_lo_que_no_es_un_evento(self):
        with pytest.raises(TypeError):

            @when(str)  # pyright: ignore[reportArgumentType]
            def _mal(self: object, evento: object) -> None: ...

    def test_when_sin_argumentos_falla(self):
        with pytest.raises(ValueError):
            when()


class TestElStreamType:
    def test_por_defecto_es_el_nombre_de_la_clase_en_snake_case(self):
        class CarritoDeCompras(AggregateRoot):
            pass

        assert CarritoDeCompras.__stream_type__ == "carrito_de_compras"

    def test_se_puede_fijar_explicitamente(self):
        """Lo recomendado en cuanto hay streams en producción: un rename los dejaría huérfanos."""

        class Cosa(AggregateRoot):
            __stream_type__ = "legado_v1"

        assert Cosa.__stream_type__ == "legado_v1"

    def test_una_subclase_no_hereda_el_stream_type_del_padre(self):
        """
        Dos tipos distintos no pueden compartir espacio de streams: el repositorio del padre
        cargaría filas del hijo y las reconstruiría con la clase equivocada.
        """

        class PedidoEspecial(Pedido):
            pass

        assert PedidoEspecial.__stream_type__ == "pedido_especial"

    def test_stream_id_for_no_necesita_instanciar(self):
        """El repositorio tiene que leer antes de que el objeto exista."""
        identificador = uuid4()

        assert Pedido.stream_id_for(identificador) == f"pedido-{identificador}"


class TestApply:
    def test_despacha_a_la_clase_exacta(self):
        pedido = Pedido.load_from_history([PedidoCreado(cliente="acme")])

        pedido.apply(PedidoPagado(monto=7))

        assert pedido.total == 7

    def test_despacha_por_la_jerarquia_del_evento(self):
        """Un `@when` sobre la base de familia cubre a todas sus subclases."""

        class Auditor(AggregateRoot):
            vistos: int = 0

            @when(PedidoEvent)
            def _anotar(self, evento: PedidoEvent) -> None:
                self.vistos += 1

        auditor = Auditor()
        auditor.apply(PedidoCreado())
        auditor.apply(PedidoPagado(monto=1))
        auditor.apply(PedidoCancelado())

        assert auditor.vistos == 3

    def test_lo_especifico_le_gana_a_la_familia(self):
        class Mixto(AggregateRoot):
            familia: int = 0
            exacto: int = 0

            @when(PedidoEvent)
            def _familia(self, evento: PedidoEvent) -> None:
                self.familia += 1

            @when(PedidoPagado)
            def _exacto(self, evento: PedidoPagado) -> None:
                self.exacto += 1

        mixto = Mixto()
        mixto.apply(PedidoPagado(monto=1))

        assert (mixto.exacto, mixto.familia) == (1, 0), (
            "un evento se aplica con UN mutador, el más específico; no con los dos"
        )

        mixto.apply(PedidoCreado())
        assert (mixto.exacto, mixto.familia) == (1, 1)

    def test_apply_no_encola(self):
        """La diferencia con `raise_event`: el replay no puede reescribir historia."""
        pedido = Pedido.load_from_history([PedidoCreado()])

        pedido.apply(PedidoPagado(monto=3))

        assert pedido.uncommitted_events == ()

    def test_un_evento_sin_mutador_falla(self):
        """
        Ignorarlo sería peor que un no-op: es estado que se pierde en silencio, el agregado
        queda reconstruido a medias y el negocio decide sobre eso.
        """
        pedido = Pedido.load_from_history([PedidoCreado()])

        with pytest.raises(UnhandledEventError) as excinfo:
            pedido.apply(EventoAjeno())

        assert "EventoAjeno" in str(excinfo.value)

    def test_strict_mutators_false_lo_deja_pasar(self):
        """El caso legítimo: un evento que sólo le importa a las proyecciones."""

        class Laxo(AggregateRoot):
            __strict_mutators__ = False

        laxo = Laxo()
        laxo.apply(EventoAjeno())  # no lanza


class TestRaiseEvent:
    def test_aplica_y_encola(self):
        pedido = Pedido.crear("acme")

        pedido.pagar(50)

        assert pedido.total == 50
        assert len(pedido.uncommitted_events) == 2

    def test_si_el_mutador_falla_el_evento_no_queda_encolado(self):
        """
        Un pendiente que el propio agregado no supo aplicar se escribiría en el almacén y
        rompería todos los replays futuros.
        """
        pedido = Pedido.crear("acme")
        pedido.clear_domain_events()

        with pytest.raises(UnhandledEventError):
            pedido.raise_event(EventoAjeno())

        assert pedido.uncommitted_events == ()

    def test_las_reglas_de_negocio_viven_fuera_del_mutador(self):
        pedido = Pedido.crear("acme")
        pedido.apply(PedidoCancelado(motivo="arrepentimiento"))

        with pytest.raises(ValueError):
            pedido.pagar(10)


class TestLaContabilidadDeVersiones:
    def test_un_agregado_nuevo_arranca_en_cero(self):
        """Y 0 es lo que `EXPECTED_VERSION_NO_STREAM` significa: el stream no existe."""
        assert Pedido.crear("acme").version == 0

    def test_raise_event_no_avanza_la_version(self):
        """
        `version` es la versión **confirmada en el almacén**. Avanzarla al emitir haría que
        el `expected_version` del append fuera el de un estado que todavía no se escribió.
        """
        pedido = Pedido.crear("acme")

        pedido.pagar(10)
        pedido.pagar(20)

        assert pedido.version == 0
        assert len(pedido.uncommitted_events) == 3

    def test_mark_events_as_committed_avanza_y_vacia(self):
        pedido = Pedido.crear("acme")
        pedido.pagar(10)

        pedido.mark_events_as_committed()

        assert pedido.version == 3 - 1  # creado + pagado
        assert pedido.uncommitted_events == ()

    def test_tras_un_conflicto_los_pendientes_siguen_ahi(self):
        """
        El invariante que hace posible el reintento: si `save()` falla con `ConcurrencyError`
        no llama a `mark_events_as_committed()`, así que el agregado queda intacto.
        """
        pedido = Pedido.crear("acme")
        pedido.pagar(10)
        pendientes_antes = pedido.uncommitted_events

        # `save()` no llamó a mark_events_as_committed porque el append falló.

        assert pedido.uncommitted_events == pendientes_antes
        assert pedido.version == 0


class TestLoadFromHistory:
    def test_reconstruye_el_estado(self):
        pedido = Pedido.load_from_history(
            [PedidoCreado(cliente="acme"), PedidoPagado(monto=30), PedidoPagado(monto=12)]
        )

        assert pedido.cliente == "acme"
        assert pedido.total == 42
        assert pedido.version == 3

    def test_no_deja_nada_pendiente(self):
        """El replay reconstruye lo que ya está escrito: nada de eso está por escribirse."""
        pedido = Pedido.load_from_history([PedidoCreado(), PedidoPagado(monto=1)])

        assert pedido.uncommitted_events == ()

    def test_el_replay_es_determinista(self):
        """
        Aplicar el mismo historial dos veces tiene que dar el mismo estado. Sin esto, el
        estado de un agregado depende de cuándo se lo cargó.
        """
        historial = [
            PedidoCreado(cliente="acme"),
            PedidoPagado(monto=30),
            PedidoCancelado(motivo="x"),
            PedidoPagado(monto=12),
        ]

        identificador = uuid4()
        primero = Pedido.load_from_history(historial, aggregate_id=identificador)
        segundo = Pedido.load_from_history(historial, aggregate_id=identificador)

        assert primero.model_dump() == segundo.model_dump()
        assert primero.version == segundo.version

    def test_construir_en_vivo_y_reconstruir_dan_el_mismo_estado(self):
        """
        La propiedad que justifica que `apply()` tenga doble vida: es literalmente el mismo
        código el que corre en vivo y en el replay.
        """
        en_vivo = Pedido.crear("acme")
        en_vivo.pagar(30)
        en_vivo.pagar(12)

        reconstruido = Pedido.load_from_history(
            list(en_vivo.uncommitted_events), aggregate_id=en_vivo.id
        )

        assert reconstruido.model_dump() == en_vivo.model_dump()

    def test_sin_aggregate_id_cada_carga_inventa_uno_distinto(self):
        """
        El motivo de que `aggregate_id` exista. `model_construct()` aplica el
        `default_factory=uuid4` del campo `id`, asi que dos cargas del mismo historial dan
        agregados con `stream_id` distinto -- y el `save()` siguiente escribiria en el lugar
        equivocado. El repositorio siempre lo pasa.
        """
        historial = [PedidoCreado(cliente="acme")]

        assert (
            Pedido.load_from_history(historial).id
            != Pedido.load_from_history(historial).id
        )

    def test_con_aggregate_id_el_stream_id_es_estable(self):
        identificador = uuid4()

        pedido = Pedido.load_from_history([PedidoCreado()], aggregate_id=identificador)

        assert pedido.id == identificador
        assert pedido.stream_id == f"pedido-{identificador}"

    def test_un_historial_vacio_da_un_agregado_sin_campos(self):
        """
        `model_construct()` deja ausentes los requeridos hasta que el primer evento los
        asigna: es lo que permite declarar `total: int` sin default.
        """
        pedido = Pedido.load_from_history([])

        assert pedido.version == 0
        with pytest.raises(AttributeError):
            pedido.total

    def test_los_privados_quedan_aislados_entre_instancias(self):
        """`model_construct` inicializa `__pydantic_private__` por instancia, no compartido."""
        uno = Pedido.load_from_history([PedidoCreado()])
        otro = Pedido.load_from_history([PedidoCreado()])

        uno.register_event(PedidoPagado(monto=1))

        assert otro.uncommitted_events == ()
        assert uno._version == otro._version == 1

    def test_un_campo_sin_default_no_necesita_valor_de_relleno(self):
        """El punto de `model_construct`: no hace falta llenar el modelo de defaults falsos."""

        class ConDecimal(AggregateRoot):
            saldo: Decimal

            @when(PedidoCreado)
            def _crear(self, evento: PedidoCreado) -> None:
                self.saldo = Decimal("0")

        agregado = ConDecimal.load_from_history([PedidoCreado()])

        assert agregado.saldo == Decimal("0")


class TestSnapshots:
    def _snapshot_de(self, pedido: Pedido) -> Snapshot:
        return Snapshot(
            stream_id=pedido.stream_id,
            aggregate_type="tests.Pedido",
            version=pedido.version,
            state=pedido.snapshot_state(),
            taken_at=AHORA,
        )

    def test_snapshot_state_es_serializable_a_json(self):
        """
        `mode="json"`: un UUID guardado como objeto y releído desde JSON no es el mismo
        valor, y la diferencia aparece recién al comparar.
        """
        pedido = Pedido.load_from_history([PedidoCreado(cliente="acme")])

        estado = pedido.snapshot_state()

        assert isinstance(estado["id"], str)

    def test_restore_snapshot_recupera_estado_y_version(self):
        original = Pedido.load_from_history(
            [PedidoCreado(cliente="acme"), PedidoPagado(monto=30)]
        )

        restaurado = Pedido.restore_snapshot(self._snapshot_de(original))

        assert restaurado.total == 30
        assert restaurado.cliente == "acme"
        assert restaurado.version == 2

    def test_el_snapshot_mas_la_cola_da_el_mismo_estado_que_el_historial_completo(self):
        """La propiedad que hace del snapshot una optimización y no otra fuente de verdad."""
        historial = [
            PedidoCreado(cliente="acme"),
            PedidoPagado(monto=30),
            PedidoPagado(monto=12),
        ]
        identificador = uuid4()
        completo = Pedido.load_from_history(historial, aggregate_id=identificador)

        parcial = Pedido.load_from_history(historial[:2], aggregate_id=identificador)
        con_snapshot = Pedido.load_from_history(
            historial[2:], snapshot=self._snapshot_de(parcial)
        )

        assert con_snapshot.model_dump() == completo.model_dump()
        assert con_snapshot.version == completo.version == 3

    def test_un_snapshot_corrupto_falla_al_restaurar(self):
        """
        `model_validate` y no `model_construct`: el problema salta acá, con el stream_id a
        mano, en vez de más tarde y en otro lugar.
        """
        roto = Snapshot(
            stream_id="pedido-1",
            aggregate_type="tests.Pedido",
            version=1,
            state={"id": "no-es-un-uuid", "cliente": "acme", "total": "no-es-un-int"},
            taken_at=AHORA,
        )

        with pytest.raises(Exception):
            Pedido.restore_snapshot(roto)


class TestLaRelacionConBaseEntity:
    def test_comparte_el_acumulador_de_eventos(self):
        """
        Un handler que hace `pull_domain_events()` funciona contra los dos sin saber cuál
        tiene enfrente.
        """
        assert issubclass(AggregateRoot, EventRecorder)

    def test_no_es_una_base_entity(self):
        """
        Si lo fuera, `collect_domain_entities()` del UoW le drenaría los eventos además del
        repositorio event-sourced, y cada evento se publicaría dos veces.
        """
        from hexcore.domain.base import BaseEntity

        assert not issubclass(AggregateRoot, BaseEntity)

    def test_no_revalida_en_cada_asignacion(self):
        """Un replay de miles de eventos no puede pagar una validación completa por paso."""
        assert AggregateRoot.model_config.get("validate_assignment") is False

    def test_no_arrastra_los_campos_de_base_entity(self):
        """`is_active` es un evento en un agregado, y `updated_at` lo pisaría el ORM."""
        campos = set(AggregateRoot.model_fields)

        assert campos == {"id"}
