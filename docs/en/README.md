# HexCore Documentation

HexCore is a reusable core for Python applications built on **hexagonal architecture**, **DDD**,
**CQRS** and **background tasks**. It ships the abstractions — entities, repositories, unit of
work, buses — *and* the infrastructure that every project otherwise rewrites: the SQL session
layer, the FastAPI factories, the worker runner, the dynamic cron, identity, and the testing
utilities.

The design goal is that **the happy path takes zero configuration**: `create_app()` with no
arguments gives you a usable app, `init_engine()` with no arguments gives you a
production-correct engine.

> 🇪🇸 [Versión en español](../es/) — a complete translation of this documentation.

---

## Getting started

| # | Document | Covers |
| :-- | :-- | :-- |
| 1 | **[Installation](./installation.md)** | The package, the 17 extras, and what each one enables |
| 2 | **[Quickstart](./quickstart.md)** | A complete app and a complete worker, in two screens |
| 3 | **[Configuration](./configuration.md)** | `ServerConfig`, `LazyConfig`, discovery and CORS |

## The framework

| # | Document | Covers |
| :-- | :-- | :-- |
| 4 | **[SQL layer](./sql.md)** | Engine, sessions, scopes, unit of work, Alembic |
| 5 | **[Repositories and entities](./repositories.md)** | `BaseEntity`, events, generic repositories, queries and cursors |
| 6 | **[FastAPI utilities](./fastapi.md)** | `create_app`, lifespan, health, rate limiting, streaming, routers |
| 7 | **[CQRS architecture](./cqrs.md)** | Commands, queries, events, buses, middleware, factory |
| 8 | **[Queues and workers](./queues-and-workers.md)** | Smart Routing, enqueuers, the consumer and the runner |
| 9 | **[Scheduled tasks](./cron.md)** | Dynamic cron, distributed locks, catch-up |
| 9b | **[Event Sourcing](./event-sourcing.md)** | Event store, aggregates, projections, the outbox — and what it does not guarantee |
| 10 | **[Testing](./testing.md)** | Test buses, fakes, fixtures, overrides |
| 11 | **[CLI](./cli.md)** | `hexcore init`, migrations, `hexcore identity` |

## Identity

| # | Document | Covers |
| :-- | :-- | :-- |
| 12 | **[Darwin: introduction](./darwin/)** | What it is, quickstart, and the decisions that change how you integrate it |
| 13 | **[Storage](./darwin/storage.md)** | Backends, schema, Alembic, `init_beanie`, your own user model |
| 14 | **[Bundled plugins](./darwin/bundled-plugins.md)** | All six, with routes and warnings |
| 15 | **[Writing a plugin](./darwin/writing-plugins.md)** | Extension points, hooks, and the traps |

## Reference

| # | Document | Covers |
| :-- | :-- | :-- |
| 16 | **[API reference](./reference.md)** | Every public symbol, by facade |
| 17 | **[Versions and migration](./versions-and-migration.md)** | Support policy, removed API, migration guides |
| 18 | **[Typing](./typing.md)** | The house rule, the generated stubs, and the CI gates |

---

## The three imports

There is one facade module per task. They re-export the public surface **without moving
anything**: the long paths keep working and return the same object.

```python
import hexcore.fastapi as hx    # create_app, build_lifespan, providers, middlewares, health
import hexcore.cqrs as cqrs     # Command, Query, handlers, decorators, buses, worker, cron
import hexcore.sql as sql       # init_engine, session_scope, uow_scope, Base, query DTOs
```

Plus one more for identity:

```python
import hexcore.darwin as darwin  # IdentityConfig, configure_identity, build_identity_router
```

All four facades resolve names **lazily** (PEP 562): `import hexcore.cqrs` works with no extras
installed, and `cqrs.SqlAlchemyCronJobRepository` only requires `[sql]` at the exact moment you
ask for it. That is why each facade ships a generated `.pyi` — without it, type checkers would
see `Any` for all 64 symbols of `hexcore.cqrs`.

---

## What you get, at a glance

| You need | API | Extra |
| :-- | :-- | :-- |
| A wired-up FastAPI app | `hx.create_app()`, `hx.AppFeatures` | `api` |
| Orchestrated startup and shutdown | `hx.build_lifespan()` + steps | `api` |
| SQL engine and sessions | `sql.init_engine()`, `sql.dispose_engine()`, `sql.PoolSettings` | `sql` |
| A session or UoW outside a request | `sql.session_scope()`, `sql.uow_scope()` | `sql` |
| Request IDs correlated in the logs | `hx.RequestIDMiddleware`, `hx.install_request_id_logging()` | `api` |
| Domain exceptions mapped to HTTP | `hx.register_exception_handlers()` | `api` |
| Health checks that actually probe | `hx.register_health_routes()`, `hx.check_health()` | `api` |
| Rate limiting | `hx.rate_limit()` | `api` |
| SSE / WebSocket / connection caps | `hx.sse_stream()`, `hx.ws_heartbeat()`, `hx.connection_slot()` | `api` |
| Router composition | `hx.build_root_router()`, `hx.mount_routers()` | `api` |
| List and search endpoints | `hx.register_query_endpoint()` | `api` |
| Cursor pagination | `sql.CursorPageDTO`, `sql.CursorRequestDTO` | `sql` |
| Commands, queries and events | `cqrs.Command`, `cqrs.Query`, `cqrs.HandlerRegistry` | — |
| Running work in the background | `cqrs.background_command`, `cqrs.background_handler`, `cqrs.background_task` | — |
| The worker entrypoint | `cqrs.run_cqrs_worker()`, `cqrs.run_procrastinate_worker()` | — |
| Cron you can edit without a restart | `cqrs.DynamicScheduler`, `cqrs.SqlAlchemyCronJobRepository` | `sql` |
| Distributed locks | `cqrs.RedisLockProvider`, `cqrs.PostgresLockProvider` | `redis` / `sql` |
| Identity and authentication | `darwin.configure_identity()`, `darwin.build_identity_router()` | `darwin` + storage |
| Testing all of the above | `hexcore.testing` | — |

---

## Conventions used here

- **The canonical names are the `Abstract*` ones.** The v1/v2 `I*` aliases were removed in 7.0;
  the replacement table is in [Versions and migration](./versions-and-migration.md).
- **Examples are code that runs.** See [the rule](../README.md#the-rule-of-this-documentation).
- **⚠️ warnings are real failure modes**, not style notes. Almost all of them describe something
  that raises no exception and surfaces far from its cause.
