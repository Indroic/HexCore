"""
Darwin — Fase F5 del plan rbac/drbac: `PolicyDecisionPoint`, con repositorios falsos.

Lo que se fija:

1. Una regla `deny` cuya condición da `True` gana, sin importar la prioridad de las `allow`
   candidatas.
2. Sin ningún deny que aplique, la primera `allow` cuya condición da `True` decide.
3. Una condición indeterminada (`None`) —sin que ninguna otra regla haya decidido con
   claridad— deniega (fail-closed), y **no** tapa un allow o un deny que sí se resolvió.
4. La herencia jerárquica de scope: una política en `"org:1"` aplica también a
   `"org:1/proj:2"`.
5. Los bindings de rol contextual: vencidos no cuentan, la condición del binding se evalúa
   contra el contexto **sin** los roles contextuales (no hay ciclos de auto-habilitación), y
   —con `role_permissions` configurado— un rol contextual puede conceder directamente.
6. Sin ninguna regla ni binding que aplique: `not_applicable`, para que el combinador de
   `AuthorizationEngine` siga con el resto de los providers.
7. `PolicySetCache`: la misma capa de scope no se vuelve a compilar si la versión no cambió, y
   sí se recompila cuando cambia.
"""
from __future__ import annotations

import typing as t
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from hexcore.darwin.domain.authorization import AccessRequest, ResourceRef
from hexcore.darwin.domain.context import AuthContext, Principal
from hexcore.darwin.plugins.drbac.cache import PolicySetCache
from hexcore.darwin.plugins.drbac.conditions import And, Const, Eq, In, Var
from hexcore.darwin.plugins.drbac.domain import GLOBAL_SCOPE, Policy, RoleBinding, Rule, scope_chain
from hexcore.darwin.plugins.drbac.pdp import EvaluationLimits, PolicyDecisionPoint

AHORA = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


class _FakePolicies:
    def __init__(self, policies: t.Sequence[Policy] = ()) -> None:
        self.policies = list(policies)
        self.calls: list[tuple[str, ...]] = []

    async def list_enabled_for_scopes(self, scope_keys: t.Iterable[str]) -> list[Policy]:
        claves = tuple(scope_keys)
        self.calls.append(claves)
        conjunto = set(claves)
        return [p for p in self.policies if p.scope_key in conjunto and p.enabled]


class _FakeBindings:
    def __init__(self, bindings: t.Sequence[RoleBinding] = ()) -> None:
        self.bindings = list(bindings)

    async def active_for_subject_at_scopes(
        self, subject_id: t.Any, scope_paths: t.Iterable[str], *, at: datetime
    ) -> list[RoleBinding]:
        conjunto = set(scope_paths)
        return [
            b
            for b in self.bindings
            if b.subject_id == subject_id and b.scope_path in conjunto and not b.is_expired_at(at)
        ]


class _FakeVersions:
    def __init__(self, version: int = 1) -> None:
        self.version = version

    async def get(self, scope_key: str) -> int:
        return self.version

    async def bump(self, scope_key: str) -> int:
        self.version += 1
        return self.version


def _actor(user_id: t.Any, *, scopes: frozenset[str] = frozenset()) -> Principal:
    return Principal(user_id=user_id, scopes=scopes)


def _ctx(actor: Principal) -> AuthContext[t.Any]:
    return AuthContext(actor=actor, subject=actor, transport="cookie")


def _pdp(
    policies: t.Sequence[Policy] = (),
    bindings: t.Sequence[RoleBinding] = (),
    *,
    role_permissions: t.Any = None,
) -> PolicyDecisionPoint:
    return PolicyDecisionPoint(
        policies=_FakePolicies(policies),
        bindings=_FakeBindings(bindings),
        versions=_FakeVersions(),
        role_permissions=role_permissions,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ── deny/allow/indeterminado ───────────────────────────────────────────────────
@pytest.mark.anyio
async def test_deny_gana_aunque_la_allow_tenga_mas_prioridad() -> None:
    owner_id = uuid4()
    reglas = (
        Rule(
            id=uuid4(), position=1, effect="allow", actions=["invoice.approve"],
            resource_type="invoice",
        ),
        Rule(
            id=uuid4(), position=0, effect="deny", actions=["invoice.approve"],
            resource_type="invoice",
            condition=Eq(Var("resource.owner_id"), Var("subject.id")),
        ),
    )
    politica = Policy(id=uuid4(), scope_key="org:1", name="p", rules=reglas)
    pdp = _pdp([politica])

    ctx = _ctx(_actor(owner_id))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", owner_id=owner_id, scope_path="org:1"),
    )
    decision = await pdp.decide(request)
    assert decision.effect == "deny"


@pytest.mark.anyio
async def test_allow_decide_cuando_ningun_deny_aplica() -> None:
    otro = uuid4()
    owner_id = uuid4()
    reglas = (
        Rule(id=uuid4(), effect="allow", actions=["invoice.approve"], resource_type="invoice"),
        Rule(
            id=uuid4(), effect="deny", actions=["invoice.approve"], resource_type="invoice",
            condition=Eq(Var("resource.owner_id"), Var("subject.id")),
        ),
    )
    politica = Policy(id=uuid4(), scope_key="org:1", name="p", rules=reglas)
    pdp = _pdp([politica])

    ctx = _ctx(_actor(otro))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", owner_id=owner_id, scope_path="org:1"),
    )
    decision = await pdp.decide(request)
    assert decision.effect == "allow"


@pytest.mark.anyio
async def test_indeterminado_deniega_fail_closed() -> None:
    regla = Rule(
        id=uuid4(), effect="allow", actions=["invoice.approve"], resource_type="invoice",
        condition=Eq(Var("resource.status"), Const("draft")),
    )
    politica = Policy(id=uuid4(), scope_key="org:1", name="p", rules=(regla,))
    pdp = _pdp([politica])

    ctx = _ctx(_actor(uuid4()))
    # `resource.status` no viene en `attributes` ni lo resuelve ningún PIP: indeterminado.
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", scope_path="org:1"),
    )
    decision = await pdp.decide(request)
    assert decision.effect == "deny"


@pytest.mark.anyio
async def test_indeterminado_no_tapa_un_allow_que_ya_decidio() -> None:
    """Ver el docstring de `PolicyDecisionPoint._evaluar`: el orden es deny-claro > allow-claro
    > indeterminado > no-aplica, así que un allow que ya se resolvió gana sobre un deny que no
    se pudo evaluar."""
    reglas = (
        Rule(id=uuid4(), effect="allow", actions=["invoice.approve"], resource_type="invoice"),
        Rule(
            id=uuid4(), effect="deny", actions=["invoice.approve"], resource_type="invoice",
            condition=Eq(Var("resource.status"), Const("draft")),
        ),
    )
    politica = Policy(id=uuid4(), scope_key="org:1", name="p", rules=reglas)
    pdp = _pdp([politica])

    ctx = _ctx(_actor(uuid4()))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", scope_path="org:1"),
    )
    decision = await pdp.decide(request)
    assert decision.effect == "allow"


@pytest.mark.anyio
async def test_sin_ninguna_regla_aplicable_es_not_applicable() -> None:
    pdp = _pdp()
    ctx = _ctx(_actor(uuid4()))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", scope_path="org:1"),
    )
    decision = await pdp.decide(request)
    assert decision.effect == "not_applicable"


@pytest.mark.anyio
async def test_una_regla_de_otro_resource_type_no_es_candidata() -> None:
    regla = Rule(id=uuid4(), effect="allow", actions=["invoice.approve"], resource_type="invoice")
    politica = Policy(id=uuid4(), scope_key="org:1", name="p", rules=(regla,))
    pdp = _pdp([politica])

    ctx = _ctx(_actor(uuid4()))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="user", id="1", scope_path="org:1"),
    )
    decision = await pdp.decide(request)
    assert decision.effect == "not_applicable"


@pytest.mark.anyio
async def test_comodin_en_actions_matchea() -> None:
    regla = Rule(id=uuid4(), effect="deny", actions=["invoice.*"], resource_type="invoice")
    politica = Policy(id=uuid4(), scope_key="org:1", name="p", rules=(regla,))
    pdp = _pdp([politica])

    ctx = _ctx(_actor(uuid4()))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", scope_path="org:1"),
    )
    decision = await pdp.decide(request)
    assert decision.effect == "deny"


# ── Herencia jerárquica de scope ────────────────────────────────────────────────
@pytest.mark.anyio
async def test_una_politica_en_org_aplica_a_su_descendiente() -> None:
    regla = Rule(id=uuid4(), effect="deny", actions=["invoice.approve"], resource_type="invoice")
    politica = Policy(id=uuid4(), scope_key="org:1", name="p", rules=(regla,))
    pdp = _pdp([politica])

    ctx = _ctx(_actor(uuid4()))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", scope_path="org:1/proj:2/task:3"),
    )
    decision = await pdp.decide(request)
    assert decision.effect == "deny"


@pytest.mark.anyio
async def test_una_politica_en_una_rama_no_aplica_a_otra() -> None:
    regla = Rule(id=uuid4(), effect="deny", actions=["invoice.approve"], resource_type="invoice")
    politica = Policy(id=uuid4(), scope_key="org:1/proj:7", name="p", rules=(regla,))
    pdp = _pdp([politica])

    ctx = _ctx(_actor(uuid4()))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", scope_path="org:1/proj:8"),
    )
    decision = await pdp.decide(request)
    assert decision.effect == "not_applicable"


def test_scope_chain_incluye_global_y_cada_prefijo() -> None:
    assert scope_chain("org:1/proj:2") == ("", "org:1", "org:1/proj:2")
    assert scope_chain(GLOBAL_SCOPE) == ("",)


# ── Bindings de rol contextual ──────────────────────────────────────────────────
@pytest.mark.anyio
async def test_binding_vencido_no_cuenta() -> None:
    uid = uuid4()

    async def permisos(scope_key: str, role_name: str) -> frozenset[str] | None:
        return frozenset({"invoice.*"}) if role_name == "temp_admin" else None

    vencido = RoleBinding(
        id=uuid4(), subject_id=uid, role_name="temp_admin", scope_path="org:1",
        expires_at=AHORA - timedelta(days=1),
    )
    pdp = _pdp(bindings=[vencido], role_permissions=permisos)

    ctx = _ctx(_actor(uid))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", scope_path="org:1"),
    )
    decision = await pdp.decide(request)
    assert decision.effect == "not_applicable"


@pytest.mark.anyio
async def test_binding_vigente_con_role_permissions_concede() -> None:
    uid = uuid4()

    async def permisos(scope_key: str, role_name: str) -> frozenset[str] | None:
        return frozenset({"invoice.*"}) if role_name == "temp_admin" else None

    binding = RoleBinding(
        id=uuid4(), subject_id=uid, role_name="temp_admin", scope_path="org:1", expires_at=None
    )
    pdp = _pdp(bindings=[binding], role_permissions=permisos)

    ctx = _ctx(_actor(uid))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", scope_path="org:1"),
    )
    decision = await pdp.decide(request)
    assert decision.effect == "allow"


@pytest.mark.anyio
async def test_sin_role_permissions_el_binding_no_concede_nada_por_si_solo() -> None:
    uid = uuid4()
    binding = RoleBinding(
        id=uuid4(), subject_id=uid, role_name="temp_admin", scope_path="org:1", expires_at=None
    )
    pdp = _pdp(bindings=[binding])  # sin `role_permissions`

    ctx = _ctx(_actor(uid))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", scope_path="org:1"),
    )
    decision = await pdp.decide(request)
    assert decision.effect == "not_applicable"


@pytest.mark.anyio
async def test_binding_visible_en_subject_roles_para_una_condicion() -> None:
    """Un binding contextual, sin `role_permissions`, sigue siendo visible en
    `subject.roles` — una regla puede usarlo directamente en su condición."""
    uid = uuid4()
    binding = RoleBinding(
        id=uuid4(), subject_id=uid, role_name="accountant", scope_path="org:1", expires_at=None
    )
    regla = Rule(
        id=uuid4(), effect="allow", actions=["invoice.approve"], resource_type="invoice",
        condition=In(Const("accountant"), Var("subject.roles")),
    )
    politica = Policy(id=uuid4(), scope_key="org:1", name="p", rules=(regla,))
    pdp = _pdp([politica], bindings=[binding])

    ctx = _ctx(_actor(uid))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", scope_path="org:1"),
    )
    decision = await pdp.decide(request)
    assert decision.effect == "allow"


@pytest.mark.anyio
async def test_condicion_del_binding_se_evalua_sin_los_roles_contextuales() -> None:
    """Dos bindings que se habilitarían el uno al otro no forman un ciclo: la condición de
    cada binding se evalúa contra el contexto **sin** ningún rol contextual todavía."""
    uid = uuid4()
    binding_a = RoleBinding(
        id=uuid4(), subject_id=uid, role_name="rol_a", scope_path="org:1", expires_at=None,
        condition=In(Const("rol_b"), Var("subject.roles")),
    )
    regla = Rule(
        id=uuid4(), effect="allow", actions=["invoice.approve"], resource_type="invoice",
        condition=In(Const("rol_a"), Var("subject.roles")),
    )
    politica = Policy(id=uuid4(), scope_key="org:1", name="p", rules=(regla,))
    pdp = _pdp([politica], bindings=[binding_a])

    ctx = _ctx(_actor(uid))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", scope_path="org:1"),
    )
    decision = await pdp.decide(request)
    # `binding_a` exige `rol_b`, que nunca aparece: `binding_a` nunca se activa, y por lo tanto
    # la regla que exige `rol_a` tampoco.
    assert decision.effect == "not_applicable"


# ── `PolicySetCache` ─────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_la_misma_version_no_recompila_la_capa() -> None:
    regla = Rule(id=uuid4(), effect="deny", actions=["invoice.approve"], resource_type="invoice")
    politica = Policy(id=uuid4(), scope_key="org:1", name="p", rules=(regla,))
    fake_policies = _FakePolicies([politica])
    pdp = PolicyDecisionPoint(
        policies=fake_policies, bindings=_FakeBindings(), versions=_FakeVersions(version=1)
    )

    ctx = _ctx(_actor(uuid4()))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", scope_path="org:1"),
    )
    await pdp.decide(request)
    llamadas_previas = len(fake_policies.calls)
    await pdp.decide(request)
    assert len(fake_policies.calls) == llamadas_previas  # cacheado, no volvió a consultar


@pytest.mark.anyio
async def test_bump_de_version_fuerza_recompilar() -> None:
    regla = Rule(id=uuid4(), effect="deny", actions=["invoice.approve"], resource_type="invoice")
    politica = Policy(id=uuid4(), scope_key="org:1", name="p", rules=(regla,))
    fake_policies = _FakePolicies([politica])
    fake_versions = _FakeVersions(version=1)
    pdp = PolicyDecisionPoint(policies=fake_policies, bindings=_FakeBindings(), versions=fake_versions)

    ctx = _ctx(_actor(uuid4()))
    request = AccessRequest(
        context=ctx, action="invoice.approve",
        resource=ResourceRef(type="invoice", id="1", scope_path="org:1"),
    )
    await pdp.decide(request)
    llamadas_previas = len(fake_policies.calls)
    await fake_versions.bump("org:1")
    await pdp.decide(request)
    assert len(fake_policies.calls) > llamadas_previas


@pytest.mark.anyio
async def test_policy_set_cache_clear_local() -> None:
    cache = PolicySetCache()
    llamado = False

    async def compute() -> tuple[t.Any, ...]:
        nonlocal llamado
        llamado = True
        return ()

    await cache.get_or_compute("org:1", 1, compute=compute)
    assert llamado is True

    llamado = False
    await cache.get_or_compute("org:1", 1, compute=compute)
    assert llamado is False  # cacheado

    cache.clear_local()
    llamado = False
    await cache.get_or_compute("org:1", 1, compute=compute)
    assert llamado is True  # se limpió
