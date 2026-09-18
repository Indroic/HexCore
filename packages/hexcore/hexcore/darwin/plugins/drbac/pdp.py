"""
`PolicyDecisionPoint`: el motor de `drbac`. Junta políticas, bindings de rol contextual y
atributos del recurso (PIP), y produce una `Decision` para `DrbacAuthorizationProvider`.

Flujo de `decide()`, resumido (el detalle está en cada método):

1. `scope_chain(resource.scope_path)` — todos los ancestros del scope del recurso, del más
   general al más específico.
2. Por cada capa de esa cadena, las reglas ya compiladas de sus políticas habilitadas —
   `PolicySetCache`, versionado por `AbstractDrbacAuthzVersionRepository`—, concatenadas.
3. Filtro barato: sólo las reglas cuyo `resource_type` y `actions` coinciden con el pedido
   (`Permission.grants`, del núcleo — no el matcher de `rbac`, que es de otro plugin).
4. Contexto de evaluación (Fase F4): `subject` con los roles del token más los de cualquier
   `RoleBinding` vigente y sin vencer en la cadena de scope, `resource` completado por el PIP,
   `env` con `now` y lo que haya en `AccessRequest.environment`.
5. Evaluar las reglas candidatas: **deny primero** (cualquier deny cuya condición dé `True`
   gana), si no **allow** (la primera cuya condición dé `True`), si no y **hubo alguna
   indeterminada** se deniega (fail-closed), si no, `not_applicable` — DRBAC no tiene nada que
   decir y el combinador de `AuthorizationEngine` sigue con el resto de los providers.
6. Un rol contextual vigente también puede conceder directamente, vía
   `RolePermissionsResolver` — ver su docstring: es la única forma en que DRBAC "expande" un
   rol, y no importa nada de `rbac` para hacerlo.

**Presupuesto.** `EvaluationLimits.timeout_ms` mide cuánto tarda **evaluar** las reglas ya
compiladas —una llamada sincrónica, sin I/O— y lo loguea si se excede; no cancela nada, porque
no hay nada async ahí que `asyncio.wait_for` pueda interrumpir de verdad. La protección real
contra un árbol de condición enorme es `ConditionLimits` (Fase F4), aplicada al **guardar** una
política y de nuevo al compilar cada capa. Deliberadamente **no** hay ningún timeout sobre la
consulta al repositorio (`_reglas_candidatas`): su latencia depende del backend de
almacenamiento, no del tamaño de ninguna política, así que acotarla con el mismo presupuesto
confundiría "la base tardó" con "la política es maliciosa" — y bajo carga real (CI, un pool de
conexiones frío) esa confusión es un `deny` intermitente sobre un request perfectamente sano.
"""
from __future__ import annotations

import logging
import time
import typing as t
from dataclasses import dataclass
from datetime import datetime

from hexcore.darwin.domain.authorization import AccessRequest, Decision
from hexcore.darwin.domain.context import Principal
from hexcore.darwin.domain.permissions import Permission
from hexcore.darwin.plugins.drbac.conditions import (
    ConditionEvaluator,
    ConditionLimits,
    EvaluationContext,
    PredicateRegistry,
    compile_condition,
    default_predicate_registry,
    evaluate_condition,
)
from hexcore.darwin.plugins.drbac.domain import GLOBAL_SCOPE, Rule, scope_chain

if t.TYPE_CHECKING:
    from hexcore.darwin.domain.ports import AbstractClock
    from hexcore.darwin.plugins.drbac.cache import PolicySetCache
    from hexcore.darwin.plugins.drbac.domain import (
        AbstractDrbacAuthzVersionRepository,
        AbstractDrbacPolicyRepository,
        AbstractDrbacRoleBindingRepository,
    )
    from hexcore.darwin.plugins.drbac.pip import PolicyInformationPoint

__all__ = ["EvaluationLimits", "RolePermissionsResolver", "CompiledRule", "PolicyDecisionPoint"]

logger = logging.getLogger("hexcore.darwin.drbac")


@dataclass(frozen=True, slots=True)
class EvaluationLimits:
    """
    Los presupuestos de una decisión de DRBAC.

    `max_nodes`/`max_depth` son los mismos límites estructurales que `ConditionLimits` (Fase
    F4) — acá viven de nuevo porque el PDP los aplica de nuevo, defensivamente, al compilar cada
    capa (una política pudo haber llegado a la base por otro camino que el de guardado normal:
    una migración, una carga masiva). `timeout_ms` sólo mide cuánto tarda **evaluar** las reglas
    ya compiladas —sin cancelar nada, ver el docstring del módulo—, así que es una señal para
    los logs y no una defensa por sí sola: la defensa real contra "muchas reglas en el mismo
    scope" sigue siendo el tamaño de cada árbol, acotado por `max_nodes`/`max_depth`.
    """

    max_nodes: int = 256
    max_depth: int = 16
    timeout_ms: int = 50


#: Cómo DRBAC expande un `RoleBinding.role_name` a los permisos que ese rol concede en
#: `scope_key`. `None` (el default) es "no hay expansión": los bindings contextuales siguen
#: visibles en `EvaluationContext.subject["roles"]` para que una condición los use
#: (``In(Var("subject.roles"), ["accountant"])``), pero no conceden nada por sí solos.
#:
#: Sin este resolver DRBAC no puede saber qué permisos tiene un rol —ésa es información de
#: `rbac`, y los dos plugins no se importan entre sí (ver el docstring de
#: `domain.RoleBinding`)—, así que quien cablea los dos pasa esto explícitamente:
#:
#:     DrbacPlugin(role_permissions=rbac_plugin.service().permission_keys_for_role_name)
RolePermissionsResolver = t.Callable[[str, str], t.Awaitable["frozenset[str] | None"]]


@dataclass(frozen=True, slots=True)
class CompiledRule:
    """Una `Rule` ya compilada, más de qué capa de `scope_chain()` vino (para el `explain`)."""

    rule: Rule
    scope_key: str
    evaluator: ConditionEvaluator | None  # `None` = sin condición, siempre aplica.

    def evaluate(self, context: EvaluationContext) -> bool | None:
        return True if self.evaluator is None else self.evaluator(context)


def _permission_matches(actions: t.Sequence[str], required: str) -> bool:
    """
    Si alguna de `actions` concede `required`, con la misma semántica de comodines que el resto
    de Darwin (`"invoice.*"`, `"*"`).

    Usa `Permission` del **núcleo** (`domain/permissions.py`) y no `rbac.matcher`: éste último
    vive adentro del plugin `rbac`, y `drbac` no lo importa — ver el docstring del módulo.
    `actions` de una regla es una lista corta (unas pocas entradas), así que el recorrido lineal
    de `Permission.grants` no necesita la precompilación que sí vale la pena para el conjunto de
    permisos, potencialmente grande, de `rbac`.
    """
    return any(Permission(value=a).grants(required) for a in actions)


class PolicyDecisionPoint:
    """
    Uso::

        pdp = PolicyDecisionPoint(
            policies=repos.PolicyRepository(),
            bindings=repos.RoleBindingRepository(),
            versions=repos.AuthzVersionRepository(),
            pip=PolicyInformationPoint(resolvers={"invoice": InvoiceAttributes(...)}),
            role_permissions=rbac_plugin.service().permission_keys_for_role_name,
        )
        decision = await pdp.decide(access_request)
    """

    def __init__(
        self,
        *,
        policies: "AbstractDrbacPolicyRepository",
        bindings: "AbstractDrbacRoleBindingRepository",
        versions: "AbstractDrbacAuthzVersionRepository",
        pip: "PolicyInformationPoint | None" = None,
        predicates: PredicateRegistry | None = None,
        role_permissions: RolePermissionsResolver | None = None,
        limits: EvaluationLimits = EvaluationLimits(),
        cache: "PolicySetCache | None" = None,
        clock: "AbstractClock | None" = None,
    ) -> None:
        self._policies = policies
        self._bindings = bindings
        self._versions = versions
        self._pip = pip
        self._predicates = predicates or default_predicate_registry()
        self._role_permissions = role_permissions
        self._limits = limits
        self._clock = clock
        if cache is None:
            from hexcore.darwin.plugins.drbac.cache import PolicySetCache

            cache = PolicySetCache()
        self._cache = cache

    def _reloj(self) -> "AbstractClock":
        if self._clock is not None:
            return self._clock
        from hexcore.darwin.infrastructure.clock import SystemClock

        return SystemClock()

    async def decide(self, request: AccessRequest) -> Decision:
        actor = request.context.actor
        scope_path = (
            request.resource.scope_path if request.resource is not None else GLOBAL_SCOPE
        )
        resource_type = request.resource.type if request.resource is not None else ""
        cadena = scope_chain(scope_path)
        ahora = self._reloj().now()

        # Sin `asyncio.wait_for` acá: esta llamada es I/O de verdad (una consulta al
        # repositorio en cada scope todavía no cacheado), y su latencia depende del backend de
        # almacenamiento, no del tamaño de ninguna política — cortarla con el mismo presupuesto
        # que protege contra un árbol de condición enorme confundiría "la base tardó" con "la
        # política es maliciosa". Lo que sí acota el tamaño de lo que se compila acá es
        # `ConditionLimits` (Fase F4), aplicado en `_compilar_capa`.
        candidatas = await self._reglas_candidatas(cadena, request.action, resource_type)

        permiso_contextual = await self._permiso_via_rol_contextual(
            actor, cadena, request.action, at=ahora
        )

        if not candidatas:
            if permiso_contextual:
                return Decision(effect="allow", provider="drbac")
            return Decision(effect="not_applicable", provider="drbac")

        contexto = await self._contexto_de_evaluacion(request, actor, cadena, at=ahora)

        # `_evaluar` es sincrónico —closures ya compiladas, sin I/O—, así que no hay nada que
        # `asyncio.wait_for` pueda interrumpir de verdad ahí: un `wait_for` sobre una llamada
        # sync sólo mide cuánto tardó **después** de que ya terminó. Por eso el presupuesto acá
        # es una medición con log, no una cancelación — la protección real contra un árbol
        # gigante es `ConditionLimits` (Fase F4), aplicado al compilar cada capa.
        inicio = time.perf_counter()
        resultado = self._evaluar(candidatas, contexto)
        transcurrido_ms = (time.perf_counter() - inicio) * 1000
        if transcurrido_ms > self._limits.timeout_ms:
            logger.warning(
                "drbac: evaluar %d regla(s) para la acción %r tardó %.1fms, más que el "
                "presupuesto de %sms.",
                len(candidatas),
                request.action,
                transcurrido_ms,
                self._limits.timeout_ms,
            )

        if resultado.effect == "not_applicable" and permiso_contextual:
            return Decision(effect="allow", provider="drbac")
        return resultado

    # ── Reglas candidatas, compiladas y cacheadas por capa ────────────────────
    async def _reglas_candidatas(
        self, cadena: tuple[str, ...], action: str, resource_type: str
    ) -> list[CompiledRule]:
        resultado: list[CompiledRule] = []
        for scope_key in cadena:
            for compilada in await self._capa(scope_key):
                if compilada.rule.resource_type != resource_type:
                    continue
                if _permission_matches(compilada.rule.actions, action):
                    resultado.append(compilada)
        return resultado

    async def _capa(self, scope_key: str) -> tuple[CompiledRule, ...]:
        version = await self._versions.get(scope_key)
        return await self._cache.get_or_compute(
            scope_key, version, compute=lambda: self._compilar_capa(scope_key)
        )

    async def _compilar_capa(self, scope_key: str) -> tuple[CompiledRule, ...]:
        limites = ConditionLimits(
            max_nodes=self._limits.max_nodes, max_depth=self._limits.max_depth
        )
        politicas = await self._policies.list_enabled_for_scopes([scope_key])
        compiladas: list[CompiledRule] = []
        for politica in sorted(politicas, key=lambda p: p.priority):
            for regla in politica.rules:
                evaluador = (
                    compile_condition(regla.condition, predicates=self._predicates, limits=limites)
                    if regla.condition is not None
                    else None
                )
                compiladas.append(
                    CompiledRule(rule=regla, scope_key=scope_key, evaluator=evaluador)
                )
        return tuple(compiladas)

    # ── Evaluación: deny primero, luego allow, luego indeterminado fail-closed ─
    @staticmethod
    def _evaluar(candidatas: list[CompiledRule], context: EvaluationContext) -> Decision:
        # Deny primero (como grupo, no sólo por posición): un deny cuya condición no se pudo
        # decidir no debería bloquear un allow que sí se resolvió con claridad, ni al revés —
        # el orden textual del plan es "deny aplica" > "allow aplica" > "indeterminado" >
        # "no aplica", y ese es el orden en el que este método los chequea.
        ordenadas = sorted(candidatas, key=lambda c: (c.rule.effect != "deny", c.rule.position))

        vio_indeterminado = False
        for compilada in ordenadas:
            resultado = compilada.evaluate(context)
            if resultado is None:
                vio_indeterminado = True
                continue
            if not resultado:
                continue
            return Decision(
                effect=compilada.rule.effect,
                provider="drbac",
                policy_id=str(compilada.rule.id),
            )

        if vio_indeterminado:
            return Decision(
                effect="deny",
                provider="drbac",
                reason="una condición no se pudo resolver (fail-closed).",
            )
        return Decision(effect="not_applicable", provider="drbac")

    # ── Contexto de evaluación: subject/resource/env ──────────────────────────
    async def _contexto_de_evaluacion(
        self,
        request: AccessRequest,
        actor: t.Any,
        cadena: tuple[str, ...],
        *,
        at: datetime,
    ) -> EvaluationContext:
        resource: dict[str, t.Any] = {}
        if request.resource is not None:
            if self._pip is not None:
                resource = dict(await self._pip.attributes_for(request.resource))
            else:
                resource = dict(request.resource.attributes)
            resource.setdefault("type", request.resource.type)
            resource.setdefault("id", request.resource.id)
            resource.setdefault(
                "owner_id", str(request.resource.owner_id) if request.resource.owner_id else None
            )
            resource.setdefault("scope_path", request.resource.scope_path)

        env: dict[str, t.Any] = dict(request.environment)
        env.setdefault("now", at)

        subject: dict[str, t.Any] = {}
        roles_del_token: frozenset[str] = frozenset()
        if isinstance(actor, Principal):
            subject["id"] = str(actor.user_id)
            subject["email"] = actor.email
            roles_del_token = actor.roles

        roles_contextuales = await self._roles_contextuales(
            actor, cadena, subject=subject, resource=resource, env=env, at=at
        )
        subject["roles"] = sorted(roles_del_token | roles_contextuales)

        return EvaluationContext(subject=subject, resource=resource, env=env)

    async def _roles_contextuales(
        self,
        actor: t.Any,
        cadena: tuple[str, ...],
        *,
        subject: t.Mapping[str, t.Any],
        resource: t.Mapping[str, t.Any],
        env: t.Mapping[str, t.Any],
        at: datetime,
    ) -> frozenset[str]:
        if not isinstance(actor, Principal):
            return frozenset()

        ligaduras = await self._bindings.active_for_subject_at_scopes(
            actor.user_id, cadena, at=at
        )
        resultado: set[str] = set()
        for ligadura in ligaduras:
            if ligadura.is_expired_at(at):
                continue
            if ligadura.condition is not None:
                # Con `subject.roles` todavía sin los contextuales: el binding se evalúa contra
                # lo que el actor tenía **antes** de este binding, para que dos bindings no
                # puedan habilitarse el uno al otro en un ciclo.
                contexto_previo = EvaluationContext(subject=subject, resource=resource, env=env)
                if evaluate_condition(ligadura.condition, contexto_previo) is not True:
                    continue
            resultado.add(ligadura.role_name)
        return frozenset(resultado)

    async def _permiso_via_rol_contextual(
        self, actor: t.Any, cadena: tuple[str, ...], action: str, *, at: datetime
    ) -> bool:
        """
        Si algún rol contextual vigente del actor concede `action` directamente, vía
        `RolePermissionsResolver`. `False` sin resolver configurado, o sin `rbac` cableado —
        nunca un error: es una ampliación opcional, igual que la integración con
        `organization` del provider de `rbac`.
        """
        if self._role_permissions is None or not isinstance(actor, Principal):
            return False

        ligaduras = await self._bindings.active_for_subject_at_scopes(
            actor.user_id, cadena, at=at
        )
        for ligadura in ligaduras:
            if ligadura.is_expired_at(at):
                continue
            if ligadura.condition is not None:
                contexto = EvaluationContext(subject={"id": str(actor.user_id)})
                if evaluate_condition(ligadura.condition, contexto) is not True:
                    continue
            try:
                permisos = await self._role_permissions(ligadura.scope_path, ligadura.role_name)
            except Exception:
                logger.warning(
                    "drbac: RolePermissionsResolver falló para el rol contextual '%s'; se "
                    "ignora ese binding.",
                    ligadura.role_name,
                    exc_info=True,
                )
                continue
            if permisos and _permission_matches(tuple(permisos), action):
                return True
        return False
