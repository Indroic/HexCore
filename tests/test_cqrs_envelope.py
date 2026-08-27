"""
El sobre de metadata del núcleo, sin Darwin.

Este archivo prueba el mecanismo pelado: el registro de proveedores y restauradores, los dos
métodos concretos del serializer, y la propiedad que hace que todo esto sea aditivo —un
payload sin sobre se deserializa igual—.

Se prueba sin Darwin **a propósito**: si estos tests necesitaran identidad para pasar, el
punto de extensión estaría acoplado al único consumidor que hoy tiene, y el próximo (un
tenant id, un trace id de OpenTelemetry) descubriría el acoplamiento tarde.
"""
from __future__ import annotations

import typing as t
from contextlib import asynccontextmanager
from contextvars import ContextVar

import pytest

from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.envelope import (
    ENVELOPE_METADATA_KEY,
    AbstractEnvelopeRestorer,
    clear_envelope_registry,
    collect_envelope_metadata,
    message_correlation_id,
    register_envelope_metadata_provider,
    register_envelope_restorer,
    registered_envelope_keys,
    restored_envelope_scope,
    unregister_envelope_key,
)
from hexcore.domain.events import DomainEvent
from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer


class Cobrar(Command):
    monto: int


class Cobrado(DomainEvent):
    monto: int


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _registro_limpio():
    """
    Vacía el registro antes y después.

    Antes **y** después: es estado global de proceso, así que un test que deje un proveedor
    puesto hace que el siguiente pase —o falle— por el motivo equivocado, y el que verifica
    "sin proveedores el payload es idéntico" pasaría a depender del orden de ejecución.
    """
    clear_envelope_registry()
    yield
    clear_envelope_registry()


# ── Registro ──────────────────────────────────────────────────────────────────
def test_sin_proveedores_el_sobre_esta_vacio():
    assert collect_envelope_metadata(Cobrar(monto=1)) == {}


def test_un_proveedor_aporta_su_clave():
    register_envelope_metadata_provider("trace", lambda _m: "abc123")

    assert collect_envelope_metadata(Cobrar(monto=1)) == {"trace": "abc123"}


def test_un_proveedor_que_devuelve_none_no_aporta_la_clave():
    """
    `None` es "no hay nada que propagar", no "propagá `None`".

    Es el caso normal: un comando encolado desde un script no tiene contexto ambiental, y
    tener que representar eso con una clave presente y vacía obligaría a cada restaurador a
    distinguir "vino vacío" de "no vino".
    """
    register_envelope_metadata_provider("trace", lambda _m: None)

    assert collect_envelope_metadata(Cobrar(monto=1)) == {}


def test_el_proveedor_recibe_el_mensaje():
    """Lo recibe porque el grant se ata al mensaje: sin el mensaje no hay atadura posible."""
    vistos: list[t.Any] = []
    register_envelope_metadata_provider("x", lambda m: vistos.append(m) or "v")

    comando = Cobrar(monto=7)
    collect_envelope_metadata(comando)

    assert vistos == [comando]


def test_registrar_dos_veces_la_misma_clave_reemplaza():
    """
    Reemplaza en vez de acumular: dos proveedores para la misma clave sólo pueden ser un
    cableado duplicado, y elegir uno en silencio daría un sobre que depende del orden de
    importación.
    """
    register_envelope_metadata_provider("x", lambda _m: "primero")
    register_envelope_metadata_provider("x", lambda _m: "segundo")

    assert collect_envelope_metadata(Cobrar(monto=1)) == {"x": "segundo"}


def test_un_proveedor_que_lanza_rompe_el_encolado():
    """
    Falla cerrando, al revés que `rate_limit`.

    Tragarse el error dejaría un mensaje encolado sin el actor, que después se ejecuta sin
    autoridad y falla en el worker con un error que no señala la causa.
    """

    def explota(_m: t.Any) -> t.Any:
        raise RuntimeError("el contenedor no está configurado")

    register_envelope_metadata_provider("auth", explota)

    with pytest.raises(RuntimeError, match="no está configurado"):
        collect_envelope_metadata(Cobrar(monto=1))


def test_unregister_saca_proveedor_y_restaurador():
    register_envelope_metadata_provider("x", lambda _m: "v")
    register_envelope_restorer("x", _RestauradorNulo())
    assert registered_envelope_keys() == frozenset({"x"})

    unregister_envelope_key("x")

    assert registered_envelope_keys() == frozenset()


def test_unregister_es_idempotente():
    unregister_envelope_key("no-existe")  # no lanza


# ── Los dos métodos concretos del serializer ──────────────────────────────────
def test_serialize_envelope_sin_proveedores_da_el_payload_de_siempre():
    """
    La propiedad que hace que esto sea aditivo y no un cambio rompedor: sin nadie registrado,
    el payload es **idéntico** al que el framework generaba antes de que el sobre existiera.
    """
    serializer = PydanticSerializer()
    comando = Cobrar(monto=5)

    assert serializer.serialize_envelope(comando) == serializer.serialize(comando)
    assert ENVELOPE_METADATA_KEY not in serializer.serialize_envelope(comando)


def test_serialize_envelope_agrega_la_clave():
    register_envelope_metadata_provider("trace", lambda _m: "t1")
    serializer = PydanticSerializer()

    payload = serializer.serialize_envelope(Cobrar(monto=5))

    assert payload[ENVELOPE_METADATA_KEY] == {"trace": "t1"}
    assert payload["__data__"]["monto"] == 5


def test_serialize_envelope_acepta_metadata_explicita():
    """Pasarla explícita saltea el registro: es lo que usan los tests y un productor puntual."""
    register_envelope_metadata_provider("trace", lambda _m: "del-registro")
    serializer = PydanticSerializer()

    payload = serializer.serialize_envelope(Cobrar(monto=5), {"trace": "explicita"})

    assert payload[ENVELOPE_METADATA_KEY] == {"trace": "explicita"}


def test_un_payload_legado_sin_sobre_sigue_deserializando():
    """
    **La razón de que los dos métodos sean concretos y no abstractos.**

    Los mensajes que ya estaban encolados cuando esto se deployó tienen que seguir
    procesándose, y un worker nuevo consumiendo una cola vieja es el caso normal de todo
    deploy, no un caso borde.
    """
    serializer = PydanticSerializer()
    legado = serializer.serialize(Cobrar(monto=9))  # sin `__meta__`

    mensaje, sobre = serializer.deserialize_envelope(legado)

    assert isinstance(mensaje, Cobrar)
    assert mensaje.monto == 9
    assert sobre == {}


def test_deserialize_envelope_devuelve_el_sobre_aparte():
    serializer = PydanticSerializer()
    payload = serializer.serialize_envelope(Cobrar(monto=3), {"trace": "t"})

    mensaje, sobre = serializer.deserialize_envelope(payload)

    assert isinstance(mensaje, Cobrar)
    assert sobre == {"trace": "t"}


def test_deserialize_envelope_le_saca_la_clave_al_serializer():
    """
    La clave se **saca** antes de delegar, en vez de confiar en que `deserialize` la ignore:
    un serializador estricto que valide las claves del payload es legítimo y fallaría.
    """
    recibidos: list[dict[str, t.Any]] = []

    class Espia(PydanticSerializer):
        def deserialize(self, data: dict[str, t.Any]) -> t.Any:
            recibidos.append(data)
            return super().deserialize(data)

    serializer = Espia()
    payload = serializer.serialize_envelope(Cobrar(monto=1), {"trace": "t"})

    serializer.deserialize_envelope(payload)

    assert ENVELOPE_METADATA_KEY not in recibidos[0]


def test_deserialize_envelope_no_muta_el_payload_recibido():
    """
    Importa porque los buses distribuidos **reencolan el mismo dict** después de
    deserializarlo: si `deserialize_envelope` le sacara la clave in situ, el handler de
    background del otro lado recibiría un payload sin sobre y perdería el actor.
    """
    serializer = PydanticSerializer()
    payload = serializer.serialize_envelope(Cobrar(monto=1), {"trace": "t"})

    serializer.deserialize_envelope(payload)

    assert payload[ENVELOPE_METADATA_KEY] == {"trace": "t"}


# ── Restauración ──────────────────────────────────────────────────────────────
VALOR: ContextVar[str | None] = ContextVar("test_envelope_valor", default=None)


class _RestauradorNulo(AbstractEnvelopeRestorer):
    @asynccontextmanager
    async def restore(self, value: t.Any, message: t.Any) -> t.AsyncIterator[None]:
        yield


class _RestauradorDeValor(AbstractEnvelopeRestorer):
    """Publica el valor en un ContextVar, con reset — igual que `auth_scope`."""

    def __init__(self) -> None:
        self.mensajes: list[t.Any] = []

    @asynccontextmanager
    async def restore(self, value: t.Any, message: t.Any) -> t.AsyncIterator[None]:
        self.mensajes.append(message)
        token = VALOR.set(str(value))
        try:
            yield
        finally:
            VALOR.reset(token)


@pytest.mark.anyio
async def test_sin_sobre_el_scope_no_hace_nada():
    async with restored_envelope_scope({}, Cobrar(monto=1)):
        assert VALOR.get() is None

    async with restored_envelope_scope(None, Cobrar(monto=1)):
        assert VALOR.get() is None


@pytest.mark.anyio
async def test_el_restaurador_publica_dentro_del_scope_y_limpia_al_salir():
    register_envelope_restorer("v", _RestauradorDeValor())

    async with restored_envelope_scope({"v": "hola"}, Cobrar(monto=1)):
        assert VALOR.get() == "hola"

    assert VALOR.get() is None


@pytest.mark.anyio
async def test_el_reset_ocurre_aunque_el_cuerpo_lance():
    """
    La razón de que `restore` sea un context manager y no una función que devuelve un valor:
    sin el reset en `finally`, un job que falla le filtra su contexto al siguiente job del
    mismo worker.
    """
    register_envelope_restorer("v", _RestauradorDeValor())

    with pytest.raises(ValueError):
        async with restored_envelope_scope({"v": "hola"}, Cobrar(monto=1)):
            raise ValueError("el handler explotó")

    assert VALOR.get() is None


@pytest.mark.anyio
async def test_el_restaurador_recibe_el_mensaje_deserializado():
    restaurador = _RestauradorDeValor()
    register_envelope_restorer("v", restaurador)
    comando = Cobrar(monto=4)

    async with restored_envelope_scope({"v": "x"}, comando):
        pass

    assert restaurador.mensajes == [comando]


@pytest.mark.anyio
async def test_una_clave_sin_restaurador_lanza_con_remediacion():
    """
    No se ejecuta el mensaje: el productor selló un contexto que este proceso no puede
    verificar, así que el handler correría sin la autoridad que el mensaje traía. El mensaje
    de error nombra la clave y trae la línea de cableado que falta.
    """
    with pytest.raises(RuntimeError) as excinfo:
        async with restored_envelope_scope({"auth": "sellado"}, Cobrar(monto=1)):
            pass  # pragma: no cover

    mensaje = str(excinfo.value)
    assert "'auth'" in mensaje
    assert "configure_identity" in mensaje


@pytest.mark.anyio
async def test_varias_claves_se_restauran_todas():
    register_envelope_restorer("v", _RestauradorDeValor())
    register_envelope_restorer("nulo", _RestauradorNulo())

    async with restored_envelope_scope({"v": "a", "nulo": "b"}, Cobrar(monto=1)):
        assert VALOR.get() == "a"


# ── Identificador del mensaje ─────────────────────────────────────────────────
def test_message_correlation_id_de_un_comando():
    comando = Cobrar(monto=1)

    assert message_correlation_id(comando) == str(comando.command_id)


def test_message_correlation_id_de_un_evento():
    evento = Cobrado(monto=1)

    assert message_correlation_id(evento) == str(evento.event_id)


def test_message_correlation_id_de_algo_que_no_es_mensaje():
    assert message_correlation_id(object()) is None
