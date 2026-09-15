"""
Roles y permisos, con jerarquía y comodines.

Absorbe `hexcore.domain.auth.permissions.PermissionsRegistry`, que era un `Dict[str, str]` de
nombre a valor y nada más: sin `Role`, sin jerarquía, sin ningún método para **preguntar** si
un permiso está concedido, sin `RLock` a pesar de tener estado mutable compartido, sin
consumidores y sin tests. Servía para listar nombres de permisos, no para autorizar.

Lo que se agrega, y por qué cada cosa:

- **`Permission` con comodines.** ``users.*`` concede ``users.view`` y ``users.invite``. Sin
  esto, agregar un permiso nuevo obliga a editar todos los roles que deberían tenerlo, y en
  la práctica alguien concede ``*`` "por ahora".
- **`Role` con herencia.** ``admin`` hereda de ``editor``, que hereda de ``viewer``. Sin
  herencia, los roles se copian y pegan y después divergen en silencio.
- **Resolución transitiva con detección de ciclos**, al construir y no al consultar.
- **`RLock`**, porque el registro es estado mutable compartido y el resto del repo trata la
  seguridad ante hilos como requisito (`HandlerRegistry`, `CQRSContainer`, `session`).
"""
from __future__ import annotations

import threading
import typing as t
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "Permission",
    "Role",
    "RoleRegistry",
    "PermissionCycleError",
]

#: Separador de la jerarquía de permisos. ``users.invite`` es un permiso de ``users``.
SEPARATOR = "."
WILDCARD = "*"


class PermissionCycleError(ValueError):
    """
    Los roles forman un ciclo de herencia.

    Se detecta al **registrar**, no al consultar: un ciclo descubierto en el primer chequeo
    de permisos de producción es un `RecursionError` en un camino caliente. Descubrirlo al
    arrancar es un error con los nombres de los roles involucrados.
    """


class Permission(BaseModel):
    """
    Un permiso, con soporte de comodín al final.

    Uso::

        Permission(value="users.*").grants("users.invite")     # True
        Permission(value="users.view").grants("users.invite")  # False
        Permission(value="*").grants("cualquier.cosa")         # True
    """

    model_config = ConfigDict(frozen=True)

    value: str = Field(min_length=1)

    def grants(self, required: str) -> bool:
        """Si este permiso concede `required`."""
        if self.value == WILDCARD:
            return True
        if self.value == required:
            return True
        if not self.value.endswith(f"{SEPARATOR}{WILDCARD}"):
            return False
        # `users.*` concede `users.invite` y `users.invite.bulk`, pero no `users` pelado:
        # el comodín cubre descendientes, no el nodo mismo.
        prefijo = self.value[: -len(WILDCARD)]
        return required.startswith(prefijo)

    def __str__(self) -> str:
        return self.value


class Role(BaseModel):
    """
    Un rol: un nombre, un set de permisos y los roles de los que hereda.

    `inherits` guarda **nombres**, no instancias, para que el orden de registro no importe:
    podés declarar ``admin`` antes que ``editor``. La resolución la hace el registro.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1)
    permissions: frozenset[str] = frozenset()
    inherits: frozenset[str] = frozenset()


class RoleRegistry:
    """
    Registro de roles y permisos, con resolución transitiva cacheada.

    Instanciable, no un singleton global: el `PermissionsRegistry` anterior también era
    por instancia y es lo correcto —los tests necesitan registros aislados—, pero además se
    expone `default_registry()` para las apps que quieren uno solo y no quieren pasarlo a
    mano por todas las capas.

    Thread-safe con `RLock`. Reentrante y no un `Lock` porque `resolve_permissions` se llama
    a sí misma al recorrer la herencia.

    Uso::

        roles = RoleRegistry()
        roles.register_role("viewer", permissions={"users.view"})
        roles.register_role("editor", permissions={"users.edit"}, inherits={"viewer"})
        roles.register_role("admin", permissions={"users.*"}, inherits={"editor"})

        roles.has_permission({"editor"}, "users.view")    # True, heredado de viewer
        roles.has_permission({"admin"}, "users.invite")   # True, por el comodín
        roles.has_permission({"viewer"}, "users.edit")    # False
    """

    def __init__(self) -> None:
        self._roles: dict[str, Role] = {}
        #: Cache de permisos resueltos por rol. Se invalida entero ante cualquier registro:
        #: un registro nuevo puede afectar a cualquier rol que herede de él, y calcular qué
        #: subconjunto invalidar cuesta más que recalcular a demanda.
        self._resueltos: dict[str, frozenset[str]] = {}
        self._lock = threading.RLock()

    # ── Registro ──────────────────────────────────────────────────────────────
    def register_role(
        self,
        name: str,
        *,
        permissions: t.Iterable[str] = (),
        inherits: t.Iterable[str] = (),
        replace: bool = False,
    ) -> "RoleRegistry":
        """
        Registra un rol. Fluido: devuelve `self`.

        Args:
            replace: Por defecto redefinir un rol es un error. Un registro duplicado suele
                ser un módulo importado dos veces, y dejar que la segunda definición gane en
                silencio hace que los permisos dependan del orden de importación.

        Raises:
            ValueError: si el rol ya existe y `replace` es `False`.
            PermissionCycleError: si la herencia forma un ciclo.
        """
        with self._lock:
            if name in self._roles and not replace:
                raise ValueError(
                    f"El rol '{name}' ya está registrado. Si la redefinición es a "
                    f"propósito, pasá `replace=True`; si no, revisá si el módulo que lo "
                    f"declara se está importando dos veces."
                )

            self._roles[name] = Role(
                name=name,
                permissions=frozenset(permissions),
                inherits=frozenset(inherits),
            )
            self._resueltos.clear()

            # Al registrar, no al consultar: un ciclo descubierto en el primer chequeo de
            # producción es un RecursionError en el camino caliente.
            self._detectar_ciclos(name)

        return self

    def register_roles(self, roles: t.Mapping[str, t.Iterable[str]]) -> "RoleRegistry":
        """Atajo para registrar varios roles planos, sin herencia."""
        for name, permissions in roles.items():
            self.register_role(name, permissions=permissions)
        return self

    def _detectar_ciclos(self, start: str) -> None:
        """DFS con la pila explícita, para reportar el ciclo completo y no sólo que hay uno."""
        camino: list[str] = []
        visitados: set[str] = set()

        def recorrer(nombre: str) -> None:
            if nombre in camino:
                ciclo = camino[camino.index(nombre) :] + [nombre]
                raise PermissionCycleError(
                    f"La herencia de roles forma un ciclo: {' -> '.join(ciclo)}. "
                    f"Un rol no puede heredar de sí mismo, ni directa ni indirectamente."
                )
            if nombre in visitados:
                return
            rol = self._roles.get(nombre)
            if rol is None:
                return
            camino.append(nombre)
            for padre in sorted(rol.inherits):
                recorrer(padre)
            camino.pop()
            visitados.add(nombre)

        recorrer(start)

    # ── Consulta ──────────────────────────────────────────────────────────────
    def resolve_permissions(self, role_name: str) -> frozenset[str]:
        """
        Los permisos de un rol, **incluidos los heredados**.

        Un rol que no existe devuelve el set vacío en vez de lanzar: los roles llegan desde
        el token, o sea desde afuera, y un rol borrado del registro no debería tumbar el
        request. Devolver vacío falla **cerrando** —no concede nada—, que es el lado seguro.
        """
        with self._lock:
            return self._resolver_sin_lock(role_name)

    def _resolver_sin_lock(self, role_name: str) -> frozenset[str]:
        cacheado = self._resueltos.get(role_name)
        if cacheado is not None:
            return cacheado

        rol = self._roles.get(role_name)
        if rol is None:
            return frozenset()

        acumulado = set(rol.permissions)
        for padre in rol.inherits:
            acumulado |= self._resolver_sin_lock(padre)

        resuelto = frozenset(acumulado)
        self._resueltos[role_name] = resuelto
        return resuelto

    def has_permission(self, role_names: t.Iterable[str], required: str) -> bool:
        """
        Si alguno de los roles concede `required`, contando herencia y comodines.

        Uso::

            if not roles.has_permission(principal.roles, "users.invite"):
                raise InsufficientScopeError({"users.invite"})
        """
        with self._lock:
            for nombre in role_names:
                for concedido in self._resolver_sin_lock(nombre):
                    if Permission(value=concedido).grants(required):
                        return True
        return False

    def effective_permissions(self, role_names: t.Iterable[str]) -> frozenset[str]:
        """La unión de los permisos de varios roles. Para armar el claim `scopes`."""
        with self._lock:
            acumulado: set[str] = set()
            for nombre in role_names:
                acumulado |= self._resolver_sin_lock(nombre)
        return frozenset(acumulado)

    # ── Introspección ─────────────────────────────────────────────────────────
    @property
    def role_names(self) -> frozenset[str]:
        with self._lock:
            return frozenset(self._roles)

    def get_role(self, name: str) -> Role | None:
        with self._lock:
            return self._roles.get(name)

    def all_permission_values(self) -> frozenset[str]:
        """Todo permiso mencionado por algún rol. Útil para un panel de administración."""
        with self._lock:
            acumulado: set[str] = set()
            for rol in self._roles.values():
                acumulado |= rol.permissions
        return frozenset(acumulado)

    def build_permissions_enum(self) -> type[Enum]:
        """
        Un `Enum` dinámico con todos los permisos registrados.

        Compatible con `PermissionsRegistry.build_permissions_enum`, con la anotación
        corregida: la anterior decía `-> Enum` cuando devuelve la **clase**, no un miembro.

        Los nombres se derivan del valor (``users.invite`` → ``USERS_INVITE``) porque un
        identificador de Python no puede llevar puntos.
        """
        with self._lock:
            miembros = {
                valor.replace(SEPARATOR, "_").replace(WILDCARD, "ALL").upper(): valor
                for valor in sorted(self.all_permission_values())
            }
        return Enum("PermissionsEnum", miembros, type=str)

    def clear(self) -> None:
        """Vacía el registro. Para aislar tests."""
        with self._lock:
            self._roles.clear()
            self._resueltos.clear()


# ── Registro por defecto del proceso ─────────────────────────────────────────
_default: RoleRegistry | None = None
_default_lock = threading.RLock()


def default_registry() -> RoleRegistry:
    """
    El registro compartido del proceso, creado al primer uso.

    Para las apps que tienen un solo juego de roles y no quieren pasar el registro a mano por
    todas las capas. Quien necesite aislamiento —un test, un multi-tenant con roles por
    tenant— instancia su propio `RoleRegistry`.
    """
    global _default
    with _default_lock:
        if _default is None:
            _default = RoleRegistry()
        return _default


def reset_default_registry() -> None:
    """Descarta el registro compartido. Para los tests."""
    global _default
    with _default_lock:
        _default = None
