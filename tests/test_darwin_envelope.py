"""
Darwin Fase 6: el sobre firmado que lleva el actor a través de una cola.

Los tres grupos, y cada uno fija una decisión de diseño distinta:

1. **El códec.** Round trip, y los cinco rechazos: firma manipulada, payload manipulado,
   sobre vencido, versión desconocida, y —el importante— **grant re-adjuntado a otro
   mensaje**, que es la escalación de privilegios que la atadura `cid`/`mt` existe para
   impedir.
2. **El restaurador.** Revalida la fila de `session` contra la base, así que un sobre
   perfectamente firmado de una sesión revocada se rechaza igual.
3. **El circuito completo**, contra SQLite real: sign-in → despacho de un
   `@background_command` → el payload que quedó en el enqueuer → `CQRSConsumer` → el handler
   ve el mismo actor **y** el mismo subject.

Más la regresión de `IN_WORKER`, que documenta por qué un middleware **no** puede ramificar
sobre `is_worker_execution()`.
"""
from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("aiosqlite")
pytest.importorskip("joserfc")
pytest.importorskip("argon2")

from sqlalchemy.pool import StaticPool  # noqa: E402

from hexcore.cqrs import CQRSConsumer, background_command  # noqa: E402
from hexcore.darwin import (  # noqa: E402
    AuthContext,
    AuthEnvelopeCodec,
    FixedClock,
    IdentityConfig,
    Impersonation,
    Principal,
    StaticKeyStore,
    SystemPrincipal,
    TokenConfig,
    WorkerContextIntegrityError,
    auth_scope,
    configure_identity,
    create_identity_tables,
    current_auth,
    generate_signing_key,
    reset_identity,
    system_context,
)
from hexcore.darwin.infrastructure.envelope import (  # noqa: E402
    ENVELOPE_KEY,
    ENVELOPE_VERSION,
)
from hexcore.domain.cqrs.commands import Command  # noqa: E402
from hexcore.domain.cqrs.context import is_worker_execution  # noqa: E402
from hexcore.domain.cqrs.envelope import (  # noqa: E402
    ENVELOPE_METADATA_KEY,
    clear_envelope_registry,
    registered_envelope_keys,
)
from hexcore.domain.cqrs.handlers import AbstractCommandHandler  # noqa: E402
from hexcore.domain.cqrs.middleware import AbstractMiddleware  # noqa: E402
from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer  # noqa: E402
from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (  # noqa: E402
    dispose_engine,
    init_engine,
)
from hexcore.testing import build_test_buses  # noqa: E402

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)
SQLITE_URL = "sqlite+aiosqlite:///:memory:"
CLAVE = "k" * 48


class TransferirFondos(Command):
    monto: int


@background_command(queue="dinero")
class CobrarFactura(Command):
    factura_id: str


class BorrarCuenta(Command):
    motivo: str = "pedido del usuario"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def reloj() -> FixedClock:
    return FixedClock(AHORA)


@pytest.fixture
def codec(reloj) -> AuthEnvelopeCodec:
    return AuthEnvelopeCodec(secret=CLAVE, clock=reloj, ttl=timedelta(hours=24))


def _contexto(*, scopes: frozenset[str] = frozenset({"dinero.mover"})) -> AuthContext:
    principal = Principal(
        user_id=uuid4(), session_id=uuid4(), email="a@b.c", scopes=scopes
    )
    return AuthContext(actor=principal, subject=principal, transport="cookie")


def _repartir(sellado: str) -> tuple[dict, str]:
    """Abre el sobre sin verificarlo, para poder manipularlo en los tests de integridad."""
    payload, firma = sellado.split(".", 1)
    relleno = "=" * (-len(payload) % 4)
    return json.loads(base64.urlsafe_b64decode(payload + relleno)), firma


def _rearmar(cuerpo: dict, firma: str) -> str:
    """Rearma el sobre con una firma dada, para probar que el MAC lo rechaza."""
    return f"{_codificar(cuerpo)}.{firma}"


def _refirmar(codec: AuthEnvelopeCodec, cuerpo: dict) -> str:
    """
    Rearma el sobre **con firma válida**.

    Para los rechazos que tienen que ocurrir *después* del MAC: sin re-firmar, el test pasaría
    por el motivo equivocado y no probaría el chequeo que dice probar.
    """
    payload = _codificar(cuerpo)
    return f"{payload}.{codec._firmar(payload)}"  # pyright: ignore[reportPrivateUsage]


def _codificar(cuerpo: dict) -> str:
    crudo = json.dumps(cuerpo, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(crudo).rstrip(b"=").decode()


# ── 1. El códec ───────────────────────────────────────────────────────────────
def test_round_trip_conserva_actor_y_subject(codec):
    contexto = _contexto()
    comando = TransferirFondos(monto=100)

    restaurado = codec.open(codec.seal(contexto, comando), comando)

    assert restaurado.actor_id == contexto.actor_id
    assert restaurado.subject_id == contexto.subject_id
    assert restaurado.actor.scopes == frozenset({"dinero.mover"})
    assert restaurado.actor.email == "a@b.c"  # type: ignore[union-attr]


def test_el_transporte_restaurado_es_worker(codec):
    """
    Nunca el original, y no es un descuido.

    Un job de background no está sirviendo un request con cookie, y código que ramifica por
    transporte —el chequeo anti-CSRF, por ejemplo— tiene que poder distinguirlo. El
    transporte original queda en el registro de auditoría del request que encoló.
    """
    comando = TransferirFondos(monto=1)

    restaurado = codec.open(codec.seal(_contexto(), comando), comando)

    assert restaurado.transport == "worker"


def test_el_usuario_extendido_no_viaja(codec):
    """
    `AuthContext.user` es el modelo de la app, de tipo arbitrario y sin garantía de ser
    serializable. Serializarlo "cuando se pueda" daría un campo que existe o no según el
    tipo, que es la clase de contrato que nadie puede programar en contra.
    """
    principal = Principal(user_id=uuid4(), session_id=uuid4())
    contexto = AuthContext(
        actor=principal,
        subject=principal,
        transport="cookie",
        user={"plan": "premium"},
    )
    comando = TransferirFondos(monto=1)

    restaurado = codec.open(codec.seal(contexto, comando), comando)

    assert restaurado.user is None


def test_un_principal_de_sistema_round_trippea_como_sistema(codec):
    """
    `kind` explícito y no inferido: un `SystemPrincipal` sin scopes y un `Principal` sin
    sesión ni email tienen la misma forma, y confundirlos daría un actor de sistema donde
    había un usuario — o al revés, que es peor, porque un `SystemPrincipal` no responde a la
    denylist ni a la revocación por generación.
    """
    cron = SystemPrincipal(name="cierre-nocturno", scopes=frozenset({"register.close"}))
    contexto = AuthContext(actor=cron, subject=cron, transport="internal")
    comando = TransferirFondos(monto=1)

    restaurado = codec.open(codec.seal(contexto, comando), comando)

    assert isinstance(restaurado.actor, SystemPrincipal)
    assert restaurado.is_system
    assert restaurado.actor_id == "cierre-nocturno"
    assert restaurado.has_scope("register.close")


def test_la_impersonacion_round_trippea_completa(codec):
    soporte = Principal(user_id=uuid4(), session_id=uuid4())
    cliente = Principal(user_id=uuid4(), session_id=soporte.session_id)
    supervisor = uuid4()
    contexto = AuthContext(
        actor=soporte,
        subject=cliente,
        transport="cookie",
        impersonation=Impersonation(
            granted_by=supervisor,
            reason="ticket #4821",
            granted_at=AHORA,
            expires_at=AHORA + timedelta(minutes=60),
        ),
    )
    comando = BorrarCuenta()

    restaurado = codec.open(codec.seal(contexto, comando), comando)

    assert restaurado.is_impersonating
    assert restaurado.impersonation is not None
    assert restaurado.impersonation.granted_by == supervisor
    assert restaurado.impersonation.reason == "ticket #4821"
    assert restaurado.actor_id == soporte.user_id
    assert restaurado.subject_id == cliente.user_id


# ── 1b. Los rechazos ──────────────────────────────────────────────────────────
def test_un_grant_readjuntado_a_otro_mensaje_se_rechaza(codec):
    """
    **El ataque que la atadura `mt` existe para impedir.**

    Capturar el sobre de un `BorrarCuenta` legítimo y re-adjuntarlo a un `TransferirFondos`
    da un sobre que verifica —está bien firmado— y sin este chequeo el worker ejecutaría la
    transferencia con la autoridad del grant de borrado. Es escalación de privilegios a un
    `LPUSH` de distancia.
    """
    sellado = codec.seal(_contexto(), BorrarCuenta())

    with pytest.raises(WorkerContextIntegrityError, match="re-adjuntado"):
        codec.open(sellado, TransferirFondos(monto=1_000_000))


def test_un_grant_readjuntado_a_otra_instancia_del_mismo_tipo_se_rechaza(codec):
    """
    La atadura `cid`, que es la mitad que `mt` no cubre: dos `TransferirFondos` distintos son
    del mismo tipo, así que sin el `command_id` el sobre de una transferencia de $10 sirve
    para una de $1.000.000.
    """
    sellado = codec.seal(_contexto(), TransferirFondos(monto=10))

    with pytest.raises(WorkerContextIntegrityError, match="atado al mensaje"):
        codec.open(sellado, TransferirFondos(monto=1_000_000))


def test_una_firma_manipulada_se_rechaza(codec):
    comando = TransferirFondos(monto=1)
    payload, firma = codec.seal(_contexto(), comando).split(".", 1)
    alterada = ("A" if firma[0] != "A" else "B") + firma[1:]

    with pytest.raises(WorkerContextIntegrityError, match="firma"):
        codec.open(f"{payload}.{alterada}", comando)


def test_un_payload_manipulado_se_rechaza(codec):
    """
    Escalar los scopes editando el JSON no funciona: el MAC se calcula sobre el texto, así
    que cualquier cambio lo invalida.
    """
    comando = TransferirFondos(monto=1)
    cuerpo, firma = _repartir(codec.seal(_contexto(), comando))
    cuerpo["actor"]["scopes"] = ["admin.todo"]

    with pytest.raises(WorkerContextIntegrityError, match="firma"):
        codec.open(_rearmar(cuerpo, firma), comando)


def test_swapear_actor_y_subject_se_rechaza(codec):
    """
    Convertir una sesión normal en una impersonación al revés tampoco funciona, y falla en el
    MAC antes de llegar al invariante del contexto.
    """
    comando = BorrarCuenta()
    cuerpo, firma = _repartir(codec.seal(_contexto(), comando))
    cuerpo["actor"], cuerpo["subject"] = cuerpo["subject"], cuerpo["actor"]
    cuerpo["actor"]["id"] = str(uuid4())

    with pytest.raises(WorkerContextIntegrityError):
        codec.open(_rearmar(cuerpo, firma), comando)


def test_un_sobre_firmado_con_otro_secreto_se_rechaza(reloj):
    """El síntoma de un despliegue con la clave desincronizada entre web y worker."""
    comando = TransferirFondos(monto=1)
    emisor = AuthEnvelopeCodec(secret="a" * 48, clock=reloj)
    receptor = AuthEnvelopeCodec(secret="b" * 48, clock=reloj)

    with pytest.raises(WorkerContextIntegrityError, match="secreto de firma"):
        receptor.open(emisor.seal(_contexto(), comando), comando)


def test_un_sobre_vencido_se_rechaza(reloj):
    """
    Un payload rescatado de una dead-letter queue no se puede reproducir un mes después. El
    reloj es un puerto inyectado, así que esto no necesita `freezegun`.
    """
    comando = TransferirFondos(monto=1)
    codec = AuthEnvelopeCodec(secret=CLAVE, clock=reloj, ttl=timedelta(hours=1))
    sellado = codec.seal(_contexto(), comando)

    reloj.set(AHORA + timedelta(hours=1, seconds=1))

    with pytest.raises(WorkerContextIntegrityError, match="venció"):
        codec.open(sellado, comando)


def test_un_sobre_dentro_del_ttl_se_acepta(reloj):
    comando = TransferirFondos(monto=1)
    codec = AuthEnvelopeCodec(secret=CLAVE, clock=reloj, ttl=timedelta(hours=1))
    sellado = codec.seal(_contexto(), comando)

    reloj.set(AHORA + timedelta(minutes=59))

    assert codec.open(sellado, comando) is not None


def test_un_sobre_fechado_en_el_futuro_se_rechaza(reloj):
    comando = TransferirFondos(monto=1)
    futuro = AuthEnvelopeCodec(secret=CLAVE, clock=FixedClock(AHORA + timedelta(hours=1)))
    presente = AuthEnvelopeCodec(secret=CLAVE, clock=reloj)

    with pytest.raises(WorkerContextIntegrityError, match="futuro"):
        presente.open(futuro.seal(_contexto(), comando), comando)


def test_un_desfasaje_de_reloj_chico_se_tolera(reloj):
    """
    La tolerancia existe por desfasaje entre el proceso que encola y el que consume, que en
    un cluster es normal y no es un ataque. Sin ella, un worker con el reloj 5 s atrasado
    rechazaría todo.
    """
    comando = TransferirFondos(monto=1)
    adelantado = AuthEnvelopeCodec(
        secret=CLAVE, clock=FixedClock(AHORA + timedelta(seconds=30))
    )
    presente = AuthEnvelopeCodec(secret=CLAVE, clock=reloj)

    assert presente.open(adelantado.seal(_contexto(), comando), comando) is not None


def test_una_version_desconocida_se_rechaza(codec):
    """
    El sobre tiene TTL, así que cuando el formato cambie va a haber sobres de los dos
    formatos en la cola al mismo tiempo. Sin este campo el síntoma sería una firma que no
    verifica sin causa aparente durante la ventana del deploy.
    """
    comando = TransferirFondos(monto=1)
    cuerpo, _ = _repartir(codec.seal(_contexto(), comando))
    cuerpo["v"] = ENVELOPE_VERSION + 1

    # Se re-firma, para que el rechazo sea por la versión y no por el MAC.
    with pytest.raises(WorkerContextIntegrityError, match="Versión de sobre desconocida"):
        codec.open(_refirmar(codec, cuerpo), comando)


@pytest.mark.parametrize("basura", ["", "sin-punto", "a.b.c", 42, None, {"a": 1}])
def test_un_sobre_con_forma_invalida_se_rechaza(codec, basura):
    with pytest.raises(WorkerContextIntegrityError):
        codec.open(basura, TransferirFondos(monto=1))


def test_un_sobre_impersonado_sin_permiso_no_se_puede_reconstruir(codec):
    """
    El invariante de `AuthContext` vale igual del otro lado de la cola: un sobre cuyo subject
    difiere del actor sin permiso de impersonación no describe un contexto legítimo, y se
    rechaza en vez de reconstruirse.
    """
    comando = BorrarCuenta()
    cuerpo, _ = _repartir(codec.seal(_contexto(), comando))
    cuerpo["subject"]["id"] = str(uuid4())  # subject != actor, y sin `imp`

    with pytest.raises(WorkerContextIntegrityError, match="no describe un contexto válido"):
        codec.open(_refirmar(codec, cuerpo), comando)


# ── 2. El restaurador, contra la base ─────────────────────────────────────────
@pytest.fixture
def contenedor(reloj):
    """Darwin cableado contra SQLite en memoria, con reloj y claves fijos."""
    asyncio.run(dispose_engine())
    motor = init_engine(SQLITE_URL, poolclass=StaticPool)
    asyncio.run(create_identity_tables(motor))

    reset_identity()
    contenedor = configure_identity(
        IdentityConfig(
            storage="sqlalchemy",
            secret_key=CLAVE,
            tokens=TokenConfig(issuer="https://api.test"),
            require_verified_email=False,
        ),
        clock=reloj,
        key_store=StaticKeyStore([generate_signing_key(kid="k1")]),
    )
    yield contenedor

    reset_identity()
    asyncio.run(dispose_engine())


async def _usuario_con_sesion(contenedor):
    """Un usuario dado de alta y logueado. Devuelve `(usuario, sesión, par de tokens)`."""
    identidad = contenedor.identity_service()
    usuario, _ = await identidad.sign_up(email="dueño@test.com", password="una-clave-larga")
    return await identidad.sign_in(email="dueño@test.com", password="una-clave-larga")


@pytest.mark.anyio
async def test_el_restaurador_acepta_una_sesion_viva(contenedor):
    usuario, sesion, _ = await _usuario_con_sesion(contenedor)
    contexto = AuthContext(
        actor=Principal(user_id=usuario.id, session_id=sesion.id),
        subject=Principal(user_id=usuario.id, session_id=sesion.id),
        transport="cookie",
    )
    comando = CobrarFactura(factura_id="f-1")
    sellado = contenedor.envelope_codec().seal(contexto, comando)

    async with contenedor.envelope_restorer().restore(sellado, comando):
        actual = current_auth()
        assert actual is not None
        assert actual.actor_id == usuario.id


@pytest.mark.anyio
async def test_el_restaurador_rechaza_una_sesion_revocada(contenedor):
    """
    **El chequeo que no se puede saltear.** Verificar la firma y el `exp` sólo prueba que el
    sobre es auténtico y reciente, no que la sesión siga viva. Un TTL de 24 h sin esto son
    24 h de ejecución con una credencial revocada.
    """
    usuario, sesion, _ = await _usuario_con_sesion(contenedor)
    contexto = AuthContext(
        actor=Principal(user_id=usuario.id, session_id=sesion.id),
        subject=Principal(user_id=usuario.id, session_id=sesion.id),
        transport="cookie",
    )
    comando = CobrarFactura(factura_id="f-1")
    sellado = contenedor.envelope_codec().seal(contexto, comando)

    await contenedor.session_service().revoke(sesion.id, reason="logout")

    with pytest.raises(WorkerContextIntegrityError, match="ya no está viva"):
        async with contenedor.envelope_restorer().restore(sellado, comando):
            pass  # pragma: no cover


@pytest.mark.anyio
async def test_el_restaurador_rechaza_una_sesion_inexistente(contenedor):
    # El mismo principal en los dos lados: un actor distinto del subject sin permiso de
    # impersonación no se puede ni construir, y acá lo que se prueba es otra cosa.
    principal = Principal(user_id=uuid4(), session_id=uuid4())
    contexto = AuthContext(actor=principal, subject=principal, transport="cookie")
    comando = CobrarFactura(factura_id="f-1")
    sellado = contenedor.envelope_codec().seal(contexto, comando)

    with pytest.raises(WorkerContextIntegrityError, match="no existe"):
        async with contenedor.envelope_restorer().restore(sellado, comando):
            pass  # pragma: no cover


@pytest.mark.anyio
async def test_un_principal_de_sistema_no_se_revalida(contenedor):
    """
    No tiene sesión revocable: su autoridad es el cableado del proceso. Exigirle una fila
    haría imposible que un cron encolara nada.
    """
    cron = SystemPrincipal(name="cierre", scopes=frozenset({"register.close"}))
    contexto = AuthContext(actor=cron, subject=cron, transport="internal")
    comando = CobrarFactura(factura_id="f-1")
    sellado = contenedor.envelope_codec().seal(contexto, comando)

    async with contenedor.envelope_restorer().restore(sellado, comando):
        actual = current_auth()
        assert actual is not None
        assert actual.is_system


@pytest.mark.anyio
async def test_el_restaurador_rechaza_una_sesion_ya_rotada(contenedor):
    """
    `consumed_at` cuenta como no viva, y acá no es un error sino lo esperable: toda rotación
    de refresh consume la fila anterior. Se rechaza igual porque esa sesión ya no es la
    vigente; quien necesite sobrevivir a una rotación lleva el dato en el propio comando.
    """
    usuario, sesion, par = await _usuario_con_sesion(contenedor)
    contexto = AuthContext(
        actor=Principal(user_id=usuario.id, session_id=sesion.id),
        subject=Principal(user_id=usuario.id, session_id=sesion.id),
        transport="cookie",
    )
    comando = CobrarFactura(factura_id="f-1")
    sellado = contenedor.envelope_codec().seal(contexto, comando)

    await contenedor.session_service().refresh(par.refresh_token, transport="cookie")

    with pytest.raises(WorkerContextIntegrityError, match="ya no está viva"):
        async with contenedor.envelope_restorer().restore(sellado, comando):
            pass  # pragma: no cover


# ── 3. El cableado y el circuito completo ─────────────────────────────────────
def test_configure_identity_registra_el_sobre(contenedor):
    assert ENVELOPE_KEY in registered_envelope_keys()


def test_reset_identity_deregistra_el_sobre(contenedor):
    """
    Deregistra porque el registro es estado global del **núcleo**, no del contenedor: dejarlo
    puesto haría que un test posterior sellara contra un contenedor que ya no existe, y el
    error saldría en el encolado de otro test.
    """
    reset_identity()

    assert ENVELOPE_KEY not in registered_envelope_keys()


@pytest.mark.anyio
async def test_sin_contexto_ambiental_no_hay_sobre(contenedor):
    """
    Encolar sin estar autenticado es legítimo —un cron, un seed, la CLI— y exigir contexto
    rompería todo el uso de background que no tiene nada que ver con identidad.
    """
    buses = build_test_buses()

    await buses.command_bus.dispatch(CobrarFactura(factura_id="f-1"))

    payload = buses.enqueuer.recorded[0].payload
    assert ENVELOPE_METADATA_KEY not in payload


@pytest.mark.anyio
async def test_el_circuito_completo_conserva_actor_y_subject(contenedor):
    """
    El test que justifica la fase entera: el actor cruza la cola.

    Sign-in real, `@background_command` real, el payload que quedó en el enqueuer, y el
    `CQRSConsumer` del otro lado. El handler ve el mismo actor **y** el mismo subject que el
    request que lo encoló.
    """
    usuario, sesion, _ = await _usuario_con_sesion(contenedor)
    vistos: list[tuple] = []

    class CobrarFacturaHandler(AbstractCommandHandler[CobrarFactura, None]):
        async def handle(self, command: CobrarFactura) -> None:
            contexto = current_auth()
            assert contexto is not None
            vistos.append((contexto.actor_id, contexto.subject_id, contexto.transport))

    buses = build_test_buses()
    buses.registry.register_command_handler(CobrarFactura, CobrarFacturaHandler())

    contexto = AuthContext(
        actor=Principal(
            user_id=usuario.id, session_id=sesion.id, scopes=frozenset({"facturas.cobrar"})
        ),
        subject=Principal(user_id=usuario.id, session_id=sesion.id),
        transport="bearer",
    )

    # Lado web: hay contexto ambiental, así que el sobre se sella.
    with auth_scope(contexto):
        await buses.command_bus.dispatch(CobrarFactura(factura_id="f-42"))

    payload = buses.enqueuer.recorded[0].payload
    assert ENVELOPE_KEY in payload[ENVELOPE_METADATA_KEY]
    assert vistos == []  # todavía no corrió: está encolado

    # Lado worker: ningún contexto ambiental propio.
    assert current_auth() is None
    consumer = CQRSConsumer(buses.command_bus, buses.event_bus)
    await consumer.process_command(payload)

    assert vistos == [(usuario.id, usuario.id, "worker")]
    assert current_auth() is None  # y no se filtró al salir


@pytest.mark.anyio
async def test_el_circuito_completo_conserva_la_impersonacion(contenedor):
    """
    Lo que hace auditable el plugin de impersonate: el worker sabe que el actor no es el
    subject, y por qué.
    """
    identidad = contenedor.identity_service()
    soporte, _ = await identidad.sign_up(email="soporte@test.com", password="clave-larga-1")
    _, sesion_soporte, _ = await identidad.sign_in(
        email="soporte@test.com", password="clave-larga-1"
    )
    cliente, _ = await identidad.sign_up(email="cliente@test.com", password="clave-larga-2")

    vistos: list[tuple] = []

    class Handler(AbstractCommandHandler[CobrarFactura, None]):
        async def handle(self, command: CobrarFactura) -> None:
            contexto = current_auth()
            assert contexto is not None
            vistos.append(
                (
                    contexto.actor_id,
                    contexto.subject_id,
                    contexto.is_impersonating,
                    contexto.impersonation.reason if contexto.impersonation else None,
                )
            )

    buses = build_test_buses()
    buses.registry.register_command_handler(CobrarFactura, Handler())

    contexto = AuthContext(
        actor=Principal(user_id=soporte.id, session_id=sesion_soporte.id),
        subject=Principal(user_id=cliente.id, session_id=sesion_soporte.id),
        transport="cookie",
        impersonation=Impersonation(
            granted_by=uuid4(),
            reason="ticket #4821",
            granted_at=AHORA,
            expires_at=AHORA + timedelta(minutes=60),
        ),
    )

    with auth_scope(contexto):
        await buses.command_bus.dispatch(CobrarFactura(factura_id="f-1"))

    await CQRSConsumer(buses.command_bus).process_command(
        buses.enqueuer.recorded[0].payload
    )

    assert vistos == [(soporte.id, cliente.id, True, "ticket #4821")]


@pytest.mark.anyio
async def test_un_sobre_readjuntado_en_la_cola_no_se_ejecuta(contenedor):
    """
    El ataque completo, extremo a extremo: capturar el `__meta__` de un mensaje legítimo y
    pegarlo en otro. El handler no corre.
    """
    usuario, sesion, _ = await _usuario_con_sesion(contenedor)
    corridas: list[str] = []

    class Handler(AbstractCommandHandler[CobrarFactura, None]):
        async def handle(self, command: CobrarFactura) -> None:
            corridas.append(command.factura_id)  # pragma: no cover

    buses = build_test_buses()
    buses.registry.register_command_handler(CobrarFactura, Handler())
    contexto = AuthContext(
        actor=Principal(user_id=usuario.id, session_id=sesion.id),
        subject=Principal(user_id=usuario.id, session_id=sesion.id),
        transport="cookie",
    )

    with auth_scope(contexto):
        await buses.command_bus.dispatch(CobrarFactura(factura_id="chico"))

    legitimo = buses.enqueuer.recorded[0].payload
    serializer = PydanticSerializer()
    forjado = serializer.serialize(CobrarFactura(factura_id="enorme"))
    forjado[ENVELOPE_METADATA_KEY] = legitimo[ENVELOPE_METADATA_KEY]

    with pytest.raises(WorkerContextIntegrityError):
        await CQRSConsumer(buses.command_bus).process_command(forjado)

    assert corridas == []


@pytest.mark.anyio
async def test_un_worker_sin_darwin_no_ejecuta_un_mensaje_con_sobre(contenedor):
    """
    El productor selló un contexto que este proceso no puede verificar. Ejecutar sería correr
    el handler sin la autoridad que el mensaje traía; se falla con la línea de cableado que
    falta.
    """
    usuario, sesion, _ = await _usuario_con_sesion(contenedor)
    buses = build_test_buses()
    contexto = AuthContext(
        actor=Principal(user_id=usuario.id, session_id=sesion.id),
        subject=Principal(user_id=usuario.id, session_id=sesion.id),
        transport="cookie",
    )
    with auth_scope(contexto):
        await buses.command_bus.dispatch(CobrarFactura(factura_id="f-1"))
    payload = buses.enqueuer.recorded[0].payload

    # El worker no cableó Darwin.
    clear_envelope_registry()

    with pytest.raises(RuntimeError, match="configure_identity"):
        await CQRSConsumer(buses.command_bus).process_command(payload)


@pytest.mark.anyio
async def test_un_cron_bajo_system_context_sella_su_principal(contenedor):
    vistos: list[str] = []

    class Handler(AbstractCommandHandler[CobrarFactura, None]):
        async def handle(self, command: CobrarFactura) -> None:
            contexto = current_auth()
            assert contexto is not None
            vistos.append(str(contexto.actor_id))

    buses = build_test_buses()
    buses.registry.register_command_handler(CobrarFactura, Handler())

    with system_context("cierre-nocturno", scopes={"facturas.cobrar"}):
        await buses.command_bus.dispatch(CobrarFactura(factura_id="f-1"))

    await CQRSConsumer(buses.command_bus).process_command(
        buses.enqueuer.recorded[0].payload
    )

    assert vistos == ["cierre-nocturno"]


# ── La trampa de IN_WORKER ────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_un_middleware_no_puede_ramificar_sobre_is_worker_execution(contenedor):
    """
    **Trampa verificada, fijada acá para que nadie la reintroduzca.**

    `InMemoryCommandBus.dispatch` envuelve el pipeline en `local_execution()`, que pone
    `IN_WORKER=False` **antes** de que corra cualquier middleware. Dentro de `handle()` el
    flag es siempre `False`, incluso despachando desde el consumer.

    La regla correcta es **"¿hay contexto ambiental?"**, que además es independiente del
    orden de los middlewares y funciona igual en los cinco buses.
    """
    observado: list[tuple[bool, bool]] = []

    class Observador(AbstractMiddleware):
        async def handle(self, message, next_handler):  # type: ignore[no-untyped-def]
            observado.append((is_worker_execution(), current_auth() is not None))
            return await next_handler(message)

    usuario, sesion, _ = await _usuario_con_sesion(contenedor)

    class Handler(AbstractCommandHandler[CobrarFactura, None]):
        async def handle(self, command: CobrarFactura) -> None:
            pass

    from hexcore.application.cqrs.in_memory_buses import InMemoryCommandBus
    from hexcore.application.cqrs.pipeline import MiddlewarePipeline

    buses = build_test_buses()
    buses.registry.register_command_handler(CobrarFactura, Handler())
    contexto = AuthContext(
        actor=Principal(user_id=usuario.id, session_id=sesion.id),
        subject=Principal(user_id=usuario.id, session_id=sesion.id),
        transport="cookie",
    )
    with auth_scope(contexto):
        await buses.command_bus.dispatch(CobrarFactura(factura_id="f-1"))

    bus_worker = InMemoryCommandBus(
        registry=buses.registry,
        pipeline=MiddlewarePipeline([Observador()]),
        enqueuer=buses.enqueuer,
        serializer=PydanticSerializer(),
    )
    await CQRSConsumer(bus_worker).process_command(buses.enqueuer.recorded[0].payload)

    assert observado == [(False, True)], (
        "el flag de worker ya está consumido cuando corre el middleware; lo que sí está "
        "disponible es el contexto ambiental"
    )
