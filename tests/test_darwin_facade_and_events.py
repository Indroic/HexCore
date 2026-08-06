"""
Darwin: la fachada, los eventos y las entidades.

Los tests de eventos fijan **dos trampas verificadas** del bus de HexCore que condicionan el
diseño del módulo:

1. `InMemoryEventBus.publish` despacha por **clase exacta** (`self._handlers.get(type(event))`):
   no recorre el MRO, así que suscribirse a una clase base no recibe nada. Por eso Darwin
   emite hojas concretas y **no** shippea un `AuthEvent` base para suscribirse.
2. `DomainEvent.event_name` usa `.replace("Event", "")`, **no** `removesuffix`. O sea que
   "Event" en el medio del nombre se pierde: `EventLogCreatedEvent` sale `"LOGCREATED"`.
   Todos los nombres de Darwin llevan "Event" sólo como sufijo.
"""
from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

import hexcore.darwin as darwin
from hexcore.darwin import (
    CREDENTIAL_PROVIDER,
    IDENTITY_EXCEPTION_STATUS_MAP,
    Account,
    IdentitySession,
    IdentityError,
    SessionRevokedEvent,
    User,
    UserSignedInEvent,
    Verification,
)

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


# ── Fachada ───────────────────────────────────────────────────────────────────
def test_la_fachada_expone_todo_su_exports():
    """Todo lo declarado en `_EXPORTS` tiene que resolver de verdad."""
    for nombre in darwin.__all__:
        assert getattr(darwin, nombre) is not None, nombre


def test_all_coincide_con_exports():
    assert darwin.__all__ == sorted(darwin._EXPORTS)
    assert dir(darwin) == darwin.__all__


def test_un_nombre_inexistente_es_attribute_error():
    """El `__getattr__` de la fachada no debe tragarse los errores de tipeo."""
    with pytest.raises(AttributeError, match="has no attribute"):
        darwin.EstoNoExiste


def test_la_fachada_cachea_lo_resuelto():
    """
    El segundo acceso no vuelve a pasar por `__getattr__`.

    Se reimporta de cero en vez de usar `importlib.reload`: `reload` re-ejecuta el módulo
    **en su namespace existente** sin limpiarlo, así que los nombres ya cacheados por otro
    test seguirían ahí y el test mediría cualquier cosa.
    """
    import sys

    for nombre in [m for m in sys.modules if m.startswith("hexcore.darwin")]:
        del sys.modules[nombre]

    modulo = importlib.import_module("hexcore.darwin")

    assert "Principal" not in modulo.__dict__
    modulo.Principal
    assert "Principal" in modulo.__dict__


def test_importar_la_fachada_no_arrastra_los_submodulos():
    """
    La pereza es el punto: importar `hexcore.darwin` no puede cargar el paquete entero.

    Si cargara todo, `hexcore.darwin.infrastructure` (fases siguientes) traería sqlalchemy y
    joserfc, y el contrato de `tests/test_optional_dependencies.py` se caería.
    """
    import sys

    for nombre in [m for m in sys.modules if m.startswith("hexcore.darwin")]:
        del sys.modules[nombre]

    import hexcore.darwin  # noqa: F401

    cargados = {m for m in sys.modules if m.startswith("hexcore.darwin.domain")}
    assert cargados == set(), f"la fachada cargó submódulos de prepo: {cargados}"


# ── Mapa de excepciones ───────────────────────────────────────────────────────
def test_la_base_no_esta_en_el_mapa():
    """
    Deliberado. Los handlers se registran ordenados por profundidad de MRO, así que mapear
    `IdentityError` a un 4xx haría que una excepción nueva sin mapear se tragara con ese
    código en vez de salir como 500 y notarse en los tests.
    """
    assert IdentityError not in IDENTITY_EXCEPTION_STATUS_MAP


def test_autenticacion_es_401_y_autorizacion_es_403():
    """
    "No sé quién sos" vs "sé quién sos y no te alcanza" — que es exactamente la semántica de
    esos dos códigos.
    """
    from hexcore.darwin import (
        AuthenticationError,
        AuthorizationError,
    )

    for exc, status in IDENTITY_EXCEPTION_STATUS_MAP.items():
        if issubclass(exc, AuthenticationError):
            assert status == 401, exc.__name__
        elif issubclass(exc, AuthorizationError):
            assert status == 403, exc.__name__


def test_el_mapa_no_choca_con_el_de_hexcore():
    """
    Se mergea en `create_app`, así que una colisión silenciosa sobrescribiría un mapeo del
    framework.
    """
    pytest.importorskip("fastapi")
    from hexcore.infrastructure.api.exception_handlers import (
        DEFAULT_EXCEPTION_STATUS_MAP,
    )

    assert set(IDENTITY_EXCEPTION_STATUS_MAP) & set(DEFAULT_EXCEPTION_STATUS_MAP) == set()


def test_no_se_filtra_por_que_fallo_la_autenticacion():
    """
    Un mail inexistente y una contraseña equivocada tienen que dar el mismo mensaje: si
    difirieran, el atacante enumera usuarios registrados sin adivinar una contraseña.
    """
    from hexcore.darwin import InvalidCredentialsError

    assert str(InvalidCredentialsError()) == "Las credenciales son inválidas."


# ── Eventos: las dos trampas del bus ──────────────────────────────────────────
def test_event_name_no_se_mutila_en_ningun_evento():
    """
    Trampa 2. `event_name` hace `.replace("Event", "")`, no `removesuffix`, así que un
    "Event" en el medio del nombre se pierde. Se verifica sobre **todos** los eventos.
    """
    from hexcore.darwin.domain import events as modulo_eventos

    for nombre in modulo_eventos.__all__:
        clase = getattr(modulo_eventos, nombre)
        # Exactamente una aparición de "Event", y al final.
        assert nombre.count("Event") == 1, f"{nombre} tiene 'Event' más de una vez"
        assert nombre.endswith("Event"), nombre

        instancia = _evento_minimo(clase)
        esperado = nombre.removesuffix("Event").upper()
        assert instancia.event_name == esperado, (
            f"{nombre}.event_name es {instancia.event_name!r} y debería ser {esperado!r}"
        )


def test_no_se_exporta_una_clase_base_de_eventos():
    """
    Trampa 1. El bus despacha por clase exacta, así que suscribirse a una base no recibe
    nada. Shippear una base invitaría a suscribirse a ella y a que el handler nunca corra,
    sin ningún error.
    """
    from hexcore.darwin.domain import events as modulo_eventos

    exportados = set(modulo_eventos.__all__)
    assert "AuthEvent" not in exportados
    # El contenedor de campos comunes es privado justamente para que no se use así.
    assert not any(nombre.startswith("_") for nombre in exportados)
    assert "_IdentityEventFields" not in {n for n in darwin.__all__}


def test_todo_evento_lleva_actor_y_sujeto():
    """
    Sin el actor, la acción queda atribuida a la víctima bajo impersonación — que es
    justamente lo que el módulo existe para evitar.
    """
    from hexcore.darwin.domain import events as modulo_eventos

    for nombre in modulo_eventos.__all__:
        campos = getattr(modulo_eventos, nombre).model_fields
        assert "actor_user_id" in campos, nombre
        assert "subject_user_id" in campos, nombre
        assert "impersonated" in campos, nombre


def test_el_bus_en_memoria_entrega_una_hoja_concreta():
    """Prueba de que el patrón elegido funciona con el bus real."""
    import asyncio

    from hexcore.application.cqrs.in_memory_buses import InMemoryEventBus

    bus = InMemoryEventBus()
    recibidos: list[object] = []

    async def handler(evento):
        recibidos.append(evento)

    bus.subscribe(SessionRevokedEvent, handler)
    asyncio.run(
        bus.publish(SessionRevokedEvent(session_id=uuid4(), reason="logout"))
    )

    assert len(recibidos) == 1


def test_los_eventos_son_inmutables():
    evento = UserSignedInEvent(transport="cookie")

    with pytest.raises(Exception):
        evento.transport = "bearer"  # type: ignore[misc]


def _evento_minimo(clase):
    """Instancia un evento llenando sólo lo obligatorio."""
    kwargs = {}
    for nombre, campo in clase.model_fields.items():
        if campo.is_required():
            kwargs[nombre] = _valor_para(nombre, campo)
    return clase(**kwargs)


def _valor_para(nombre: str, campo):
    import typing as t

    anotacion = campo.annotation
    # `Literal[...]`: hay que usar uno de sus valores, no un string cualquiera.
    if t.get_origin(anotacion) is t.Literal:
        return t.get_args(anotacion)[0]

    como_texto = str(anotacion)
    if "UUID" in como_texto:
        return uuid4()
    if "int" in como_texto:
        return 0
    return "x"


# ── Entidades ─────────────────────────────────────────────────────────────────
def test_la_sesion_lleva_dos_principales():
    """
    El desvío más importante frente a Better Auth, que tiene un solo `userId`.

    Con dos ids siempre presentes, toda fila escrita por la sesión es atribuible sin
    ambigüedad. Con un id y un flag opcional, reconstruir quién hizo qué depende de que el
    flag se seteara bien en todos los caminos.
    """
    campos = IdentitySession.model_fields

    assert "actor_user_id" in campos
    assert "subject_user_id" in campos
    assert campos["actor_user_id"].is_required()
    assert campos["subject_user_id"].is_required()
    assert "user_id" not in campos


def test_is_impersonated_sale_de_comparar_los_dos_ids():
    usuario, operador = uuid4(), uuid4()

    normal = _sesion(actor_user_id=usuario, subject_user_id=usuario)
    impersonada = _sesion(actor_user_id=operador, subject_user_id=usuario)

    assert normal.is_impersonated is False
    assert impersonada.is_impersonated is True


@pytest.mark.parametrize(
    "kwargs, viva",
    [
        ({}, True),
        ({"revoked_at": AHORA}, False),
        ({"consumed_at": AHORA}, False),
        ({"expires_at": AHORA - timedelta(seconds=1)}, False),
    ],
)
def test_is_live_at_junta_las_tres_condiciones(kwargs, viva):
    """
    Un solo lugar donde se juntan revocada, consumida y vencida.

    Tener esto disperso es cómo aparece el bug de "el logout no cierra la sesión": algún
    camino chequea dos de las tres.
    """
    sesion = _sesion(**kwargs)

    assert sesion.is_live_at(AHORA) is viva


def test_la_sesion_guarda_el_hash_y_no_el_token():
    """Un dump de la tabla de sesiones no puede ser un set de credenciales utilizables."""
    campos = IdentitySession.model_fields

    assert "token_hash" in campos
    assert "token" not in campos


def test_la_verificacion_guarda_el_hash_y_tiene_techo_de_intentos():
    campos = Verification.model_fields

    assert "value_hash" in campos
    assert "value" not in campos
    assert "attempts" in campos
    assert "purpose" in campos


def test_is_usable_at_pone_techo_a_la_fuerza_bruta():
    """Un OTP de 6 dígitos son 10^6 combinaciones: sin techo se agotan en minutos."""
    base = dict(
        identifier="ana@ejemplo.com",
        value_hash="h",
        purpose="otp",
        expires_at=AHORA + timedelta(minutes=10),
    )

    assert Verification(**base, attempts=4).is_usable_at(AHORA, max_attempts=5) is True
    assert Verification(**base, attempts=5).is_usable_at(AHORA, max_attempts=5) is False
    assert (
        Verification(**base, consumed_at=AHORA).is_usable_at(AHORA) is False
    ), "un token consumido no se puede reusar"


def test_la_contrasena_vive_en_account_no_en_user():
    """
    Diseño de Better Auth y es el correcto: un usuario puede tener cero contraseñas (entra
    sólo con Google) o cambiar de método sin tocar su fila.
    """
    assert "password" not in User.model_fields
    assert "password" in Account.model_fields

    credencial = Account(
        user_id=uuid4(),
        provider_id=CREDENTIAL_PROVIDER,
        account_id="x",
        password="$argon2id$...",
    )
    google = Account(user_id=uuid4(), provider_id="google", account_id="123")

    assert credencial.is_credential is True
    assert google.is_credential is False


def test_el_usuario_tiene_generacion_de_token_para_revocacion_masiva():
    """Permite revocar todas las sesiones con un UPDATE, sin importar cuántas haya."""
    assert User.model_fields["token_generation"].default == 0


def test_locked_until_es_distinto_de_is_active():
    """
    Una cuenta bloqueada existe y vuelve; una desactivada se fue. Mezclarlas hace que
    desbloquear y reactivar sean la misma operación.
    """
    usuario = User(email="ana@ejemplo.com", locked_until=AHORA + timedelta(minutes=15))

    assert usuario.is_locked_at(AHORA) is True
    assert usuario.is_locked_at(AHORA + timedelta(minutes=20)) is False
    assert usuario.is_active is True


def _sesion(**overrides) -> IdentitySession:
    usuario = uuid4()
    base = dict(
        actor_user_id=usuario,
        subject_user_id=usuario,
        token_hash="h" * 64,
        expires_at=AHORA + timedelta(minutes=5),
    )
    base.update(overrides)
    return IdentitySession(**base)
