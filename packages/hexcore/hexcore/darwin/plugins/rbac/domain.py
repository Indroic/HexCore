"""
El dominio de `rbac`: roles y permisos **persistidos y asignables**, con scope y expiración.

Hasta acá Darwin sólo tenía `RoleRegistry` (`domain/permissions.py`): roles "de código",
declarados al arrancar, sin persistencia ni forma de asignarlos desde una API. Este módulo no
lo reemplaza — lo complementa. Los roles de `RoleRegistry` siguen siendo la fuente de verdad
para lo que un rol *significa* (qué permisos tiene, de quién hereda); lo que faltaba era
**quién tiene qué rol, en qué scope, y hasta cuándo**, y eso es lo que las cinco tablas de este
plugin agregan.

`scope_key` es la convención de tenancy de todo el plugin: `""` es global, y **nunca `NULL`**.
Un `UNIQUE` con `NULL` no rechaza duplicados —cada `NULL` es distinto de sí mismo, en SQL y en
Mongo— así que `""` es lo que hace que `UNIQUE(scope_key, name)` signifique algo. Es la misma
decisión que ya toma `IdentityConfig` en otras partes del framework.

Este módulo es **dominio puro**: sólo stdlib y pydantic. Los puertos declaran lo que los
handlers de RBAC necesitan y nada más — no hay `list_all()` genérico, y anti-escalada, ciclos
de herencia y todo lo que orquesta varias operaciones vive en `service.py` (Fase F2), no acá.
"""
from __future__ import annotations

import abc
import typing as t
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from hexcore.darwin.domain.exceptions import IdentityError

__all__ = [
    "GLOBAL_SCOPE",
    "RbacRole",
    "RbacPermission",
    "RoleAssignment",
    "AbstractRbacRoleRepository",
    "AbstractRbacPermissionRepository",
    "AbstractRbacUserRoleRepository",
    "AbstractAuthzVersionRepository",
    "RbacError",
    "RoleNotFoundError",
    "RBAC_EXCEPTION_STATUS_MAP",
]

#: El scope global. Nunca `NULL` — ver el docstring del módulo.
GLOBAL_SCOPE = ""


# ── Las entidades ─────────────────────────────────────────────────────────────
class RbacRole(BaseModel):
    """
    Un rol persistido, en un scope.

    `is_system` marca los roles sincronizados desde `RoleRegistry` ("roles de código"): el
    servicio de la Fase F2 los sincroniza al arrancar y **rechaza** editarlos o borrarlos por
    API. No es un campo informativo, es el candado — la razón está en que un rol de código
    puede estar hardcodeado en rutas y hooks del lado del consumidor, así que borrarlo desde un
    panel de administración rompería algo que ese panel no puede ver.
    """

    id: UUID
    scope_key: str = GLOBAL_SCOPE
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    is_system: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RbacPermission(BaseModel):
    """
    Una entrada del catálogo de permisos: `key` + descripción.

    `key` es `"recurso.accion"` o `"recurso.*"` — la misma forma que `Permission` ya valida en
    `domain/permissions.py`. El catálogo es para un panel de administración ("elegí de esta
    lista") y para el seed de los roles de código; **la decisión de autorización no lo
    consulta**: usa el `key` que ya viene guardado en `darwin_rbac_role_permission`, así que un
    catálogo vacío no le impide a un rol conceder nada.
    """

    id: UUID
    key: str = Field(min_length=1, max_length=255)
    description: str = ""

    @field_validator("key")
    @classmethod
    def _tiene_forma_de_permiso(cls, valor: str) -> str:
        """
        `"recurso.accion"`, `"recurso.*"` o `"*"` a secas. Mismo criterio que `Permission`.

        Se valida acá y no con un `CHECK` de SQL: la forma de un permiso es una regla del
        dominio, no del almacenamiento, y validarla en el dominio la deja igual de estricta en
        Mongo, donde no hay `CHECK` que declarar.
        """
        if valor == "*":
            return valor
        partes = valor.split(".")
        if len(partes) < 2 or any(not p for p in partes):
            raise ValueError(
                f"'{valor}' no tiene forma de permiso. Esperado 'recurso.accion', "
                f"'recurso.*' o '*'."
            )
        return valor


class RoleAssignment(BaseModel):
    """
    Un rol asignado a un usuario, en un scope, con vencimiento opcional.

    `expires_at=None` es "no vence". La expiración es la propia asignación la que vence, no el
    rol: el mismo rol puede estar asignado a otro usuario (o al mismo, en otro scope) sin
    fecha.
    """

    id: UUID
    user_id: UUID
    role_id: UUID
    scope_key: str = GLOBAL_SCOPE
    #: Quién la otorgó. Auditoría: sin esto, "¿quién le dio admin a X?" no tiene respuesta.
    granted_by: UUID | None = None
    expires_at: datetime | None = None
    created_at: datetime | None = None

    def is_expired_at(self, moment: datetime) -> bool:
        return self.expires_at is not None and moment >= self.expires_at


# ── Los puertos ───────────────────────────────────────────────────────────────
class AbstractRbacRoleRepository(abc.ABC):
    """
    Los roles: alta, baja, permisos directos y de quién heredan.

    Los permisos directos y los padres del rol se declaran acá —`set_permissions`/`set_parents`
    reemplazan el conjunto entero— y no como `add_permission`/`remove_permission` de a uno: un
    formulario de edición de rol manda el set completo, y reemplazar es una sola operación en
    vez de un diff que el llamador tendría que calcular.
    """

    @abc.abstractmethod
    async def add(self, role: RbacRole) -> RbacRole:
        """Crea el rol. Sin permisos ni padres — para eso, `set_permissions`/`set_parents`."""

    @abc.abstractmethod
    async def get(self, role_id: UUID) -> RbacRole | None:
        raise NotImplementedError

    @abc.abstractmethod
    async def get_by_name(self, scope_key: str, name: str) -> RbacRole | None:
        """Por nombre dentro de un scope, que es `UNIQUE(scope_key, name)`."""

    @abc.abstractmethod
    async def list_for_scope(self, scope_key: str) -> list[RbacRole]:
        raise NotImplementedError

    @abc.abstractmethod
    async def update(self, role: RbacRole) -> RbacRole:
        """
        Actualiza descripción y nombre. **No** toca `is_system` — eso lo decide el servicio,
        no un `UPDATE` genérico.

        Raises:
            RoleNotFoundError: si el rol no existe.
        """

    @abc.abstractmethod
    async def delete(self, role_id: UUID) -> bool:
        """Borra el rol. Sus permisos, padres y asignaciones se van por `CASCADE`."""

    @abc.abstractmethod
    async def set_permissions(self, role_id: UUID, permission_keys: t.Iterable[str]) -> None:
        """Reemplaza el set de permisos **directos** del rol por estas claves."""

    @abc.abstractmethod
    async def permission_keys_for(self, role_id: UUID) -> frozenset[str]:
        """
        Los permisos **directos** del rol, sin resolver herencia.

        Resolver la herencia completa —recorrer `parent_ids_for` transitivamente— es del
        servicio, con el mismo DFS con detección de ciclos que ya usa `RoleRegistry`: este
        puerto sólo lee una tabla.
        """

    @abc.abstractmethod
    async def set_parents(self, role_id: UUID, parent_ids: t.Iterable[UUID]) -> None:
        """Reemplaza de qué roles hereda éste. La validación de ciclos es del servicio."""

    @abc.abstractmethod
    async def parent_ids_for(self, role_id: UUID) -> frozenset[UUID]:
        raise NotImplementedError


class AbstractRbacPermissionRepository(abc.ABC):
    """El catálogo de permisos. Ver el docstring de `RbacPermission`."""

    @abc.abstractmethod
    async def add(self, permission: RbacPermission) -> RbacPermission:
        raise NotImplementedError

    @abc.abstractmethod
    async def get_by_key(self, key: str) -> RbacPermission | None:
        raise NotImplementedError

    @abc.abstractmethod
    async def list_all(self) -> list[RbacPermission]:
        raise NotImplementedError

    @abc.abstractmethod
    async def ensure(self, keys: t.Iterable[str]) -> None:
        """
        Da de alta las claves que no estén, sin duplicar las que ya están.

        Idempotente a propósito: es lo que el seed de los roles de código llama en cada
        arranque, y un seed que fallara la segunda vez por `UNIQUE` violado tumbaría el
        proceso por algo que no es un error.
        """


class AbstractRbacUserRoleRepository(abc.ABC):
    """Las asignaciones de rol a usuario, por scope."""

    @abc.abstractmethod
    async def assign(self, assignment: RoleAssignment) -> RoleAssignment:
        raise NotImplementedError

    @abc.abstractmethod
    async def revoke(self, user_id: UUID, role_id: UUID, scope_key: str) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    async def list_for_user(self, user_id: UUID, scope_key: str) -> list[RoleAssignment]:
        """Las asignaciones de ese usuario en ese scope, **vencidas incluidas**: para un panel
        de administración que quiere mostrar todo, no sólo lo vigente."""

    @abc.abstractmethod
    async def active_role_ids_for(
        self, user_id: UUID, scope_key: str, *, at: datetime
    ) -> frozenset[UUID]:
        """
        Los ids de rol **vigentes** de un usuario en ese scope, a `at`.

        Es la consulta del camino caliente: el resolver de principales (Fase F2) la llama en
        cada sign-in y en cada rotación de refresh, así que filtra los vencidos en la propia
        consulta y no después en Python.
        """


class AbstractAuthzVersionRepository(abc.ABC):
    """
    El contador de versión de permisos por scope.

    Es lo que permite invalidar sin esperar el TTL de una cache ni el vencimiento de un token:
    subir la versión de un scope hace que cualquier decisión cacheada con la versión vieja se
    trate como stale. Ver el claim `pv` de `AccessTokenClaims` (Fase F0).
    """

    @abc.abstractmethod
    async def get(self, scope_key: str) -> int:
        """La versión actual. `0` si el scope nunca subió — no lanza."""

    @abc.abstractmethod
    async def bump(self, scope_key: str) -> int:
        """
        Sube la versión en **una sola operación atómica** y devuelve el valor nuevo.

        Tiene que ser upsert-y-sumar en una sentencia, no leer-sumar-escribir: dos revocaciones
        concurrentes en el mismo scope con leer-y-después-escribir perderían una, y el efecto
        perdido es "este cambio de permisos no se propaga hasta que algo más lo fuerce".
        """


# ── Las excepciones ───────────────────────────────────────────────────────────
class RbacError(IdentityError):
    """Base de las fallas del plugin `rbac`."""


class RoleNotFoundError(RbacError):
    """No existe ese rol. 404."""


#: El mapa que el plugin aporta vía `exception_status_map()` (Fase F2). Se declara ya, junto a
#: la excepción, con el mismo criterio que `ORGANIZATION_EXCEPTION_STATUS_MAP`.
RBAC_EXCEPTION_STATUS_MAP: dict[type[Exception], int] = {
    RoleNotFoundError: 404,
}
