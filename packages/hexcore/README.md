# HexCore

[![PyPI](https://img.shields.io/pypi/v/hexcore?color=blue)](https://pypi.org/project/hexcore/)
[![PyPI Downloads](https://static.pepy.tech/personalized-badge/hexcore?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/hexcore)
[![Python](https://img.shields.io/pypi/pyversions/hexcore)](https://pypi.org/project/hexcore/)
[![License](https://img.shields.io/pypi/l/hexcore)](https://github.com/Indroic/HexCore/blob/master/LICENSE)

📖 **[Full documentation](https://github.com/Indroic/HexCore/tree/master/docs/hexcore/)** ·
🐙 **[Repository](https://github.com/Indroic/HexCore)** ·
🟦 **[TypeScript client for Darwin](https://github.com/Indroic/HexCore/tree/master/docs/darwin-client/)**

A reusable core for Python applications built on **hexagonal architecture**, **DDD**, **CQRS**
and **background tasks**. HexCore ships the abstractions (entities, repositories, unit of work,
buses) *and* the infrastructure every project otherwise rewrites: the SQL session layer, the
FastAPI factories, the worker runner, the dynamic cron, identity, and the testing utilities.

The design goal is that the happy path takes **zero configuration**: `create_app()` with no
arguments gives you a usable app, `init_engine()` with no arguments gives you a
production-correct engine.

> 🇪🇸 **¿Preferís español?** La documentación está completa en los dos idiomas:
> **[docs/es/](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/)**.

```python
# main.py — a complete HexCore app
from hexcore.fastapi import build_lifespan, create_app, SqlEngineStep

app = create_app(
    lifespan=build_lifespan(SqlEngineStep()),
    routers=[users_router, tickets_router],
)
```

```python
# worker.py — the complete worker, with cron, mutual death and SIGTERM
import hexcore.cqrs as cqrs

await cqrs.run_procrastinate_worker(
    procrastinate_app,
    queues=["default", "reactive"],
    scheduler=cqrs.DynamicScheduler(repo, enqueuer, lock_provider=lock),
    on_startup=[lambda: cqrs.seed_cron_jobs(CRON_JOBS)],
)
```

---

## 📚 Documentation

**→ [`docs/`](https://github.com/Indroic/HexCore/tree/master/docs/hexcore/) — 🇬🇧 [English](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/) · 🇪🇸 [español](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/)**

| | English | Español |
| :-- | :-- | :-- |
| Installation and extras | [installation](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/installation.md) | [instalacion](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/instalacion.md) |
| Quickstart | [quickstart](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/quickstart.md) | [inicio-rapido](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/inicio-rapido.md) |
| Configuration | [configuration](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/configuration.md) | [configuracion](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/configuracion.md) |
| SQL layer | [sql](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/sql.md) | [sql](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/sql.md) |
| Repositories and entities | [repositories](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/repositories.md) | [repositorios](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/repositorios.md) |
| FastAPI utilities | [fastapi](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/fastapi.md) | [fastapi](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/fastapi.md) |
| CQRS architecture | [cqrs](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/cqrs.md) | [cqrs](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/cqrs.md) |
| Queues and workers | [queues-and-workers](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/queues-and-workers.md) | [colas-y-workers](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/colas-y-workers.md) |
| Scheduled tasks | [cron](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/cron.md) | [cron](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/cron.md) |
| **Event Sourcing** | [event-sourcing](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/event-sourcing.md) | [event-sourcing](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/event-sourcing.md) |
| Testing | [testing](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/testing.md) | [testing](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/testing.md) |
| CLI | [cli](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/cli.md) | [cli](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/cli.md) |
| **Darwin** (identity) | [darwin/](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/darwin/) | [darwin/](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/darwin/) |
| API reference | [reference](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/reference.md) | [referencia](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/referencia.md) |
| Versions and migration | [versions-and-migration](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/versions-and-migration.md) | [versiones-y-migracion](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/versiones-y-migracion.md) |
| Typing | [typing](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/typing.md) | [tipado](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/tipado.md) |

---

## Installation

```sh
pip install hexcore
```

Requires Python ≥ 3.12. HexCore pulls in no heavy dependencies: everything that is not the core
lives in **extras**, and the modules that need them only import them when you use them.

```sh
pip install "hexcore[api,sql,procrastinate]"
pip install "hexcore[darwin-sqlalchemy]"
pip install "hexcore[all]"
```

| Group | Extras |
| :-- | :-- |
| Core | `api`, `sql`, `mongo`, `redis`, `rabbitmq`, `procrastinate`, `celery` |
| Identity | `darwin`, `darwin-sqlalchemy`, `darwin-beanie`, `darwin-magic-link`, `darwin-two-factor`, `darwin-oauth`, `darwin-impersonate`, `darwin-passkey`, `darwin-organization` |
| Everything | `all` |

The full table, with what each one enables, is in
[installation](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/installation.md) · [instalación](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/instalacion.md).

> `import hexcore.cqrs` works with no extras at all: name resolution is lazy, so
> `hexcore.cqrs.SqlAlchemyCronJobRepository` only requires `[sql]` at the moment you ask for it.

---

## The four imports

There is one facade module per task. They re-export the public surface **without moving
anything**: the long paths keep resolving to the same object.

```python
import hexcore.fastapi as hx     # create_app, build_lifespan, providers, middlewares, health
import hexcore.cqrs as cqrs      # Command, Query, handlers, decorators, buses, worker, cron
import hexcore.sql as sql        # init_engine, session_scope, uow_scope, Base, query DTOs
import hexcore.darwin as darwin  # IdentityConfig, configure_identity, build_identity_router
```

The facades expose **only the canonical names**. The historical `I*` aliases were removed in
7.0 — see [Removed API](#removed-api-and-its-replacement).

---

## What you get, at a glance

| You need | API | Extra |
| :-- | :-- | :-- |
| A wired-up FastAPI app | `hx.create_app()`, `hx.AppFeatures` | `api` |
| Orchestrated startup and shutdown | `hx.build_lifespan()` + steps | `api` |
| SQL engine and sessions | `sql.init_engine()`, `sql.PoolSettings` | `sql` |
| A session or UoW outside a request | `sql.session_scope()`, `sql.uow_scope()` | `sql` |
| Health checks that actually probe | `hx.register_health_routes()` | `api` |
| Rate limiting | `hx.rate_limit()` | `api` |
| SSE / WebSocket / connection caps | `hx.sse_stream()`, `hx.connection_slot()` | `api` |
| Commands, queries and events | `cqrs.Command`, `cqrs.Query`, `cqrs.HandlerRegistry` | — |
| Running work in the background | `cqrs.background_command`, `cqrs.background_task` | — |
| The worker entrypoint | `cqrs.run_cqrs_worker()`, `cqrs.run_procrastinate_worker()` | — |
| Cron you can edit without a restart | `cqrs.DynamicScheduler`, `cqrs.SqlAlchemyCronJobRepository` | `sql` |
| Distributed locks | `cqrs.RedisLockProvider`, `cqrs.PostgresLockProvider` | `redis` / `sql` |
| Identity and authentication | `darwin.configure_identity()`, `darwin.build_identity_router()` | `darwin` + storage |
| Testing all of the above | `hexcore.testing` | — |

---

## Darwin: the identity module

Registration, email verification, sign-in, sessions with rotating refresh, revocation, audited
impersonation, and a plugin system that adds second factor, OAuth, magic links, passkeys and
organizations without the core knowing about them.

```python
from hexcore.darwin import (
    IdentityConfig,
    build_identity_router,
    configure_identity,
    identity_startup_steps,
)
from hexcore.fastapi import AppFeatures, SqlEngineStep, build_lifespan, create_app

configure_identity(IdentityConfig())

app = create_app(
    features=AppFeatures(auth_context=True, csrf=True),
    lifespan=build_lifespan(SqlEngineStep(), *identity_startup_steps()),
    routers=[build_identity_router()],
)
```

⚠️ If you use SQL, the most important thing to read before deploying is the Alembic section:
[storage](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/darwin/storage.md) · [almacenamiento](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/darwin/almacenamiento.md).
A plugin missing from your `env.py` makes `alembic revision --autogenerate` emit
`op.drop_table` for its tables.

---

## Project templates (CLI)

```sh
hexcore init my_project --template hexagonal
hexcore init my_project --template vertical-slice
```

- `hexagonal` → `src/domain`, `src/application`, `src/infrastructure`.
- `vertical-slice` → `src/features`, `src/shared/{domain,application,infrastructure}`.

Both generate a root `config.py` and leave Alembic configured. See
[CLI](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/cli.md) · [CLI](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/cli.md).

---

## Versions and support

| Series | Status | What it means |
| :-- | :-- | :-- |
| **9.x** | ✅ **Active** | The only supported one. Receives features and fixes. Adds the event store and Event Sourcing, and leaves a single event bus port. |
| **8.x** | ⛔ **Deprecated** | Ships Darwin. Migrating to 9.x is mechanical: the two deprecated names still resolve and warn. |
| **7.x** | ⛔ **Deprecated** | Removes the pre-5.0 surface and fixes the CORS and rate-limiting defects. No Darwin: it shipped before the module landed on `master`. |
| **6.x** | ⛔ **Deprecated** | No longer receives fixes. Contains the CORS and rate-limiting security defects fixed in 7.0, and the pre-5.0 aliases still present. |
| **5.x** | ⛔ **Deprecated** | Same API surface as 6.x. |
| **4.x** | ⛔ **Deprecated** | **Partial** application: missing the Celery event-loop fix, the facades, and the aligned documentation. |
| **3.x** | ⛔ **Deprecated** | **Partial** application: has the P0/P1 fixes but none of the FastAPI factories. |
| **2.x** | ⛔ **Deprecated** | Contains silent bugs fixed in 5.x: the worker re-enqueued instead of executing, the cron skipped or duplicated runs, and a Redis outage switched off the entire cron. |
| **1.x** | ⛔ **Deprecated** | No support of any kind. |

**Everything before 9.0 is deprecated. Migrate to 9.x.** The detail of each series, the silent
2.x bugs and the step-by-step guides are in
[versions and migration](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/versions-and-migration.md) ·
[versiones y migración](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/versiones-y-migracion.md).

### Removed API and its replacement

The v1/v2 aliases were deprecated since 5.0 — two full majors of notice — and were removed in
7.0. The replacement is mechanical: they are renames, not behavior changes.

| Removed in 7.0 (was v1/v2) | Use instead |
| :-- | :-- |
| `ICommandBus`, `IQueryBus`, `IEventBus` | `AbstractCommandBus`, `AbstractQueryBus`, `AbstractEventBus` |
| `ICommandHandler`, `IQueryHandler` | `AbstractCommandHandler`, `AbstractQueryHandler` |
| `IMiddleware` | `AbstractMiddleware` |
| `ISerializer` | `AbstractSerializer` |
| `IEventDispatcher` | `EventBus` |
| `EventBus.register()` / `.dispatch()` | `EventBus.subscribe()` / `.publish()` |
| `ServerConfig.event_dispatcher` | `ServerConfig.event_bus` |
| `SQLAlchemyCommonImplementationsRepo` | `SqlAlchemyRepository` |
| `BeanieODMCommonImplementationsRepo` | `BeanieRepository` |
| `NoSqlUnitOfWork` | `BeanieUnitOfWork` |
| `reset_sqlalchemy_engine()` | `dispose_engine()` |
| `MiddlewareConfig` | **Removed in 3.0.** It was dead code: never read. |

Passing `event_dispatcher=` to `ServerConfig` **fails with an error that says what to use**,
rather than being silently ignored: pydantic discards keyword arguments it does not know, and
keeping the default bus without noticing would surface much later as "my events never arrive".

If you are still on 6.x, run your tests with warnings visible to see what you have left to
migrate:

```sh
python -m pytest -W "default::DeprecationWarning"
```

---

## Contributing

1. **Code of conduct** — read the [Code of Conduct](https://github.com/Indroic/HexCore/blob/master/CODE_OF_CONDUCT.md) before interacting.
2. **Branches** — fork and create a branch (`feat/name`, `fix/name`, `docs/name`).
3. **Tests** — every fix lands with at least one test that fails before and passes after:

   ```sh
   uv sync --extra all --group dev
   uv run python -m pytest -q
   ```

   CI fails if any test is **skipped**: a skip means an extra is missing, and we would be
   reporting green without having run half the suite.
4. **Typecheck** — `uv run pyright hexcore`. The verdict comes from the ratchet, not the exit
   code: see [typing](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/en/typing.md) · [tipado](https://github.com/Indroic/HexCore/blob/master/docs/hexcore/es/tipado.md).
5. **Style** — [PEP8](https://pep8.org/). Comment the *why*, not the *what*.
6. **Commits** — [Commitizen](https://commitizen-tools.github.io/commitizen/): `feat:`, `fix:`,
   `docs:`, `refactor:`, and `!` for breaking changes. The version bump and the CHANGELOG are
   automatic on merge to `master`.
7. **PRs** — describe the problem, the reproduction, the solution and **why that option**.

Full detail in [CONTRIBUTING.md](https://github.com/Indroic/HexCore/blob/master/CONTRIBUTING.md).

### The agent skill

[`skills/hexcore/`](https://github.com/Indroic/HexCore/tree/master/skills/hexcore) is a
[Claude Code](https://claude.com/claude-code) skill covering both packages — this one and the
TypeScript client. It reads the installed package instead of remembering it, and audits a
project for the framework's silent failure modes rather than just describing them. Its prose is
under the same CI gate as the documentation, so a rename here turns it red.

To use it in a project that consumes HexCore, copy that directory into the project's
`.claude/skills/`. Details in
[skills/README.md](https://github.com/Indroic/HexCore/blob/master/skills/README.md).

---

## References

- [docs/](https://github.com/Indroic/HexCore/tree/master/docs/hexcore/) — the complete documentation, in English and Spanish.
- [docs/ARCHITECTURE_TYPING.md](https://github.com/Indroic/HexCore/blob/master/docs/ARCHITECTURE_TYPING.md) — type system and stubs.
- [CHANGELOG.md](https://github.com/Indroic/HexCore/blob/master/CHANGELOG.md) — change history.
- [CONTRIBUTING.md](https://github.com/Indroic/HexCore/blob/master/CONTRIBUTING.md) — collaboration guidelines.
- [SECURITY.md](https://github.com/Indroic/HexCore/blob/master/SECURITY.md) — security policy.
