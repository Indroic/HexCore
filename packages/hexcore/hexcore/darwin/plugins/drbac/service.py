"""
`DrbacService`: la única puerta de escritura de políticas y bindings.

Dos cosas que un repositorio no garantiza por sí solo:

1. **Los límites de tamaño se aplican acá, al guardar** (`enforce_condition_limits`, Fase F4) —
   no sólo al compilar en el PDP. Ver el docstring de `conditions.py`: frenar un árbol enorme
   recién al evaluar ya pagó el costo de haberlo persistido.
2. **`client_evaluable` nunca se confía tal cual declarado.** Si la condición de una regla usa
   `Predicate` en cualquier parte del árbol, se fuerza a `False` sin importar lo que haya puesto
   quien la creó — un predicado sólo se evalúa en el servidor (`predicates.py`), y mandarle esa
   regla al cliente (Fase F6) sería mentirle sobre qué puede evaluar por su cuenta.

**Los bindings no bumpean la versión de scope.** A diferencia de las políticas —que
`PolicySetCache` cachea por `(scope_key, version)`—, el PDP lee los bindings de rol contextual
**siempre en vivo** (`active_for_subject_at_scopes`, sin cache): no hay ninguna entrada cacheada
que un alta o una revocación de binding pueda dejar vieja, así que subir la versión ahí no
invalidaría nada — sólo confundiría a quien lee `DrbacAuthorizationChangedEvent` esperando que
cada evento se corresponda con un cambio de versión real.

**Sin anti-escalada.** A diferencia de `RbacService.set_role_permissions`/`assign_role`, este
servicio no verifica que quien crea una política no esté otorgando más de lo que él mismo tiene.
Es una decisión de alcance de la Fase F5 y no del plan original —que no especifica esa
verificación para DRBAC— y queda como un endurecimiento pendiente: hoy `authz.manage` en un
scope alcanza para escribir cualquier regla `allow` en ese scope, igual que ya alcanza para
crear roles de `rbac` con cualquier permiso listado en el catálogo.
"""
from __future__ import annotations

import typing as t
from datetime import datetime
from uuid import UUID, uuid4

from hexcore.darwin.plugins.drbac.conditions import (
    And,
    Condition,
    Not,
    Or,
    Predicate,
    enforce_condition_limits,
)
from hexcore.darwin.plugins.drbac.domain import (
    GLOBAL_SCOPE,
    AbstractDrbacAuthzVersionRepository,
    AbstractDrbacPolicyRepository,
    AbstractDrbacRoleBindingRepository,
    DrbacAuthorizationChangedEvent,
    Policy,
    PolicyAlreadyExistsError,
    PolicyNotFoundError,
    RoleBinding,
    RoleBindingNotFoundError,
    Rule,
)

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.ports import AbstractClock
    from hexcore.domain.cqrs.buses import AbstractEventBus as EventBus

__all__ = ["DrbacService"]

RuleInput = t.Union[Rule, t.Mapping[str, t.Any]]


def _usa_predicate(node: Condition) -> bool:
    """Si `node`, en cualquier profundidad, contiene un `Predicate`."""
    if isinstance(node, Predicate):
        return True
    if isinstance(node, (And, Or)):
        return any(_usa_predicate(h) for h in node.items)
    if isinstance(node, Not):
        return _usa_predicate(node.item)
    return False


class DrbacService:
    """
    Uso::

        servicio = DrbacService(
            policies=repos.PolicyRepository(),
            bindings=repos.RoleBindingRepository(),
            versions=repos.AuthzVersionRepository(),
            clock=contenedor.clock(),
        )

        politica = await servicio.create_policy(
            scope_key="org:1", name="no-self-approval",
            rules=[{
                "effect": "deny", "actions": ["invoice.approve"], "resource_type": "invoice",
                "condition": Eq(Var("resource.owner_id"), Var("subject.id")),
            }],
        )
    """

    def __init__(
        self,
        *,
        policies: AbstractDrbacPolicyRepository,
        bindings: AbstractDrbacRoleBindingRepository,
        versions: AbstractDrbacAuthzVersionRepository,
        clock: "AbstractClock",
        events: "EventBus | None" = None,
    ) -> None:
        self._policies = policies
        self._bindings = bindings
        self._versions = versions
        self._clock = clock
        self._events = events

    # ── Políticas ─────────────────────────────────────────────────────────────
    async def create_policy(
        self,
        *,
        scope_key: str = GLOBAL_SCOPE,
        name: str,
        description: str = "",
        enabled: bool = True,
        priority: int = 100,
        rules: t.Iterable[RuleInput] = (),
        created_by: UUID | None = None,
    ) -> Policy:
        """
        Raises:
            PolicyAlreadyExistsError: ya existe una política con ese nombre en ese scope.
            ConditionTooComplexError: alguna condición excede los límites de tamaño.
        """
        if await self._policies.get_by_name(scope_key, name) is not None:
            raise PolicyAlreadyExistsError(
                f"Ya existe una política '{name}' en el scope '{scope_key or '(global)'}'."
            )
        politica = Policy(
            id=uuid4(),
            scope_key=scope_key,
            name=name,
            description=description,
            enabled=enabled,
            priority=priority,
            rules=self._preparar_reglas(rules),
            created_by=created_by,
        )
        creada = await self._policies.add(politica)
        await self._bump(scope_key, reason="policy_created")
        return creada

    async def get_policy(self, policy_id: UUID) -> Policy:
        politica = await self._policies.get(policy_id)
        if politica is None:
            raise PolicyNotFoundError(f"No existe la política {policy_id}.")
        return politica

    async def list_policies(self, scope_key: str) -> list[Policy]:
        return await self._policies.list_for_scope(scope_key)

    async def update_policy(
        self,
        policy_id: UUID,
        *,
        name: str | None = None,
        description: str | None = None,
        enabled: bool | None = None,
        priority: int | None = None,
        rules: t.Iterable[RuleInput] | None = None,
    ) -> Policy:
        """
        Reemplaza lo que se pase; lo que se omite queda igual. `rules=[]` (lista vacía, no
        `None`) reemplaza por "sin reglas" — es la política deshabilitada de facto.

        Raises:
            PolicyNotFoundError
            ConditionTooComplexError
        """
        actual = await self.get_policy(policy_id)
        actualizada = actual.model_copy(
            update={
                "name": name if name is not None else actual.name,
                "description": description if description is not None else actual.description,
                "enabled": enabled if enabled is not None else actual.enabled,
                "priority": priority if priority is not None else actual.priority,
                "rules": self._preparar_reglas(rules) if rules is not None else actual.rules,
            }
        )
        guardada = await self._policies.update(actualizada)
        await self._bump(actual.scope_key, reason="policy_updated")
        return guardada

    async def delete_policy(self, policy_id: UUID) -> bool:
        actual = await self.get_policy(policy_id)
        borrada = await self._policies.delete(policy_id)
        if borrada:
            await self._bump(actual.scope_key, reason="policy_deleted")
        return borrada

    def _preparar_reglas(self, rules: t.Iterable[RuleInput]) -> tuple[Rule, ...]:
        preparadas: list[Rule] = []
        for posicion, cruda in enumerate(rules):
            if isinstance(cruda, Rule):
                regla = cruda
            else:
                # Un `id` propio es lo que le da a una regla identidad estable entre ediciones
                # (el `policy_id` de una `Decision`, un log); quien arma el cuerpo de la API no
                # tiene por qué inventarlo para una regla nueva.
                datos: dict[str, t.Any] = dict(cruda)
                datos.setdefault("id", uuid4())
                regla = Rule(**datos)
            if regla.condition is not None:
                enforce_condition_limits(regla.condition)
                if regla.client_evaluable and _usa_predicate(regla.condition):
                    regla = regla.model_copy(update={"client_evaluable": False})
            if regla.position != posicion:
                regla = regla.model_copy(update={"position": posicion})
            preparadas.append(regla)
        return tuple(preparadas)

    # ── Bindings de rol contextual ────────────────────────────────────────────
    async def create_binding(
        self,
        *,
        subject_id: UUID,
        role_name: str,
        scope_path: str = GLOBAL_SCOPE,
        condition: Condition | None = None,
        expires_at: datetime | None = None,
        granted_by: UUID | None = None,
        at: datetime | None = None,
    ) -> RoleBinding:
        """
        Raises:
            ConditionTooComplexError: si `condition` excede los límites de tamaño.
        """
        if condition is not None:
            enforce_condition_limits(condition)
        return await self._bindings.add(
            RoleBinding(
                id=uuid4(),
                subject_id=subject_id,
                role_name=role_name,
                scope_path=scope_path,
                condition=condition,
                expires_at=expires_at,
                granted_by=granted_by,
                created_at=at or self._clock.now(),
            )
        )

    async def revoke_binding(self, binding_id: UUID) -> bool:
        borrado = await self._bindings.delete(binding_id)
        if not borrado:
            raise RoleBindingNotFoundError(f"No existe el binding {binding_id}.")
        return borrado

    async def list_bindings_for_subject(self, subject_id: UUID) -> list[RoleBinding]:
        return await self._bindings.list_for_subject(subject_id)

    # ── Versión ───────────────────────────────────────────────────────────────
    async def authz_version(self, scope_key: str) -> int:
        return await self._versions.get(scope_key)

    async def _bump(self, scope_key: str, *, reason: str) -> None:
        nueva_version = await self._versions.bump(scope_key)
        if self._events is not None:
            await self._events.publish(
                DrbacAuthorizationChangedEvent(
                    scope_key=scope_key, reason=reason, version=nueva_version
                )
            )
