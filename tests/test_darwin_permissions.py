"""
Darwin: roles, permisos, jerarquía y comodines.

`RoleRegistry` absorbe `hexcore.domain.auth.permissions.PermissionsRegistry`, que era un
`Dict[str, str]` de nombre a valor: sin `Role`, sin jerarquía, sin ningún método para
**preguntar** si un permiso está concedido, sin `RLock` a pesar de tener estado mutable
compartido, sin consumidores y sin un solo test. Servía para listar nombres, no para
autorizar.

Estos tests fijan lo que se agregó y por qué.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from enum import Enum

import pytest

from hexcore.darwin import (
    Permission,
    PermissionCycleError,
    Role,
    RoleRegistry,
    default_registry,
    reset_default_registry,
)


@pytest.fixture
def roles() -> RoleRegistry:
    """viewer <- editor <- admin, con un comodín en admin."""
    registro = RoleRegistry()
    registro.register_role("viewer", permissions={"users.view"})
    registro.register_role("editor", permissions={"users.edit"}, inherits={"viewer"})
    registro.register_role("admin", permissions={"users.*"}, inherits={"editor"})
    return registro


# ── Comodines ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "concedido, requerido, esperado",
    [
        ("users.view", "users.view", True),
        ("users.view", "users.edit", False),
        ("users.*", "users.view", True),
        ("users.*", "users.invite", True),
        # El comodín cubre descendientes, no el nodo mismo: `users.*` no concede `users`.
        ("users.*", "users", False),
        # Anidado a más de un nivel.
        ("users.*", "users.invite.bulk", True),
        ("*", "cualquier.cosa", True),
        # Un prefijo parcial no alcanza: `user.*` no puede conceder algo de `users`.
        ("user.*", "users.view", False),
    ],
)
def test_los_comodines_conceden_lo_que_deben(concedido, requerido, esperado):
    assert Permission(value=concedido).grants(requerido) is esperado


# ── Herencia ──────────────────────────────────────────────────────────────────
def test_la_herencia_es_transitiva(roles: RoleRegistry):
    """admin -> editor -> viewer: admin tiene los permisos de los tres."""
    resueltos = roles.resolve_permissions("admin")

    assert resueltos == {"users.*", "users.edit", "users.view"}


def test_un_rol_hereda_del_padre(roles: RoleRegistry):
    assert roles.has_permission({"editor"}, "users.view") is True


def test_la_herencia_no_va_para_abajo(roles: RoleRegistry):
    """viewer no recibe nada de editor. Sin esto, la jerarquía no serviría para nada."""
    assert roles.has_permission({"viewer"}, "users.edit") is False


def test_el_comodin_heredado_concede_permisos_que_nadie_declaro(roles: RoleRegistry):
    """`users.*` en admin concede `users.invite`, que ningún rol menciona."""
    assert roles.has_permission({"admin"}, "users.invite") is True


def test_el_orden_de_registro_no_importa():
    """
    `inherits` guarda nombres, no instancias, así que se puede declarar el hijo antes del
    padre. Con instancias, el orden de importación de los módulos definiría los permisos.
    """
    registro = RoleRegistry()
    registro.register_role("admin", permissions={"users.delete"}, inherits={"editor"})
    registro.register_role("editor", permissions={"users.edit"})

    assert registro.has_permission({"admin"}, "users.edit") is True


def test_varios_roles_se_unen(roles: RoleRegistry):
    roles.register_role("facturacion", permissions={"billing.refund"})

    assert roles.effective_permissions({"viewer", "facturacion"}) == {
        "users.view",
        "billing.refund",
    }


# ── Ciclos ────────────────────────────────────────────────────────────────────
def test_un_ciclo_se_detecta_al_registrar_no_al_consultar():
    """
    Un ciclo descubierto en el primer chequeo de producción es un `RecursionError` en un
    camino caliente. Descubrirlo al arrancar es un error con los nombres involucrados.
    """
    registro = RoleRegistry()
    registro.register_role("a", inherits={"b"})
    registro.register_role("b", inherits={"c"})

    with pytest.raises(PermissionCycleError) as excinfo:
        registro.register_role("c", inherits={"a"})

    mensaje = str(excinfo.value)
    assert "->" in mensaje
    # El mensaje nombra a los roles del ciclo, no sólo dice que hay uno.
    for nombre in ("a", "b", "c"):
        assert nombre in mensaje


def test_un_rol_que_hereda_de_si_mismo_es_un_ciclo():
    registro = RoleRegistry()

    with pytest.raises(PermissionCycleError):
        registro.register_role("ouroboros", inherits={"ouroboros"})


# ── Registro duplicado ────────────────────────────────────────────────────────
def test_redefinir_un_rol_es_un_error_por_defecto(roles: RoleRegistry):
    """
    Un duplicado suele ser un módulo importado dos veces. Dejar que la segunda definición
    gane en silencio hace que los permisos dependan del orden de importación.
    """
    with pytest.raises(ValueError, match="ya está registrado"):
        roles.register_role("viewer", permissions={"otra.cosa"})


def test_replace_permite_redefinir_a_proposito(roles: RoleRegistry):
    roles.register_role("viewer", permissions={"users.list"}, replace=True)

    assert roles.resolve_permissions("viewer") == {"users.list"}


def test_redefinir_invalida_el_cache_de_los_hijos(roles: RoleRegistry):
    """
    El cache se limpia entero ante cualquier registro. Si no, admin seguiría respondiendo con
    los permisos viejos de viewer después de redefinirlo.
    """
    assert roles.has_permission({"admin"}, "users.view") is True

    roles.register_role("viewer", permissions={"users.list"}, replace=True)

    assert roles.has_permission({"admin"}, "users.list") is True
    assert "users.view" not in roles.resolve_permissions("admin")


# ── Fallar cerrando ───────────────────────────────────────────────────────────
def test_un_rol_inexistente_no_concede_nada(roles: RoleRegistry):
    """
    Los roles llegan desde el token, o sea desde afuera. Un rol borrado del registro no
    debería tumbar el request, y devolver vacío falla **cerrando**: no concede nada.
    """
    assert roles.resolve_permissions("rol-fantasma") == frozenset()
    assert roles.has_permission({"rol-fantasma"}, "users.view") is False


def test_sin_roles_no_hay_permisos(roles: RoleRegistry):
    assert roles.has_permission(frozenset(), "users.view") is False


# ── Introspección ─────────────────────────────────────────────────────────────
def test_build_permissions_enum_devuelve_una_clase(roles: RoleRegistry):
    """
    `PermissionsRegistry.build_permissions_enum` anotaba `-> Enum` cuando devuelve la
    **clase**, no un miembro. Acá está corregido a `-> type[Enum]`.
    """
    PermissionsEnum = roles.build_permissions_enum()

    assert isinstance(PermissionsEnum, type)
    assert issubclass(PermissionsEnum, Enum)
    # `users.edit` -> USERS_EDIT, porque un identificador no puede llevar puntos.
    assert PermissionsEnum["USERS_EDIT"].value == "users.edit"
    # `users.*` -> USERS_ALL.
    assert PermissionsEnum["USERS_ALL"].value == "users.*"


def test_role_names_y_get_role(roles: RoleRegistry):
    assert roles.role_names == {"viewer", "editor", "admin"}
    rol = roles.get_role("editor")
    assert isinstance(rol, Role)
    assert rol.inherits == {"viewer"}


def test_clear_vacia_el_registro(roles: RoleRegistry):
    roles.clear()

    assert roles.role_names == frozenset()


# ── Seguridad ante hilos ──────────────────────────────────────────────────────
def test_el_registro_es_seguro_bajo_concurrencia():
    """
    `PermissionsRegistry` no tenía `RLock` con estado mutable compartido. El resto del repo
    trata la seguridad ante hilos como requisito (`HandlerRegistry`, `CQRSContainer`).

    Se registra y se consulta a la vez desde 16 hilos: sin lock, la limpieza del cache en
    medio de una resolución puede devolver un set incompleto o explotar.
    """
    registro = RoleRegistry()
    registro.register_role("base", permissions={"base.read"})

    def trabajar(i: int) -> bool:
        registro.register_role(f"rol{i}", permissions={f"p{i}"}, inherits={"base"})
        return registro.has_permission({f"rol{i}"}, "base.read")

    with ThreadPoolExecutor(max_workers=16) as pool:
        resultados = list(pool.map(trabajar, range(64)))

    assert all(resultados)
    assert len(registro.role_names) == 65


# ── Registro compartido del proceso ───────────────────────────────────────────
def test_default_registry_es_el_mismo_objeto():
    reset_default_registry()
    try:
        assert default_registry() is default_registry()
    finally:
        reset_default_registry()


def test_reset_default_registry_lo_descarta():
    reset_default_registry()
    primero = default_registry()
    reset_default_registry()

    assert default_registry() is not primero
    reset_default_registry()
