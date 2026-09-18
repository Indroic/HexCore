"""
`RbacService`: la única puerta de escritura del plugin.

Todo lo que un repositorio no puede garantizar por sí solo vive acá, y son tres cosas
concretas:

1. **Anti-escalada.** Nadie puede otorgar —a un rol, o asignándolo a alguien— más de lo que él
   mismo tiene efectivamente en ese scope. Sin esto, cualquiera con permiso para administrar
   roles se auto-asigna `*` y el resto del control de acceso es decorativo.
2. **Ciclos de herencia.** `set_role_parents` los rechaza con el mismo criterio que
   `RoleRegistry`: un DFS que detecta el ciclo al declarar, no al resolver permisos en el
   camino caliente.
3. **La versión de autorización sube con cada mutación** que puede cambiar lo que alguien
   puede hacer: cambiar los permisos de un rol, su herencia, asignar o revocar, o borrarlo.
   Es lo que hace que `AuthorizationEngine` deje de usar una decisión cacheada sin esperar a
   que vença un TTL.

⚠️ **El bump no comparte transacción con la mutación.** Cada repositorio abre y cierra su
propia sesión (es el patrón de persistencia de todo Darwin: no hay una Unit of Work que cruce
repositorios). Si el proceso muere entre la mutación y el bump, el peor caso es una decisión
cacheada con la versión vieja que sigue viva un ciclo más de lo que debería — no una
concesión de más, porque `AuthorizationEngine` es default-deny y lo que quedó desactualizado
es una restricción, no un permiso.
"""
from __future__ import annotations

import typing as t
from datetime import datetime
from uuid import UUID, uuid4

from hexcore.darwin.plugins.rbac.domain import (
    GLOBAL_SCOPE,
    AbstractAuthzVersionRepository,
    AbstractRbacPermissionRepository,
    AbstractRbacRoleRepository,
    AbstractRbacUserRoleRepository,
    AuthorizationChangedEvent,
    EscalationError,
    RbacRole,
    RoleAlreadyExistsError,
    RoleAssignment,
    RoleCycleError,
    RoleNotFoundError,
    SystemRoleImmutableError,
)
from hexcore.darwin.plugins.rbac.matcher import CompiledPermissionSet

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.permissions import RoleRegistry
    from hexcore.darwin.domain.ports import AbstractClock
    from hexcore.domain.cqrs.buses import AbstractEventBus as EventBus

__all__ = ["RbacService"]


class RbacService:
    """
    Uso::

        servicio = RbacService(
            roles=repos.RbacRoleRepository(),
            permissions=repos.RbacPermissionRepository(),
            user_roles=repos.RbacUserRoleRepository(),
            versions=repos.AuthzVersionRepository(),
            clock=contenedor.clock(),
        )

        rol = await servicio.create_role(scope_key="org:1", name="accountant")
        await servicio.set_role_permissions(
            actor_id=admin.id, role_id=rol.id, scope_key="org:1",
            permission_keys=["invoice.read", "invoice.approve"],
        )
    """

    def __init__(
        self,
        *,
        roles: AbstractRbacRoleRepository,
        permissions: AbstractRbacPermissionRepository,
        user_roles: AbstractRbacUserRoleRepository,
        versions: AbstractAuthzVersionRepository,
        clock: "AbstractClock",
        events: "EventBus | None" = None,
    ) -> None:
        self._roles = roles
        self._permissions = permissions
        self._user_roles = user_roles
        self._versions = versions
        self._clock = clock
        self._events = events

    # ── Roles: CRUD ───────────────────────────────────────────────────────────
    async def create_role(
        self, *, scope_key: str = GLOBAL_SCOPE, name: str, description: str = ""
    ) -> RbacRole:
        """
        Crea un rol de usuario (`is_system=False`).

        Raises:
            RoleAlreadyExistsError: ya existe un rol con ese nombre en ese scope.
        """
        if await self._roles.get_by_name(scope_key, name) is not None:
            raise RoleAlreadyExistsError(
                f"Ya existe un rol '{name}' en el scope '{scope_key or '(global)'}'."
            )
        return await self._roles.add(
            RbacRole(id=uuid4(), scope_key=scope_key, name=name, description=description)
        )

    async def get_role(self, role_id: UUID) -> RbacRole:
        rol = await self._roles.get(role_id)
        if rol is None:
            raise RoleNotFoundError(f"No existe el rol {role_id}.")
        return rol

    async def list_roles(self, scope_key: str) -> list[RbacRole]:
        return await self._roles.list_for_scope(scope_key)

    async def active_role_ids(
        self, user_id: UUID, scope_key: str, *, at: datetime
    ) -> frozenset[UUID]:
        """Los roles vigentes de `user_id` en `scope_key`. Para el resolver de principales."""
        return await self._user_roles.active_role_ids_for(user_id, scope_key, at=at)

    async def authz_version(self, scope_key: str) -> int:
        """
        La versión actual de `darwin_authz_version` para `scope_key`.

        Para el `/me/permissions` del router: el cliente TypeScript (Fase F3) la usa para
        saber si el resumen que tiene en memoria quedó viejo, sin depender sólo del TTL.
        """
        return await self._versions.get(scope_key)

    async def roles_by_ids(self, role_ids: t.Iterable[UUID]) -> list[RbacRole]:
        """Para el resolver: de ids de rol a sus nombres. `None` se descarta en silencio —un
        rol borrado entre la asignación y la resolución no debería tumbar el sign-in."""
        resultado: list[RbacRole] = []
        for role_id in role_ids:
            rol = await self._roles.get(role_id)
            if rol is not None:
                resultado.append(rol)
        return resultado

    async def update_role(
        self, role_id: UUID, *, name: str | None = None, description: str | None = None
    ) -> RbacRole:
        """
        Actualiza nombre y/o descripción.

        Raises:
            RoleNotFoundError
            SystemRoleImmutableError: si el rol es `is_system`.
        """
        actual = await self.get_role(role_id)
        self._assert_editable(actual)
        return await self._roles.update(
            actual.model_copy(
                update={
                    "name": name if name is not None else actual.name,
                    "description": (
                        description if description is not None else actual.description
                    ),
                }
            )
        )

    async def delete_role(self, role_id: UUID) -> bool:
        """
        Borra el rol.

        Raises:
            RoleNotFoundError
            SystemRoleImmutableError: si el rol es `is_system`.
        """
        actual = await self.get_role(role_id)
        self._assert_editable(actual)
        borrado = await self._roles.delete(role_id)
        if borrado:
            await self._bump(actual.scope_key, reason="role_deleted")
        return borrado

    @staticmethod
    def _assert_editable(role: RbacRole) -> None:
        if role.is_system:
            raise SystemRoleImmutableError(
                f"El rol '{role.name}' es de código (`is_system`): no se edita ni se borra "
                f"por acá. Cambiá el `RoleRegistry` que lo declara."
            )

    # ── Roles: permisos directos y herencia ──────────────────────────────────
    async def set_role_permissions(
        self,
        *,
        actor_id: UUID,
        role_id: UUID,
        permission_keys: t.Iterable[str],
        at: datetime | None = None,
    ) -> None:
        """
        Reemplaza los permisos directos del rol. **Anti-escalada.**

        Raises:
            RoleNotFoundError
            SystemRoleImmutableError
            EscalationError: si `permission_keys` incluye algo que `actor_id` no tiene
                efectivamente en el scope del rol.
        """
        rol = await self.get_role(role_id)
        self._assert_editable(rol)
        claves = tuple(permission_keys)
        momento = at or self._clock.now()

        await self._assert_no_escalation(actor_id, rol.scope_key, claves, at=momento)
        # El catálogo tiene que tener la clave para que la fila de unión pueda referenciarla
        # por `permission_id` — `ensure` es idempotente, así que otorgar un permiso nuevo lo
        # da de alta en el catálogo como efecto lateral, en vez de exigir un alta manual antes.
        await self._permissions.ensure(claves)
        await self._roles.set_permissions(role_id, claves)
        await self._bump(rol.scope_key, reason="role_permissions_changed")

    async def set_role_parents(
        self,
        *,
        actor_id: UUID,
        role_id: UUID,
        parent_ids: t.Iterable[UUID],
        at: datetime | None = None,
    ) -> None:
        """
        Reemplaza de quién hereda el rol. **Ciclos y anti-escalada.**

        La anti-escalada se chequea sobre el efecto: los permisos que el rol tendría
        **después** del cambio —directos más heredados de los padres nuevos— tienen que seguir
        siendo un subconjunto de lo que `actor_id` tiene efectivamente. Sin esto, alguien sin
        permiso para tocar permisos directamente escala igual poniendo como padre un rol que
        sí los tiene.

        Raises:
            RoleNotFoundError
            SystemRoleImmutableError
            RoleCycleError
            EscalationError
        """
        rol = await self.get_role(role_id)
        self._assert_editable(rol)
        nuevos_padres = tuple(parent_ids)
        momento = at or self._clock.now()

        await self._assert_no_cycle(role_id, nuevos_padres)

        directos = await self._roles.permission_keys_for(role_id)
        heredados: set[str] = set()
        visitados: set[UUID] = {role_id}
        for padre in nuevos_padres:
            heredados |= await self._walk_role(padre, visitados=visitados)

        await self._assert_no_escalation(
            actor_id, rol.scope_key, set(directos) | heredados, at=momento
        )
        await self._roles.set_parents(role_id, nuevos_padres)
        await self._bump(rol.scope_key, reason="role_hierarchy_changed")

    async def _assert_no_cycle(self, role_id: UUID, parent_ids: t.Iterable[UUID]) -> None:
        """
        DFS con la pila explícita, mismo criterio que `RoleRegistry._detectar_ciclos`: reporta
        el ciclo completo, no sólo que hay uno.
        """
        visitados: set[UUID] = set()

        async def visitar(nodo: UUID, camino: list[UUID]) -> None:
            if nodo == role_id:
                cadena = " -> ".join(str(x) for x in (*camino, nodo))
                raise RoleCycleError(
                    f"set_role_parents formaría un ciclo de herencia: {cadena}. Un rol no "
                    f"puede heredar de sí mismo, ni directa ni indirectamente."
                )
            if nodo in visitados:
                return
            visitados.add(nodo)
            for padre in await self._roles.parent_ids_for(nodo):
                await visitar(padre, [*camino, nodo])

        for candidato in parent_ids:
            await visitar(candidato, [role_id])

    # ── Permisos efectivos ────────────────────────────────────────────────────
    async def _walk_role(
        self, role_id: UUID, *, visitados: set[UUID]
    ) -> set[str]:
        """Permisos directos de `role_id` más los de toda su cadena de herencia."""
        if role_id in visitados:
            return set()
        visitados.add(role_id)

        acumulado = set(await self._roles.permission_keys_for(role_id))
        for padre in await self._roles.parent_ids_for(role_id):
            acumulado |= await self._walk_role(padre, visitados=visitados)
        return acumulado

    async def effective_permission_keys(
        self, user_id: UUID, scope_key: str, *, at: datetime
    ) -> frozenset[str]:
        """Las claves de permiso de todos los roles vigentes de `user_id` en `scope_key`."""
        ids_de_rol = await self._user_roles.active_role_ids_for(user_id, scope_key, at=at)
        visitados: set[UUID] = set()
        acumulado: set[str] = set()
        for role_id in ids_de_rol:
            acumulado |= await self._walk_role(role_id, visitados=visitados)
        return frozenset(acumulado)

    async def effective_permissions(
        self, user_id: UUID, scope_key: str, *, at: datetime
    ) -> CompiledPermissionSet:
        """`effective_permission_keys`, ya compilado para preguntarle `grants()`."""
        return CompiledPermissionSet.compile(
            await self.effective_permission_keys(user_id, scope_key, at=at)
        )

    async def permission_keys_for_role_name(
        self, scope_key: str, name: str
    ) -> frozenset[str] | None:
        """
        Los permisos de un rol identificado por nombre, con su herencia resuelta.

        `None` si no existe ese rol en ese scope. Para integraciones que traducen un rol ajeno
        —el `OrgRole` de `organization`, por ejemplo— a un rol de `rbac` por nombre.
        """
        rol = await self._roles.get_by_name(scope_key, name)
        if rol is None:
            return None
        return frozenset(await self._walk_role(rol.id, visitados=set()))

    async def _assert_no_escalation(
        self, actor_id: UUID, scope_key: str, granted: t.Iterable[str], *, at: datetime
    ) -> None:
        """
        Cada clave de `granted` tiene que estar cubierta por lo que `actor_id` ya tiene.

        Reusa `CompiledPermissionSet.grants` para la comparación, y no sólo para permisos
        exactos: preguntarle al compilado del actor si "concede" la clave `"users.*"` que se
        quiere otorgar funciona igual de bien que para `"users.invite"` —el mismo algoritmo de
        prefijos responde "¿mi comodín es igual o más amplio que éste?"— así que no hace falta
        una segunda lógica de subconjunto de comodines.
        """
        compilado_del_actor = await self.effective_permissions(actor_id, scope_key, at=at)
        faltantes = sorted(p for p in granted if not compilado_del_actor.grants(p))
        if faltantes:
            raise EscalationError(
                f"No podés otorgar lo que no tenés en el scope "
                f"'{scope_key or '(global)'}': {', '.join(faltantes)}."
            )

    # ── Asignaciones ──────────────────────────────────────────────────────────
    async def assign_role(
        self,
        *,
        actor_id: UUID | None,
        user_id: UUID,
        role_id: UUID,
        scope_key: str = GLOBAL_SCOPE,
        expires_at: datetime | None = None,
        at: datetime | None = None,
    ) -> RoleAssignment:
        """
        Asigna el rol. **Anti-escalada**: los permisos efectivos del rol en `scope_key` tienen
        que ser un subconjunto de los de `actor_id` en ese mismo scope.

        `actor_id=None` es un otorgamiento **del sistema** —un seed, un bootstrap, la CLI sin
        `--actor-id`— y **saltea la anti-escalada**. Hace falta: sin esto, nadie podría recibir
        su primer rol en un despliegue nuevo, porque todavía no hay ningún actor con permisos
        efectivos con los que compararlo. No es un agujero silencioso —mismo criterio que
        `system_context()`—, queda auditado en la fila (`granted_by=None` es "lo otorgó el
        sistema, no una persona").

        Raises:
            RoleNotFoundError
            EscalationError: sólo si `actor_id` no es `None`.
        """
        await self.get_role(role_id)
        momento = at or self._clock.now()

        if actor_id is not None:
            permisos_del_rol = await self._walk_role(role_id, visitados=set())
            await self._assert_no_escalation(actor_id, scope_key, permisos_del_rol, at=momento)

        asignacion = await self._user_roles.assign(
            RoleAssignment(
                id=uuid4(),
                user_id=user_id,
                role_id=role_id,
                scope_key=scope_key,
                granted_by=actor_id,
                expires_at=expires_at,
                created_at=momento,
            )
        )
        await self._bump(scope_key, reason="role_assigned")
        return asignacion

    async def revoke_role(self, *, user_id: UUID, role_id: UUID, scope_key: str) -> bool:
        """
        Revoca la asignación. Sin anti-escalada: revocar nunca amplía lo que alguien puede
        hacer.
        """
        revocado = await self._user_roles.revoke(user_id, role_id, scope_key)
        if revocado:
            await self._bump(scope_key, reason="role_revoked")
        return revocado

    # ── El seed de los roles de código ────────────────────────────────────────
    async def sync_system_roles(
        self, registry: "RoleRegistry", *, scope_key: str = GLOBAL_SCOPE
    ) -> None:
        """
        Sincroniza el `RoleRegistry` de código a la tabla, como roles `is_system=True`.

        Idempotente: se puede llamar en cada arranque. El catálogo de permisos se completa con
        `ensure` (nunca duplica), y `set_permissions`/`set_parents` **reemplazan** el
        conjunto —así que si un rol de código perdió un permiso en el último deploy, la fila
        deja de tenerlo también, en vez de acumular lo que alguna vez estuvo declarado.

        No pasa por `_assert_no_escalation`: es el arranque del proceso sincronizando su propia
        fuente de verdad, no un actor pidiendo un privilegio ajeno.
        """
        await self._permissions.ensure(registry.all_permission_values())

        ids_por_nombre: dict[str, UUID] = {}
        for nombre in registry.role_names:
            existente = await self._roles.get_by_name(scope_key, nombre)
            if existente is not None:
                ids_por_nombre[nombre] = existente.id
                continue
            creado = await self._roles.add(
                RbacRole(
                    id=uuid4(),
                    scope_key=scope_key,
                    name=nombre,
                    is_system=True,
                )
            )
            ids_por_nombre[nombre] = creado.id

        for nombre, role_id in ids_por_nombre.items():
            rol_de_codigo = registry.get_role(nombre)
            if rol_de_codigo is None:  # pragma: no cover - defensivo
                continue
            await self._roles.set_permissions(role_id, rol_de_codigo.permissions)
            await self._roles.set_parents(
                role_id, (ids_por_nombre[p] for p in rol_de_codigo.inherits if p in ids_por_nombre)
            )

    # ── Interno ───────────────────────────────────────────────────────────────
    async def _bump(self, scope_key: str, *, reason: str) -> None:
        nueva_version = await self._versions.bump(scope_key)
        if self._events is not None:
            await self._events.publish(
                AuthorizationChangedEvent(
                    scope_key=scope_key, reason=reason, version=nueva_version
                )
            )
