"""
Darwin: el `AuthContext` y el invariante de impersonación.

El invariante es la razón de existir del módulo: **un contexto impersonado no auditable no se
puede construir**. No es una convención que haya que recordar ni un chequeo que haya que
llamar: es un validador de pydantic, así que el objeto no existe si no se cumple.

Se testea en **los dos sentidos**, porque los dos son defectos de auditoría:

- `subject != actor` sin permiso → no hay registro de quién autorizó ni por qué.
- permiso con `subject == actor` → mete en la auditoría una impersonación que nunca pasó, y
  una auditoría con ruido es una auditoría que nadie lee.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from hexcore.darwin import (
    AuthContext,
    Impersonation,
    ImpersonationNotPermittedError,
    InsufficientScopeError,
    Principal,
    SystemPrincipal,
    UnauthenticatedError,
    auth_scope,
    current_auth,
    require_auth,
    system_context,
)

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def soporte() -> Principal:
    return Principal(
        user_id=uuid4(), email="soporte@empresa.com", scopes=frozenset({"users.view"})
    )


@pytest.fixture
def cliente() -> Principal:
    return Principal(user_id=uuid4(), email="cliente@ejemplo.com")


def _permiso(granted_by=None) -> Impersonation:
    return Impersonation(
        granted_by=granted_by or uuid4(),
        reason="ticket #4821: el cliente no puede cerrar su cuenta",
        granted_at=AHORA,
        expires_at=AHORA + timedelta(minutes=60),
    )


# ── El invariante ─────────────────────────────────────────────────────────────
def test_una_sesion_normal_no_necesita_permiso(soporte: Principal):
    ctx = AuthContext(actor=soporte, subject=soporte, transport="cookie")

    assert ctx.is_impersonating is False
    assert ctx.actor_id == ctx.subject_id


def test_impersonar_sin_permiso_no_se_puede_construir(
    soporte: Principal, cliente: Principal
):
    """El caso que el invariante existe para impedir."""
    with pytest.raises(ValidationError) as excinfo:
        AuthContext(actor=soporte, subject=cliente, transport="cookie")

    assert "impersonation" in str(excinfo.value)


def test_impersonar_con_permiso_si(soporte: Principal, cliente: Principal):
    ctx = AuthContext(
        actor=soporte, subject=cliente, transport="cookie", impersonation=_permiso()
    )

    assert ctx.is_impersonating is True
    assert ctx.actor_id == soporte.user_id
    assert ctx.subject_id == cliente.user_id


def test_un_permiso_sin_impersonacion_real_tampoco(soporte: Principal):
    """
    El otro sentido: ensuciaría la auditoría con una impersonación que nunca ocurrió.

    Es fácil de pasar por alto y es la mitad del invariante: si sólo se validara el primer
    caso, un handler podría adjuntar un permiso "por las dudas" y la auditoría quedaría
    llena de impersonaciones falsas.
    """
    with pytest.raises(ValidationError):
        AuthContext(
            actor=soporte, subject=soporte, transport="cookie", impersonation=_permiso()
        )


def test_el_contexto_es_inmutable(soporte: Principal, cliente: Principal):
    """
    `frozen=True`: no se puede reasignar el sujeto para esquivar el validador.

    Sin esto, el invariante se chequea una vez al construir y después `ctx.subject = otro`
    lo deja en cualquier estado.
    """
    ctx = AuthContext(actor=soporte, subject=soporte, transport="cookie")

    with pytest.raises(ValidationError):
        ctx.subject = cliente  # type: ignore[misc]


def test_la_ventana_de_impersonacion_tiene_que_ser_valida():
    with pytest.raises(ValidationError, match="posterior a granted_at"):
        Impersonation(
            granted_by=uuid4(),
            reason="al revés",
            granted_at=AHORA,
            expires_at=AHORA - timedelta(minutes=1),
        )


def test_el_motivo_de_la_impersonacion_no_puede_estar_vacio():
    """Una auditoría que dice quién y a quién pero no por qué no responde nada."""
    with pytest.raises(ValidationError):
        Impersonation(
            granted_by=uuid4(),
            reason="",
            granted_at=AHORA,
            expires_at=AHORA + timedelta(minutes=5),
        )


# ── Autorización: se consulta el ACTOR, no el sujeto ──────────────────────────
def test_los_permisos_son_del_actor_no_del_sujeto(cliente: Principal):
    """
    La decisión de autorización central del módulo.

    Si se consultara el sujeto, impersonar a un admin sería una escalación de privilegios en
    un solo paso: el operador se pondría los permisos de la cuenta a la que entró.
    """
    operador = Principal(user_id=uuid4(), scopes=frozenset({"users.view"}))
    admin = Principal(user_id=uuid4(), scopes=frozenset({"users.delete"}))

    ctx = AuthContext(
        actor=operador, subject=admin, transport="cookie", impersonation=_permiso()
    )

    assert ctx.has_scope("users.view") is True
    # El permiso del sujeto NO se hereda.
    assert ctx.has_scope("users.delete") is False


def test_require_scopes_reporta_todos_los_que_faltan(soporte: Principal):
    """De a uno convertiría una corrección en varias vueltas."""
    ctx = AuthContext(actor=soporte, subject=soporte, transport="bearer")

    with pytest.raises(InsufficientScopeError) as excinfo:
        ctx.require_scopes("users.delete", "billing.refund", "users.view")

    assert excinfo.value.required == {"users.delete", "billing.refund"}


def test_assert_not_impersonating_bloquea_las_operaciones_del_dueno(
    soporte: Principal, cliente: Principal
):
    """Cambiar la contraseña, refrescar, dar de alta 2FA: sólo el dueño real."""
    impersonado = AuthContext(
        actor=soporte, subject=cliente, transport="cookie", impersonation=_permiso()
    )
    normal = AuthContext(actor=cliente, subject=cliente, transport="cookie")

    with pytest.raises(ImpersonationNotPermittedError, match="change_password"):
        impersonado.assert_not_impersonating("change_password")

    normal.assert_not_impersonating("change_password")  # no lanza


# ── Principal de sistema: no es un superusuario ───────────────────────────────
def test_el_principal_de_sistema_no_tiene_roles():
    """
    Grants enumerados, no roles. Modelarlo con roles invitaría a darle "admin" y volver al
    superusuario por la puerta de atrás.
    """
    cron = SystemPrincipal(name="cron:cerrar-registros", scopes=frozenset({"register.close"}))

    assert cron.has_scope("register.close") is True
    assert cron.has_scope("users.delete") is False
    assert cron.has_role("admin") is False


def test_system_context_publica_y_limpia():
    assert current_auth() is None

    with system_context("cron:cerrar-registros", scopes={"register.close"}) as ctx:
        activo = current_auth()
        assert activo is ctx
        assert activo.is_system is True
        assert activo.transport == "internal"
        assert activo.has_scope("register.close") is True
        assert activo.is_impersonating is False

    assert current_auth() is None


def test_el_contexto_de_sistema_no_concede_lo_que_no_declara():
    """La ausencia de actor nunca debe resolver "permitido"."""
    with system_context("seed", scopes={"seed.run"}):
        ctx = require_auth()

        with pytest.raises(InsufficientScopeError):
            ctx.require_scopes("users.delete")


# ── Publicación ambiental ─────────────────────────────────────────────────────
def test_current_auth_nunca_lanza_fuera_de_un_scope():
    """Se llama desde handlers de logging y middlewares pre-auth: no puede explotar."""
    assert current_auth() is None


def test_require_auth_lanza_con_remediacion():
    with pytest.raises(UnauthenticatedError) as excinfo:
        require_auth()

    assert "system_context" in str(excinfo.value)


def test_auth_scope_anida_y_restaura_el_de_afuera(soporte: Principal, cliente: Principal):
    """
    Al salir se restaura el de afuera, no `None`.

    Es lo que permite que un handler corriendo en un worker despache otro comando y el actor
    restaurado se propague sin que la cadena de custodia se corte.
    """
    externo = AuthContext(actor=soporte, subject=soporte, transport="cookie")
    interno = AuthContext(actor=cliente, subject=cliente, transport="bearer")

    with auth_scope(externo):
        assert current_auth() is externo
        with auth_scope(interno):
            assert current_auth() is interno
        assert current_auth() is externo

    assert current_auth() is None


def test_auth_scope_limpia_aunque_haya_excepcion(soporte: Principal):
    """
    El `reset` va en un `finally`: si no, una excepción deja el contexto colgado para la
    corutina siguiente que reuse el mismo task — o sea, filtrado de identidad entre requests.
    """
    ctx = AuthContext(actor=soporte, subject=soporte, transport="cookie")

    with pytest.raises(RuntimeError):
        with auth_scope(ctx):
            raise RuntimeError("algo explotó en el handler")

    assert current_auth() is None


def test_el_contexto_esta_aislado_entre_hilos(soporte: Principal, cliente: Principal):
    """Un ContextVar es por contexto de ejecución: dos hilos no se pisan."""
    visto: dict[str, object] = {}
    listo = threading.Event()

    def en_otro_hilo() -> None:
        otro = AuthContext(actor=cliente, subject=cliente, transport="bearer")
        with auth_scope(otro):
            listo.wait(timeout=2)
            visto["hilo"] = current_auth()

    principal_ctx = AuthContext(actor=soporte, subject=soporte, transport="cookie")
    with auth_scope(principal_ctx):
        hilo = threading.Thread(target=en_otro_hilo)
        hilo.start()
        visto["main"] = current_auth()
        listo.set()
        hilo.join(timeout=2)

    assert visto["main"] is principal_ctx
    assert visto["hilo"] is not principal_ctx
