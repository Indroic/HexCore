# Review rubric

How to review HexCore code, in the order that finds the expensive things first.

Start mechanical, then read. The script finds what a script can find; your attention is worth
more on the things it cannot judge.

```bash
python scripts/hexcore_audit.py src/ --fail-on high
python scripts/hexcore_surface.py --check <files changed in the diff>
```

Report a clean audit explicitly. "The audit is clean" is information; silence is not.

---

## Severity, and what it means here

| Level | Meaning | Examples |
| :-- | :-- | :-- |
| **Critical** | Data loss, or code that cannot run | A missing line in `env.py`; a column named `metadata`; API removed in 7.0 |
| **High** | Wrong at runtime, usually silently | CORS `*` with credentials; `RetryMiddleware` + queue retry; a message defined in `<locals>`; a bus with no enqueuer |
| **Medium** | Works, fights the framework | Reimplemented CRUD; a redeclared base field; manual event dispatch; `TestClient` without `with` |
| **Low** | Style within the framework's grain | A cron task named by hand; a projection with no `reset()` |

A finding whose failure you cannot describe concretely is not a finding. Say what breaks, with
what input, and where it surfaces.

---

## 1. Migrations first

This is the only category that destroys data, and it is invisible in review unless you look
for it deliberately.

- [ ] Does `alembic/env.py` call `ensure_framework_models_loaded()`?
- [ ] `import_all_models(models)`, over the package that actually holds the models?
- [ ] `ensure_identity_schema_loaded(plugins=[...])`, with **every** Darwin plugin listed?
- [ ] Does the diff add a table? Is it reachable from `Base.metadata`?
- [ ] Does the diff include a generated migration? **Read it.** An `op.drop_table` you did not
      ask for is the symptom.
- [ ] Any column named `metadata`?

## 2. Names that do not exist

- [ ] Anything from the removed-in-7.0 table (`references/removed-api.md`)?
- [ ] `ServerConfig(event_dispatcher=...)`? It raises.
- [ ] `bus.register(...)` / `bus.dispatch(...)` on an event bus? It is `subscribe`/`publish`.
- [ ] Imports from `hexcore.infrastructure.events` or `hexcore.domain.auth`? Both go in 10.0.
- [ ] If unsure whether a symbol exists — **do not guess**:
      `python scripts/hexcore_surface.py --find <name>`.

## 3. Transactions and events

- [ ] Is every write inside `async with uow:`?
- [ ] Is there a `session.commit()` outside a Unit of Work?
- [ ] Does application code call `dispatch_events()` or `collect_domain_events()`? That is the
      UoW's job at commit time.
- [ ] Are events emitted by the **entity** (`register_event`) rather than constructed in the
      use case?
- [ ] For Beanie: are entities tracked with `uow.collect_entity()` or
      `@register_entity_on_uow`? NoSQL tracking is explicit.
- [ ] With an event store: is exactly one of the relay and the UoW publishing?

## 4. Repositories and entities

- [ ] Three properties, not five. Are `fields_serializers` / `fields_resolvers` present but
      returning `None`? Delete them.
- [ ] Does the repository override base CRUD? Overriding is defensible for eager loading or a
      tenant filter — ask which it is, and say so in the finding rather than asserting it is
      wrong.
- [ ] Does the entity redeclare `id`, `created_at`, `updated_at` or `is_active`?
- [ ] Is `delete()` being used where a hard delete was intended? It is a **soft** delete.
- [ ] Does `ServerConfig` list the module this repository lives in, under
      `repository_discovery_paths`?

## 5. Boundaries

- [ ] Does a use case return a `BaseEntity` instead of a DTO?
- [ ] Do FastAPI dependencies (`get_session`, `get_sql_uow`) appear outside an endpoint? Those
      are scopes' job.
- [ ] Does an endpoint working on an already-open UoW use `get_sql_uow_open`?
- [ ] Do domain modules import from infrastructure?

Note: a CQRS handler taking a UoW directly is **idiomatic**, not a violation — HexCore's own
examples do it. The domain-service indirection belongs to the `UseCase` path.

## 6. Background work

- [ ] Are `Command`, `Query` and decorated tasks all at module level?
- [ ] Is the bus built with `CQRSFactory` and given an `enqueuer` when background commands
      exist?
- [ ] Is the **same** bus shared between web and worker?
- [ ] `RetryMiddleware` and a queue retry together?
- [ ] `TransactionMiddleware` with a `uow_factory`, and only on handlers that do not manage
      their own transaction?
- [ ] Does a cron job pass the function rather than a task-name string?
- [ ] Multiple replicas with a scheduler and no `lock_provider`?
- [ ] `asyncio.run()` inside a Celery task?

## 7. Configuration and exposure

- [ ] `allow_origins` with `"*"`, and what `allow_credentials` is.
- [ ] Is Darwin's secret read from `HEXCORE_DARWIN_SECRET_KEY`, and never defaulted in code?
- [ ] Is `rate_limit`'s `on_backend_error` deliberate? `"allow"` for capacity, `"deny"` on
      authentication routes.
- [ ] Behind a proxy: does the rate-limit key pass `trusted_proxies`?
- [ ] Does `/health` (liveness) probe dependencies? It must not — that turns a Redis outage
      into a restart loop.

## 8. Tests

- [ ] `with TestClient(app)`, not a bare constructor.
- [ ] Are CQRS overrides done through `override_cqrs` so they are restored?
- [ ] Is the double-half of Smart Routing covered — that the consumer *executes* what the bus
      enqueued?
- [ ] `@pytest.mark.anyio`, not `pytest-asyncio` (not a dependency of the framework).
- [ ] Does anything assert on a real `sleep` where `FixedClock` would do?

---

## Writing the finding

State the defect, the concrete path to it, and where it surfaces. The value of this framework's
failure modes is that they are **specific**, and a review that says "consider adding the
Alembic hooks" loses exactly the part that makes someone act:

> `alembic/env.py` never calls `ensure_framework_models_loaded()`. The next
> `alembic revision --autogenerate` will emit `op.drop_table("hexcore_cron_jobs")` — the
> migration generates cleanly, so this surfaces when it is applied, in whatever environment
> runs it first.

Rank by what it costs, not by how easy it was to spot.
