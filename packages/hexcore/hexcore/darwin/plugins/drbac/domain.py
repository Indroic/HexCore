"""
El dominio de `drbac`: políticas condicionales, sus reglas, y bindings de rol contextuales por
scope jerárquico.

**No hay ninguna referencia a `rbac` acá, ni por FK ni por import.** `RoleBinding.role_name` es
un `str` suelto —el mismo nombre que un rol de `rbac`— y no un `role_id: UUID` con clave foránea
a `darwin_rbac_role`: dos plugins que no se conocen entre sí (`test_darwin_plugin_decoupling.py`)
no pueden compartir una tabla por FK. Quien expande ese nombre a permisos concretos es un
callable que **el consumidor** inyecta al cablear los dos plugins juntos —típicamente
`rbac_plugin.service().permission_keys_for_role_name`—, nunca un import cruzado; ver
`pdp.RolePermissionsResolver`. Es el mismo patrón que ya usa
`RbacAuthorizationProvider._con_rol_de_organizacion` para integrarse con `organization` sin
importarlo: la integración vive en la app que cablea los plugins, no en ninguno de los dos.

`scope_path` es la ruta jerárquica materializada (`"org:42/project:7"`), a diferencia del
`scope_key` plano de `rbac`: una política en `"org:42"` aplica también a
`"org:42/project:7"` — herencia descendente, resuelta con `scope_chain()` más abajo, siempre
por **segmentos** y nunca por prefijo de string crudo (el mismo riesgo que `WithinScope`,
Fase F4, ya previene en el AST).

Igual que `rbac/domain.py`: sólo stdlib y pydantic. La orquestación (el PDP, el PIP, el cache)
vive en sus propios módulos.
"""
from __future__ import annotations

import abc
import typing as t
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hexcore.darwin.domain.exceptions import IdentityError
from hexcore.darwin.plugins.drbac.conditions import (
    Condition,
    ConditionTooComplexError,
    InvalidVarPathError,
)
from hexcore.domain.events import DomainEvent

__all__ = [
    "GLOBAL_SCOPE",
    "scope_chain",
    "Rule",
    "Policy",
    "RoleBinding",
    "AbstractDrbacPolicyRepository",
    "AbstractDrbacRoleBindingRepository",
    "AbstractDrbacAuthzVersionRepository",
    "DrbacAuthorizationChangedEvent",
    "DrbacError",
    "PolicyNotFoundError",
    "PolicyAlreadyExistsError",
    "RoleBindingNotFoundError",
    "DRBAC_EXCEPTION_STATUS_MAP",
]

#: El scope global. Nunca `NULL` — misma convención que `rbac.GLOBAL_SCOPE`.
GLOBAL_SCOPE = ""


def scope_chain(scope_path: str) -> tuple[str, ...]:
    """
    Los ancestros de `scope_path`, del más general al más específico, **incluidos** el global
    y el propio `scope_path`.

    ``scope_chain("org:42/project:7")`` → ``("", "org:42", "org:42/project:7")``. Es la lista
    de scopes cuyas políticas y bindings pueden aplicar a un recurso en ese scope — el PDP
    concatena las reglas de cada capa, de la más general a la más específica.

    Uso::

        for capa in scope_chain(resource.scope_path):
            reglas_de(capa)
    """
    if scope_path == GLOBAL_SCOPE:
        return (GLOBAL_SCOPE,)
    cadena = [GLOBAL_SCOPE]
    acumulado: list[str] = []
    for segmento in scope_path.split("/"):
        acumulado.append(segmento)
        cadena.append("/".join(acumulado))
    return tuple(cadena)


# ── Las entidades ─────────────────────────────────────────────────────────────
class Rule(BaseModel):
    """
    Una regla dentro de una `Policy`: qué acciones, sobre qué tipo de recurso, bajo qué
    condición, con qué efecto.

    `actions` usa la misma forma `"recurso.accion"`/`"recurso.*"`/`"*"` que el resto de Darwin
    (`Permission.grants`, del núcleo) — DRBAC no reimplementa el matcher de comodines de
    `rbac.matcher`: importarlo violaría el mismo desacoplamiento que `RoleBinding` respeta, así
    que la coincidencia de acción usa `hexcore.darwin.domain.permissions.Permission`, que es del
    núcleo y no de ningún plugin.

    `client_evaluable` marca si esta regla se puede mandar tal cual al cliente TypeScript (Fase
    F6) para evaluación optimista. **Nunca es `True` si `condition` usa `Predicate`**: el
    cliente no tiene forma de saber qué hace un predicado sin correr el mismo código Python —
    el servicio (Fase F5, `DrbacService`) lo fuerza a `False` en ese caso, no confía en lo que
    declaró quien creó la política.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    #: Orden de desempate dentro del mismo `effect` — no hay padres/hijos entre reglas, sólo el
    #: orden declarado dentro de su `Policy`.
    position: int = 0
    effect: t.Literal["allow", "deny"]
    actions: tuple[str, ...] = Field(min_length=1)
    resource_type: str = Field(min_length=1)
    condition: Condition | None = None
    client_evaluable: bool = False


class Policy(BaseModel):
    """
    Un conjunto de reglas, en un scope, con prioridad.

    Lleva sus `rules` embebidas en el propio modelo de dominio —aunque SQL las guarde en una
    tabla aparte (`darwin_drbac_rule`, por `position`)—: el PDP siempre necesita la política
    entera para evaluarla, nunca una regla suelta, así que separar la lectura en dos consultas
    sólo complicaría a cada repositorio sin que nadie se beneficie. Es el mismo criterio que ya
    usa el documento de Mongo, que las embebe de verdad.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    scope_key: str = GLOBAL_SCOPE
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    enabled: bool = True
    #: Menor corre primero, dentro del mismo `effect` — mismo criterio que `DarwinPlugin.priority`.
    priority: int = 100
    rules: tuple[Rule, ...] = ()
    created_by: UUID | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RoleBinding(BaseModel):
    """
    Un rol contextual: `role_name` aplica a `subject_id` en `scope_path` (y en todo lo que
    cuelgue de `scope_path`), opcionalmente sólo mientras `condition` sea verdadera, hasta
    `expires_at`.

    `role_name` es un nombre suelto y no una referencia a ningún rol de `rbac` — ver el
    docstring del módulo. Que el nombre "signifique" algo (qué permisos otorga) es una decisión
    de quien cablea los plugins juntos, con `pdp.RolePermissionsResolver`.
    """

    model_config = ConfigDict(frozen=True)

    id: UUID
    subject_id: UUID
    role_name: str = Field(min_length=1, max_length=128)
    scope_path: str = GLOBAL_SCOPE
    condition: Condition | None = None
    expires_at: datetime | None = None
    #: Quién la otorgó. Auditoría, igual que `RoleAssignment.granted_by` de `rbac`.
    granted_by: UUID | None = None
    created_at: datetime | None = None

    def is_expired_at(self, moment: datetime) -> bool:
        return self.expires_at is not None and moment >= self.expires_at


# ── Los puertos ───────────────────────────────────────────────────────────────
class AbstractDrbacPolicyRepository(abc.ABC):
    """Las políticas, con sus reglas. `set`/reemplazo entero, mismo criterio que
    `AbstractRbacRoleRepository.set_permissions`: un formulario de edición manda el conjunto
    completo, no un diff."""

    @abc.abstractmethod
    async def add(self, policy: Policy) -> Policy:
        raise NotImplementedError

    @abc.abstractmethod
    async def get(self, policy_id: UUID) -> Policy | None:
        raise NotImplementedError

    @abc.abstractmethod
    async def get_by_name(self, scope_key: str, name: str) -> Policy | None:
        """Por nombre dentro de un scope, que es `UNIQUE(scope_key, name)`."""

    @abc.abstractmethod
    async def list_for_scope(self, scope_key: str) -> list[Policy]:
        """Todas las políticas de ese scope exacto, habilitadas o no — para un panel de
        administración."""

    @abc.abstractmethod
    async def list_enabled_for_scopes(self, scope_keys: t.Iterable[str]) -> list[Policy]:
        """
        Las políticas **habilitadas** de varios scopes a la vez, ordenadas por `priority`.

        Es la consulta del camino caliente: el PDP la llama con la cadena entera de
        `scope_chain()` para no hacer una consulta por nivel de jerarquía.
        """

    @abc.abstractmethod
    async def update(self, policy: Policy) -> Policy:
        """
        Reemplaza descripción, `enabled`, `priority` y el conjunto entero de `rules`.

        Raises:
            PolicyNotFoundError
        """

    @abc.abstractmethod
    async def delete(self, policy_id: UUID) -> bool:
        raise NotImplementedError


class AbstractDrbacRoleBindingRepository(abc.ABC):
    """Los bindings de rol contextual."""

    @abc.abstractmethod
    async def add(self, binding: RoleBinding) -> RoleBinding:
        raise NotImplementedError

    @abc.abstractmethod
    async def get(self, binding_id: UUID) -> RoleBinding | None:
        raise NotImplementedError

    @abc.abstractmethod
    async def delete(self, binding_id: UUID) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    async def list_for_subject(self, subject_id: UUID) -> list[RoleBinding]:
        """Todos los bindings de ese subject, vencidos incluidos — para un panel."""

    @abc.abstractmethod
    async def active_for_subject_at_scopes(
        self, subject_id: UUID, scope_paths: t.Iterable[str], *, at: datetime
    ) -> list[RoleBinding]:
        """
        Los bindings **no vencidos** de `subject_id` cuyo `scope_path` está en `scope_paths`.

        Camino caliente del PDP: se le pasa la cadena entera de `scope_chain()` del recurso, así
        que un binding en cualquier ancestro del scope del recurso vuelve en un solo viaje.
        """


class AbstractDrbacAuthzVersionRepository(abc.ABC):
    """
    El contador de versión de DRBAC por scope — **propio**, no el de `rbac`.

    Duplicado a propósito: `AbstractAuthzVersionRepository` ya existe en `rbac.domain`, pero
    importarlo de acá violaría el mismo desacoplamiento entre plugins que el resto de este
    módulo respeta. Mismo contrato, mismo criterio de atomicidad (`bump` en una sola sentencia)
    que el original — ver su docstring.
    """

    @abc.abstractmethod
    async def get(self, scope_key: str) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    async def bump(self, scope_key: str) -> int:
        raise NotImplementedError


# ── Los eventos ───────────────────────────────────────────────────────────────
class DrbacAuthorizationChangedEvent(DomainEvent):
    """Los permisos condicionales de un scope cambiaron. Espejo de
    `rbac.domain.AuthorizationChangedEvent`, propio por la misma razón que el repositorio de
    versión."""

    scope_key: str
    #: `"policy_created"`, `"policy_updated"`, `"policy_deleted"`, `"binding_created"`,
    #: `"binding_revoked"`. Libre, para logs y métricas.
    reason: str
    version: int


# ── Las excepciones ───────────────────────────────────────────────────────────
class DrbacError(IdentityError):
    """Base de las fallas del plugin `drbac`."""


class PolicyNotFoundError(DrbacError):
    """No existe esa política. 404."""


class PolicyAlreadyExistsError(DrbacError):
    """Ya existe una política con ese nombre en ese scope (`UNIQUE(scope_key, name)`). 409."""


class RoleBindingNotFoundError(DrbacError):
    """No existe ese binding. 404."""


#: `ConditionTooComplexError`/`InvalidVarPathError` son de `conditions.py` (Fase F4, del propio
#: plugin) — se mapean acá para que un cuerpo de política inválido dé un 422 legible y no un
#: 500. `422` y no `409`: no es un conflicto con el estado existente, es la política misma la
#: que no pasa la validación.
DRBAC_EXCEPTION_STATUS_MAP: dict[type[Exception], int] = {
    PolicyNotFoundError: 404,
    PolicyAlreadyExistsError: 409,
    RoleBindingNotFoundError: 404,
    ConditionTooComplexError: 422,
    InvalidVarPathError: 422,
}
