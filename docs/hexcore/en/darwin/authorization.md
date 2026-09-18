# Authorization: `rbac`, `drbac` and the engine

Darwin has always had a cheap way to gate an action: `Principal.scopes` plus `require_scopes`,
an exact string match against whatever a `AbstractPrincipalResolver` put in the token. That is
still there, and for a small app it can be everything you need. What it cannot do is answer
"can this actor approve *this* invoice" — a decision that depends on the resource, not only on
what the actor carries — or let an admin assign roles through an API instead of a code
deployment.

That is what this page covers: `AuthorizationEngine` (the core contract, always available), and
the two plugins that use it — `rbac` (persisted, assignable roles and permissions) and `drbac`
(conditional rules and contextual, time-limited role bindings on top of `rbac`).

```sh
pip install 'hexcore[darwin-rbac]'    # roles and permissions
pip install 'hexcore[darwin-drbac]'   # + conditional policies
```

---

## `AuthorizationEngine`: one question, several providers

The core's contract is agnostic of who answers. An `AuthorizationProvider` decides `allow`,
`deny`, or `not_applicable` for an `AccessRequest`, and `AuthorizationEngine` combines every
registered provider with **deny-overrides + default-deny**:

1. Any `deny` wins, no matter how many providers said `allow`.
2. With no `deny`, the first `allow` wins.
3. If every provider answered `not_applicable` — including zero providers registered — the
   engine denies. Nothing in this module reads silence as permission.
4. A provider that raises is treated as `deny` and logged, never as a pass.

`not_applicable` is what lets several providers coexist without stepping on each other: a
`drbac` provider that only knows about `invoice.*` has no business answering `user.delete`, and
answering `deny` there would let an unrelated policy shadow `rbac`'s `allow` under
deny-overrides.

```python
from hexcore.darwin.infrastructure.api.authorization import require_permission
from hexcore.darwin.domain.authorization import ResourceRef

async def load_invoice_ref(request) -> ResourceRef:
    invoice = await get_invoice(request.path_params["invoice_id"])
    return ResourceRef(type="invoice", id=str(invoice.id), owner_id=invoice.owner_id)

@router.post(
    "/invoices/{invoice_id}/approve",
    dependencies=[Depends(require_permission("invoice.approve", resource=load_invoice_ref))],
)
async def approve(invoice_id: UUID): ...
```

For CQRS there is `@authorize_command(action, resource_from=...)` (a decorator that stamps
metadata read by `AuthorizationMiddleware`), and for imperative code inside a handler there is
`await authorize("invoice.update", ResourceRef(...))`, which uses the actor already in
`AuthContext` and raises `AccessDeniedError` — a 403 whose body carries only `required`, never
the reason a policy lost. Explaining the reason in the response would hand anyone probing the
system a map of what exists inside.

Without any plugin registered, the only provider is `ScopeAuthorizationProvider` — the
retrocompatible path over `Principal.scopes`, now with wildcard support
(`Permission.grants`) that plain `require_scopes` never had.

---

## `rbac`: roles and permissions, persisted and assignable

```python
from hexcore.darwin.plugins.rbac import RbacPlugin
from hexcore.darwin.domain.permissions import RoleRegistry

roles = (RoleRegistry()
    .register_role("viewer", permissions={"invoice.read"})
    .register_role("accountant", permissions={"invoice.create", "invoice.approve"}, inherits={"viewer"})
    .register_role("admin", permissions={"invoice.*", "authz.*"}, inherits={"accountant"}))

rbac = RbacPlugin(registry=roles, org_role_mapping={"owner": "admin", "member": "viewer"})
configure_identity(
    IdentityConfig(),
    plugins=[rbac],
    principals=rbac.principal_resolver(),   # populates Principal.roles at sign-in and refresh
)
```

`RoleRegistry` roles ("code roles") stay the source of truth for what a role **means**.
`RbacPlugin.startup_steps()` seeds them into the table as `is_system=True` — not editable
through `PATCH`/`DELETE /roles/{id}`, because a code role can be hardcoded in your app's routes
and hooks, and a panel that lets someone delete it cannot see that. What the table adds on top
is roles **per tenant**, assignments **with expiry**, and a decision point.

### Scope, the tenancy convention

Every table carries `scope_key: str`, and it is **never `NULL`** — `""` means global. A
`UNIQUE` constraint with `NULL` does not reject duplicates in SQL or in Mongo (every `NULL` is
distinct from itself), so `""` is what makes `UNIQUE(scope_key, name)` mean something.

`RbacAuthorizationProvider` does **not** walk scope hierarchy: `"org:1/proj:2"` is an exact key,
not a prefix that also checks `"org:1"`. If you need "a role assigned at the org level also
applies to its projects", that hierarchical lookup is what `drbac`'s contextual role bindings
give you — see below.

### Anti-escalation

Nobody — not even an actor with `authz.manage` — can grant a role or a permission that exceeds
what they themselves hold effectively in that scope, whether by editing a role's direct
permissions, changing who it inherits from, or assigning it to someone else:

```python
await service.set_role_permissions(
    actor_id=admin.id, role_id=role.id, permission_keys=["invoice.approve"],
)
# raises EscalationError if `admin` does not already have `invoice.approve` (or a wildcard
# that covers it) in that role's scope.
```

> **The bootstrap problem, and its one sanctioned bypass.** In a fresh deployment nobody has
> any effective permission yet, so the very first `assign_role` has nothing to compare against.
> `assign_role(actor_id=None, ...)` is the escape hatch — a system grant, skipping
> anti-escalation entirely — meant for a seed script or the CLI without `--actor-id`. It is not
> a silent hole: the assignment is audited with `granted_by=None`, which reads as "the system
> granted this, not a person" in any audit trail.

### Invalidation without waiting on a TTL

Every mutation that can change what someone can do bumps `darwin_authz_version` for that scope
in the same operation. The permission matrix cache's key carries the version
(`darwin:rbac:{scope}:v{version}:{user_id}`), so revoking a role does not require deleting a
cache entry — the old key is simply never asked for again, and the LRU or the shared `ICache`
evicts it on its own schedule. The next request that resolves that user's permissions in that
scope gets the current answer.

### `embed_in_token`

`RbacPrincipalResolver` decides how fat the token gets:

- `"roles"` (default): only role **names** travel in `Principal.roles`. Permissions are
  resolved server-side, cached, on every `AuthorizationEngine.decide()`.
- `"roles_and_permissions"`: effective permissions also land in `Principal.scopes`, so the
  retrocompatible `ScopeAuthorizationProvider` sees them too — useful while migrating a
  deployment that still uses `require_scopes` on some routes.

### CLI

```sh
hexcore darwin rbac sync            # re-seeds RoleRegistry into the table
hexcore darwin rbac list --scope org:1
hexcore darwin rbac assign --user <id> --role <id> --scope org:1
```

`rbac_cli` is a standalone `typer.Typer` you mount yourself — the core cannot import a plugin by
name, so nothing wires it into `hexcore`'s own command tree automatically.

---

## `drbac`: conditions, on top of `rbac`

```python
from hexcore.darwin.plugins.drbac import DrbacPlugin

drbac = DrbacPlugin(
    resolvers={"invoice": InvoiceAttributes(uow_scope)},   # the PIP, see below
    role_permissions=rbac.service().permission_keys_for_role_name,
)
configure_identity(IdentityConfig(), plugins=[rbac, drbac], principals=rbac.principal_resolver())
```

`DrbacPlugin.requires = ("rbac",)` — DRBAC extends RBAC, it does not replace it. And yet **no
module under `hexcore.darwin.plugins.drbac` imports anything from `hexcore.darwin.plugins.rbac`**.
`requires` only orders plugin registration; it is validated by name, never by import. A
`RoleBinding.role_name` is a bare string, not a foreign key into `rbac`'s tables, and the one
real bridge — expanding a contextual role name into the permissions it grants — is the
`role_permissions` callable above, wired by *you*, the same pattern `rbac`'s own optional
`organization` integration already uses. Two plugins genuinely not knowing about each other is
what lets you install one without the other ever being pulled in.

### Policies and rules

A `Policy` lives in a scope, has a priority, and holds an ordered list of `Rule`s:

```python
await drbac_service.create_policy(
    scope_key="org:42",
    name="no-self-approval",
    rules=[{
        "effect": "deny",
        "actions": ["invoice.approve"],
        "resource_type": "invoice",
        "condition": Eq(Var("resource.owner_id"), Var("subject.id")),
    }],
)
```

`actions` uses the same `"resource.action"` / `"resource.*"` / `"*"` wildcard form as
`Permission.grants` everywhere else in Darwin.

### The condition language

A declarative AST, on purpose instead of `eval()` on a string or a hand-rolled DSL: it is data,
so it serializes to JSON as-is, validates itself on construction, and shares one shape with the
TypeScript client. There is no path from a stored condition to arbitrary code execution.

| Node | Means |
| :-- | :-- |
| `Eq`, `Ne`, `Gt`, `Gte`, `Lt`, `Lte` | Comparisons |
| `In`, `Contains` | Membership, each other's mirror |
| `StartsWith` | String prefix |
| `WithinScope` | `left` is at or below the `right` ancestor, **by path segment** |
| `TimeBetween` | `start <= value <= end` |
| `And`, `Or`, `Not` | Combinators, three-valued (see below) |
| `Var` | Reads `subject.*` / `resource.*` / `env.*` — nothing else, checked when the `Var` is built |
| `Const` | A literal |
| `Predicate` | A named escape hatch for what the AST cannot express (see below) |

**Tri-state, Kleene logic — the same rules as SQL's `NULL`.** `true`/`false` decide; a `Var`
that cannot resolve, a predicate nobody registered, or incomparable types are `None`
("indeterminate") — never guessed toward `allow`. `And`/`Or` propagate it exactly like SQL: a
`false` in an `And`, or a `true` in an `Or`, dominates any sibling's indeterminacy.

> **`WithinScope` compares by segment, never by raw string prefix.** `"org:4"` is not an
> ancestor of `"org:42/..."` just because the string happens to be a prefix of it — that
> specific bug is exactly the cross-tenant escalation in the risk table below.

A `Predicate("business_hours")` resolves, by name, to a function registered explicitly in this
process with `@registry.predicate("business_hours")`. Not finding one registered is
indeterminate, never an error that tumbles the request. Predicates only ever run on the server
— a rule whose condition uses one is never `client_evaluable`, forced to `False` regardless of
what was declared when the policy was saved, because the browser has no way to know what a
predicate does without running the same Python.

Size limits (`ConditionLimits`, 256 nodes / 16 levels by default) are enforced **when a policy
is saved**, not only when it is evaluated — a policy is written once and evaluated on every
matching request, so refusing an oversized tree only at evaluation time would already have paid
the cost of persisting and indexing it.

### Scope hierarchy and contextual role bindings

Unlike `rbac`'s flat `scope_key`, `drbac` resolves the whole ancestor chain of a resource's
`scope_path`: a policy at `"org:1"` also applies to `"org:1/proj:2"`. A `RoleBinding` layers a
contextual, possibly time-limited role on top of that hierarchy — a role that only exists in a
given scope and its descendants, optionally gated by its own condition, and never touching
`Principal.roles` in the JWT.

### How a decision is made

1. `scope_chain(resource.scope_path)` — every ancestor, most general to most specific.
2. Per layer, the compiled, cached rules of its enabled policies
   (`PolicySetCache`, versioned by a `drbac`-only authz-version counter — its own table, not
   `rbac`'s, again because the two plugins do not share one).
3. Cheap filter: only rules whose `resource_type` and `actions` match the request.
4. The evaluation context: `subject` (the token's roles plus any active `RoleBinding`),
   `resource` (completed by the PIP, see below), `env` (`now`, plus whatever
   `AccessRequest.environment` carries).
5. **Deny that resolves `true` wins; failing that, allow that resolves `true` wins; failing
   that, if anything was indeterminate, deny (fail-closed); failing that, `not_applicable`** —
   DRBAC has nothing to say, and the combinator falls through to `rbac` or the scope provider.

### The PIP: completing `resource.*` on demand

`ResourceRef.attributes` is only what the caller already had in hand — loading the entire
resource "just in case" on every protected endpoint would be wasted work most of the time,
because most conditions do not need it. A `ResourceAttributeResolver` registered by
`resource.type` fills in the rest when a condition actually references it:

```python
class InvoiceAttributes(ResourceAttributeResolver):
    resource_type = "invoice"

    async def resolve(self, resource: ResourceRef) -> Mapping[str, Any]:
        async with uow_scope() as uow:
            invoice = await uow.invoices.get(UUID(resource.id))
        return {"status": invoice.status, "amount": invoice.amount}
```

It is memoized per batch (`POST /auth/drbac/check` evaluates up to 50 items), and a resolver
failure never propagates — it is logged, and whatever condition needed that attribute becomes
indeterminate rather than tumbling the whole decision.

### Client-side: `/me/snapshot` and `POST /check`

`GET /auth/drbac/me/snapshot` returns only the `client_evaluable` rules of a scope — no
bindings, no disabled policies, nothing with a `Predicate` in it — for the TypeScript client's
`evaluate()`, which is purely optimistic and never the authority. `POST /auth/drbac/check` is
the real thing: the same `AuthorizationEngine.decide()` that runs on every protected route,
batched and deduplicated. See
[`@hexcore-js/darwin-client`'s plugin docs](../../../darwin-client/en/plugins.md#drbac) for the
client side.

---

## Threat model

| Risk | Vector | Mitigation |
| :-- | :-- | :-- |
| Escalation via role assignment | A tenant admin assigns themselves a role with `*` | Anti-escalation: only what the actor already holds effectively in that scope; `is_system` roles are immutable through the API |
| Escalation via impersonation | Impersonate an admin | `AuthorizationEngine` always evaluates the **actor**, never the subject — the same rule `AuthContext.has_scope` already enforces |
| Cross-tenant escalation | A binding at `"org:4"` applied to `"org:42"` | `WithinScope` and scope-chain resolution compare **by path segment**, never by raw string prefix |
| Malicious or oversized policy | A huge or deeply nested condition tree | No `eval`, no regex in the AST; size limits enforced at save time and again defensively when compiling |
| Staleness after revocation | Roles embedded in a JWT live up to `access_ttl` | The `pv`/version counters make the cache miss immediately; `revoke_all_for` exists for an immediate cut |
| Client-server drift | The UI shows a button the server would reject | The UI is always optimistic; a caught `AccessDeniedError` triggers revalidation; `evaluate()` returns `"unknown"` rather than guessing `"allow"` |
| Information leak | A 403's `reason` field | `Decision.reason` is internal only — the body carries `required`, never why a policy lost. `explain` only exists behind `POST /simulate`, itself gated by `authz.debug` |
| Predicate as an execution path | A condition trying to run arbitrary code | There is no `eval`; a `Predicate` only resolves to a function explicitly registered in this process by name, and only ever runs server-side |

---

## See also

- [Bundled plugins](./bundled-plugins.md) — the full list, `rbac` and `drbac` included
- [`@hexcore-js/darwin-client` plugins](../../../darwin-client/en/plugins.md) — `rbac()` and `drbac()` on the client
- [Writing your own plugin](./writing-plugins.md)
