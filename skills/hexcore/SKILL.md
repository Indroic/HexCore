---
name: hexcore
description: Senior architect for HexCore 9.x, the Python framework for hexagonal architecture, DDD, CQRS, background workers, event sourcing and identity. Use when writing, wiring or reviewing code that imports hexcore — entities, repositories, unit of work, FastAPI apps, command/query buses, queues, cron, event stores or Darwin authentication. Covers both packages of the HexCore monorepo: the Python framework and `@hexcore-js/darwin-client`, the TypeScript client for consuming Darwin identity from a browser, SSR or a native app. Reads the installed package for its API surface instead of trusting a table, and audits existing projects for removed API and the framework's silent failure modes.
---

# HexCore

A reusable core for Python ≥ 3.12 applications: hexagonal architecture, DDD, CQRS and
background tasks. It ships the abstractions **and** the infrastructure — SQL session layer,
FastAPI factories, worker runner, dynamic cron, identity, event store, testing doubles.

The design goal is that the happy path takes zero configuration: `create_app()` with no
arguments gives a usable app, `init_engine()` with no arguments gives a production-correct
engine.

## Two packages, one repository

| Package | Registry | Tag | What it is |
| :-- | :-- | :-- | :-- |
| `hexcore` | PyPI | `hexcore-v*` | The Python framework. Everything below unless marked otherwise. |
| `@hexcore-js/darwin-client` | npm | `darwin-client-v*` | The TypeScript client for Darwin. See `references/darwin-client.md`. |

They version independently. The client is on `0.x`, so it has no major to pin — its surface is
read from the repository, not from a table here.

## Where the scripts are

The commands below write `$SKILL` for the directory holding this file: `skills/hexcore` inside
the HexCore monorepo, `.claude/skills/hexcore` in a project that installed the skill. The
harness exposes it as `${CLAUDE_SKILL_DIR}` where it can. Inside the monorepo, prefix them with
`uv run` so they see the workspace interpreter.

---

## Before writing anything: pin the version

HexCore has shipped **nine majors**, twice by accident from a `feat!:` commit in a docs PR.
The API of 2.x is not the API of 9.x — most of it was deleted in 7.0. Never answer from
memory about what exists.

```bash
python $SKILL/scripts/hexcore_surface.py --version
```

- **On 9.x** — this skill's prose applies.
- **Below 9** — read `references/removed-api.md` first. Names taught here may not exist there.
- **Above 9** — trust `--facade` and `--find` over any table in this skill. They read the
  installed package; the tables are prose.

---

## Do not recall imports. Look them up.

The import registry is not written down anywhere in this skill, on purpose: a written table
is what went stale across seven majors and left the previous version teaching
`SQLAlchemyCommonImplementationsRepo`, deleted in 7.0.

```bash
python $SKILL/scripts/hexcore_surface.py --find SqlAlchemyRepository   # facade, long path, extra
python $SKILL/scripts/hexcore_surface.py --facade cqrs                 # everything one facade exports
python $SKILL/scripts/hexcore_surface.py --deprecated                  # what dies in the next major
python $SKILL/scripts/hexcore_surface.py --darwin-client               # the TypeScript client's exports
```

`--find` searches both packages, so you do not have to know which one a name belongs to
before you ask.

These parse the package's `_EXPORTS` dicts with `ast` — no import, so they work with **zero
extras installed** and cannot be wrong about the version in front of you.

---

## The five facades

One module per task. They re-export the public surface **without moving anything**: the long
paths keep working and return the same object.

```python
import hexcore.fastapi as hx           # [api]        create_app, lifespan, health, routers
import hexcore.cqrs as cqrs            # no extras    messages, buses, worker, cron
import hexcore.sql as sql              # [sql]        engine, scopes, UoW, query DTOs
import hexcore.eventsourcing as es     # per adapter  store, aggregates, projections
import hexcore.darwin as darwin        # [darwin]     identity
```

All five resolve names **lazily** (PEP 562): `import hexcore.cqrs` works on a bare install,
and `cqrs.SqlAlchemyCronJobRepository` demands `[sql]` only at the moment you ask for it.
Each ships a generated `.pyi`, so the types are real despite the lazy loading.

Things that are deliberately **not** on a facade — `hexcore.config`, `hexcore.testing`,
`hexcore.domain.base`, `hexcore.application.use_cases.*`, `hexcore.infrastructure.uow`, the
distributed buses and the queue adapters — are imported by their long path. They are niche,
and a short name would suggest they are part of the happy path. `--find` knows where they are.

---

## Route by task

| You were asked to | Read | Then |
| :-- | :-- | :-- |
| Model an entity, repository, UoW, migrations | `references/core.md` | `assets/templates/repository.py` |
| Write or fix an Alembic `env.py` | `references/core.md` | `assets/templates/env.py` |
| Build or wire a FastAPI app | `references/fastapi.md` | `assets/templates/config.py` |
| Commands, queries, handlers, buses, middleware | `references/cqrs-workers-cron.md` | `assets/templates/handler.py` |
| Background work, queues, workers, scheduled jobs | `references/cqrs-workers-cron.md` | `assets/templates/worker.py` |
| Persist facts, aggregates, projections, an outbox | `references/event-sourcing.md` | — |
| Authentication, sessions, permissions, plugins (server) | `references/darwin.md` | — |
| Consume Darwin from a frontend, SSR or a native app (TypeScript) | `references/darwin-client.md` | — |
| Write tests | `references/testing.md` | `assets/templates/conftest.py` |
| **Review** existing code | `references/review-rubric.md` | `scripts/hexcore_audit.py` |
| Upgrade a project from 2.x–8.x | `references/removed-api.md` | `scripts/hexcore_audit.py` |
| Debug a symptom you can see | `references/failure-modes.md` | — |
| Follow a step-by-step recipe | `references/workflows.md` | — |

Read the one file the task needs. They are written to be read whole, and not to be read together.

---

## Non-negotiables

These are the rules that introspection cannot tell you, because they are about *how* to use
the API rather than what it is called.

1. **Repositories need three properties, not five.** `entity_cls`, `model_cls` (or
   `document_cls`), `not_found_exception`. `fields_serializers` and `fields_resolvers` are
   **optional** — add them only for fields the automatic conversion cannot handle.
2. **Never reimplement the base CRUD.** `get_by_id`, `get_active_by_id`, `list_all`,
   `query_all`, `query_cursor`, `save`, `delete` come from the generic repository. Add
   specialised queries only. And `delete()` is a **soft** delete.
3. **`repository_discovery_paths` is explicit and required.** The UoW discovers repositories
   from it and **fails to build** when the set is empty. It does not guess by folder
   convention — that tied the framework to one layout and failed silently on any other.
4. **Every write goes inside `async with uow:`**, and the UoW owns event dispatch: it
   collects domain events *before* committing and publishes them *after*. Application code
   never calls `dispatch_events()` or `collect_domain_events()`.
5. **Outside a request, use scopes, not dependencies.** `hx.get_session` and `hx.get_sql_uow`
   are FastAPI dependencies and work nowhere else. Workers, cron, scripts and seeds use
   `sql.session_scope()`, `sql.uow_scope()`, `sql.open_uow_scope()`, `sql.nosql_uow_scope()`.
6. **`get_sql_uow` yields the UoW *not entered*;** `get_sql_uow_open` yields it entered. The
   use case opens its own `async with self.uow:` — that is why the default does not.
7. **Alembic's `env.py` needs three calls**: `ensure_framework_models_loaded()`,
   `ensure_identity_schema_loaded(plugins=[...])` if you use Darwin, and
   `import_all_models(models)`. Omitting one emits `op.drop_table` against a table with data
   in it, in a migration that generates cleanly. This is the framework's worst failure mode
   **because nothing raises**.
8. **Events dispatch by hierarchy since 9.0.** A handler subscribed to a base class receives
   its subclasses. Before 9.0 it received nothing and did not fail either.
9. **`UseCase` is not deprecated.** It is still the right abstraction for orchestrating
   without a bus; `cqrs.UseCaseCommandHandler` adapts one into a handler. Use the bus when you
   need middleware, Smart Routing or a queue — not as a blanket upgrade.
10. **Entities do not redeclare `id`, `created_at`, `updated_at` or `is_active`.**
    `BaseEntity` provides all four.
11. **One `init_beanie` call.** It does not accumulate: a second call against the same
    database replaces the first call's registry. Every document — yours, identity's, the
    plugins' — goes in the same call.
12. **CORS: `"*"` with `allow_credentials=True` is never valid.** Declare real origins when
    you use session cookies.
13. **A Darwin frontend has a contract with the deployment, and every clause fails quietly.**
    The CSRF cookie name, the CORS origins, which plugins the server has enabled, and the
    access token's TTL all have to agree across the two packages. None of the four raises where
    the mistake is: a mismatched cookie name reads as a permissions bug, a CORS mistake arrives
    as a network error, and a plugin that is only registered client-side answers 404.
    `references/darwin-client.md` has the four, with the symptom each one produces.

House conventions worth keeping, that the framework does not enforce: a dedicated class per
business operation, DTOs at application boundaries (never a `BaseEntity`), and business rules
in a domain service rather than in the orchestrator. Note that HexCore's own examples inject
the UoW straight into a CQRS handler — that is idiomatic, not a violation.

---

## Never emit these

Deleted in 7.0. They resolve to nothing — not a deprecation warning, an `ImportError`.

| Do not write | Write instead |
| :-- | :-- |
| `SQLAlchemyCommonImplementationsRepo` | `SqlAlchemyRepository` |
| `BeanieODMCommonImplementationsRepo` | `BeanieRepository` |
| `NoSqlUnitOfWork` | `BeanieUnitOfWork` |
| `IEventDispatcher`, `InMemoryEventDispatcher` | `AbstractEventBus`, `cqrs.InMemoryEventBus` |
| `ICommandBus`, `IQueryBus`, `IEventBus` | `AbstractCommandBus`, `AbstractQueryBus`, `AbstractEventBus` |
| `ICommandHandler`, `IQueryHandler` | `AbstractCommandHandler`, `AbstractQueryHandler` |
| `IMiddleware`, `ISerializer` | `AbstractMiddleware`, `AbstractSerializer` |
| `ServerConfig(event_dispatcher=...)` | `ServerConfig(event_bus=...)` — the old name raises `ValueError` |
| `bus.register(...)` / `bus.dispatch(...)` for events | `bus.subscribe(...)` / `bus.publish(...)` |
| `reset_sqlalchemy_engine()` | `dispose_engine()` |
| `MiddlewareConfig` | nothing — it was dead code, removed in 3.0 |

Deprecated in 9.0 and removed in 10.0: `hexcore.domain.events.EventBus`, the whole
`hexcore.infrastructure.events` package, and `hexcore.domain.auth.{PermissionsRegistry,
TokenClaims}`. Run `--deprecated` for the live list. Full detail and the silent behaviour
changes: `references/removed-api.md`.

---

## Before you hand code back

```bash
python $SKILL/scripts/hexcore_surface.py --check path/to/changed_file.py
```

Every `from hexcore… import …` and every `hx.` / `cqrs.` / `sql.` / `es.` / `darwin.`
attribute is resolved against the installed package. This is the same gate HexCore runs over
its own documentation, and it is the difference between believing an import exists and
knowing it does.

When you touched or reviewed existing code:

```bash
python $SKILL/scripts/hexcore_audit.py src/ --fail-on high
```

Report what it finds. A clean audit is worth stating; an unreported critical is the migration
that drops a table.
