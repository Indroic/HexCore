# HexCore — monorepo

[![PyPI](https://img.shields.io/pypi/v/hexcore?label=hexcore&color=blue)](https://pypi.org/project/hexcore/)
[![npm](https://img.shields.io/npm/v/@hexcore-js/darwin-client?label=%40hexcore-js%2Fdarwin-client&color=blue)](https://www.npmjs.com/package/@hexcore-js/darwin-client)
[![Python](https://img.shields.io/pypi/pyversions/hexcore)](https://pypi.org/project/hexcore/)
[![License](https://img.shields.io/github/license/Indroic/HexCore)](./LICENSE)

📖 **All documentation lives in [`docs/`](./docs/)**, in English and Spanish, for both packages.

**Contents** — [What this is](#what-this-is) · [Quick look](#quick-look) ·
[Installation](#installation) · [The two packages](#the-two-packages) ·
[What is in the box](#what-is-in-the-box) · [At a glance](#at-a-glance) ·
[The TypeScript client](#the-typescript-client) · [Versions](#versions-and-support) ·
[Documentation](#documentation) · [Development](#development)

---

## What this is

**HexCore is a Python library you install with `pip`.** It is not a project template, not a
code generator, and not a service you deploy — you `import` it from an application you own, and
you keep owning the application.

What it gives you is the layer most Python backends end up writing twice: the **abstractions**
of hexagonal architecture, DDD and CQRS (entities, repositories, unit of work, command and
query buses) *and* the **infrastructure** that actually makes them run — the SQL session layer,
the FastAPI application factory, the background worker runner, a cron you can edit without a
redeploy, an event store, authentication, and the testing doubles for all of it.

Three things shape every API in it:

- **The happy path takes zero configuration.** `create_app()` with no arguments returns a usable
  app; `init_engine()` with no arguments returns a production-correct engine. You pass arguments
  when you disagree with a default, not to get off the ground.
- **Nothing heavy is a hard dependency.** Everything past the core lives in optional extras, and
  the modules that need one import it only when you use it — `import hexcore.cqrs` works with no
  extras installed at all.
- **The dangerous defaults are the safe ones.** Where a wrong choice is a silent production bug
  rather than a crash — CORS with credentials, a refresh token in `localStorage`, a cron without
  a distributed lock — the library refuses, warns, or ships the conservative option.

## Quick look

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

## Installation

The two packages are installed independently, from their own registries:

```sh
pip install hexcore                       # or: pip install "hexcore[api,sql,darwin-sqlalchemy]"
npm install @hexcore-js/darwin-client
```

Python ≥ 3.12 · Node ≥ 20. There are **17 extras**:

| Group | Extras |
| :-- | :-- |
| Core | `api`, `sql`, `mongo`, `redis`, `rabbitmq`, `procrastinate`, `celery` |
| Identity | `darwin`, `darwin-sqlalchemy`, `darwin-beanie`, `darwin-magic-link`, `darwin-two-factor`, `darwin-oauth`, `darwin-impersonate`, `darwin-passkey`, `darwin-organization` |
| Everything | `all` |

Extra-by-extra table in [installation](./docs/hexcore/en/installation.md) ·
[instalación](./docs/hexcore/es/instalacion.md).

## The two packages

| | What it is | Documentation | Published on |
| :-- | :-- | :-- | :-- |
| 🐍 [`packages/hexcore/`](./packages/hexcore/) | The Python library described above. | 🇬🇧 [EN](./docs/hexcore/en/) · 🇪🇸 [ES](./docs/hexcore/es/) | [PyPI: `hexcore`](https://pypi.org/project/hexcore/) |
| 🟦 [`packages/darwin-client/`](./packages/darwin-client/) | A TypeScript client for **Darwin**, the authentication module that ships inside the Python library. Zero runtime dependencies. | 🇬🇧 [EN](./docs/darwin-client/en/) · 🇪🇸 [ES](./docs/darwin-client/es/) | [npm: `@hexcore-js/darwin-client`](https://www.npmjs.com/package/@hexcore-js/darwin-client) |

### What Darwin is, precisely

Darwin is **not a separate product and not a separate service**. It is a module of the Python
library — `hexcore.darwin` — that you mount into your own FastAPI app. It runs in your process,
against your database, behind your domain. There is no HexCore server anywhere.

### How the two packages relate

```
your FastAPI app  ──imports──▶  hexcore  (Python, PyPI)
      │                            └── hexcore.darwin  ──exposes──▶  HTTP routes under /auth
      │                                                                      ▲
      └── your frontend  ──imports──▶  @hexcore-js/darwin-client  ───────────┘
                                        (TypeScript, npm)
```

The client speaks Darwin's HTTP contract. It does **not** import anything from the Python
package, and the Python package does not know the client exists. They are coupled by the
contract in [`packages/darwin-client/openapi/`](./packages/darwin-client/openapi/), which is
dumped from the Python code and committed, so any drift between the two shows up in the diff of
a pull request.

**You do not need both.** Use the Python library on its own and authenticate however you like;
or, if you use Darwin and your frontend is TypeScript, the client saves you from hand-rolling
the rotating refresh, the double-submit CSRF and the transport choice — where a mistake in any
of the three is a security bug, not an inconvenience.

---

## What is in the box

### The four facades

There is one facade module per task. They re-export the public surface **without moving
anything**: the long paths keep resolving to the same object.

```python
import hexcore.fastapi as hx     # create_app, build_lifespan, providers, middlewares, health
import hexcore.cqrs as cqrs      # Command, Query, handlers, decorators, buses, worker, cron
import hexcore.sql as sql        # init_engine, session_scope, uow_scope, Base, query DTOs
import hexcore.darwin as darwin  # IdentityConfig, configure_identity, build_identity_router
```

All four resolve names **lazily** (PEP 562), which is what lets `import hexcore.cqrs` succeed
with nothing installed. Each one ships a generated `.pyi` so type checkers still see real
signatures instead of `Any`.

### Domain and application

`BaseEntity` and `AggregateRoot`, domain events that entities record and the unit of work
publishes, generic repositories with filters, search, sorting and **cursor pagination**, and
Beanie documents for the Mongo side. Hybrid storage (write SQL, read a projection) is a
first-class case, not a workaround.

→ [repositories](./docs/hexcore/en/repositories.md) · [repositorios](./docs/hexcore/es/repositorios.md)

### CQRS

Three buses — command, query and event — with a handler registry, factories that resolve
dependencies at dispatch time, middleware, declarative configuration, pluggable serialization,
and an **envelope** of metadata that travels with the message.

→ [cqrs](./docs/hexcore/en/cqrs.md) · [cqrs](./docs/hexcore/es/cqrs.md)

### Queues and workers

Three decorators (`background_command`, `background_handler`, `background_task`) and **Smart
Routing**: the *same* bus instance enqueues when it runs in the web process and executes when it
runs inside the worker. No second wiring, no "did I import the sync or the async one".

Backends: Procrastinate, Celery, Redis, RabbitMQ. The runner handles SIGTERM, mutual death
between worker and scheduler, and graceful shutdown.

→ [queues-and-workers](./docs/hexcore/en/queues-and-workers.md) · [colas-y-workers](./docs/hexcore/es/colas-y-workers.md)

### Scheduled tasks (dynamic cron)

The schedule lives in a **table, not in code**: you change a cron expression, pause a job or add
one without a redeploy. With distributed locks (Redis or Postgres) so N replicas do not run the
same job N times, and explicit catch-up behaviour for a window the scheduler missed.

→ [cron](./docs/hexcore/en/cron.md) · [cron](./docs/hexcore/es/cron.md)

### Event Sourcing

An event store with optimistic concurrency, aggregates rebuilt from their events, projections,
and a **transactional outbox** wired into the unit of work so publishing cannot succeed while
the business change rolls back.

| Backend | Extra | For |
| :-- | :-- | :-- |
| `InMemoryEventStore` | — | Tests and development. Does not persist. |
| `SqlAlchemyEventStore` | `sql` | **The recommended primary store.** Shares the session with the business change. |
| `BeanieEventStore` | `mongo` | Applications already on Mongo. |
| `RedisEventStore` | `redis` | A fast short-lived log or a hot buffer. Not as the primary store. |

The documentation has a section on **what this event store does not guarantee** — read it before
choosing it.

→ [event-sourcing](./docs/hexcore/en/event-sourcing.md) · [event-sourcing](./docs/hexcore/es/event-sourcing.md)

### FastAPI utilities

`create_app()` and an orchestrated `build_lifespan()`, health checks that actually probe their
dependencies, rate limiting, correlated request IDs in the logs, domain exceptions mapped to
HTTP, SSE and WebSocket streaming with connection caps, router composition, and generated list
and search endpoints.

→ [fastapi](./docs/hexcore/en/fastapi.md) · [fastapi](./docs/hexcore/es/fastapi.md)

### SQL layer

One engine with sane pool settings, `session_scope()` and `uow_scope()` for work outside a
request, a single declarative `Base` with a naming convention, and an Alembic setup that knows
about the framework's own tables — so `--autogenerate` does not quietly emit `op.drop_table` for
them.

→ [sql](./docs/hexcore/en/sql.md) · [sql](./docs/hexcore/es/sql.md)

### Darwin — authentication

Registration, email verification, sign-in, sessions with rotating refresh and reuse detection,
revocation, and audited impersonation. Storage on SQLAlchemy or Beanie, and a plugin system that
adds the rest without the core knowing about them:

| Plugin | What it adds |
| :-- | :-- |
| `magic_link` | Single-use link login |
| `two_factor` | TOTP with backup codes |
| `oauth` | Authorization Code + PKCE |
| `impersonate` | "Sign in as", audited with reason and expiry |
| `passkey` | WebAuthn |
| `organization` | Organizations, members and invitations |

```python
from hexcore.darwin import IdentityConfig, build_identity_router, configure_identity, identity_startup_steps
from hexcore.fastapi import AppFeatures, SqlEngineStep, build_lifespan, create_app

configure_identity(IdentityConfig())

app = create_app(
    features=AppFeatures(auth_context=True, csrf=True),
    lifespan=build_lifespan(SqlEngineStep(), *identity_startup_steps()),
    routers=[build_identity_router()],
)
```

⚠️ On SQL, read the Alembic section before deploying: a plugin missing from your `env.py` makes
`alembic revision --autogenerate` emit `op.drop_table` for its tables.

→ [darwin/](./docs/hexcore/en/darwin/) · [darwin/](./docs/hexcore/es/darwin/)

### CLI

```sh
hexcore init my_project --template hexagonal       # src/domain, src/application, src/infrastructure
hexcore init my_project --template vertical-slice  # src/features, src/shared/{domain,application,infrastructure}
```

Both generate a root `config.py` and leave Alembic configured. The CLI also wraps migrations and
`hexcore identity` for identity administration.

→ [cli](./docs/hexcore/en/cli.md) · [cli](./docs/hexcore/es/cli.md)

### Testing

`hexcore.testing` ships test buses, `InMemoryTaskEnqueuer`, `override_cqrs`, `FakeLockProvider`,
fake repositories and unit of work, and pytest fixtures — including ones for the HTTP layer and
for identity. The in-memory adapters are real implementations of the ports, not stubs: the same
contract suite runs against them and against the real backends.

→ [testing](./docs/hexcore/en/testing.md) · [testing](./docs/hexcore/es/testing.md)

### Typing

Pyright in **strict** mode over the whole package, with a ratchet that only goes down, generated
`.pyi` stubs for the lazy facades, and a house rule that bans bare `# type: ignore`. A stub that
drifts from its `_EXPORTS` fails CI.

→ [typing](./docs/hexcore/en/typing.md) · [tipado](./docs/hexcore/es/tipado.md) ·
[ARCHITECTURE_TYPING.md](./docs/ARCHITECTURE_TYPING.md)

---

## At a glance

| You need | API | Extra |
| :-- | :-- | :-- |
| A wired-up FastAPI app | `hx.create_app()`, `hx.AppFeatures` | `api` |
| Orchestrated startup and shutdown | `hx.build_lifespan()` + steps | `api` |
| SQL engine and sessions | `sql.init_engine()`, `sql.PoolSettings` | `sql` |
| A session or UoW outside a request | `sql.session_scope()`, `sql.uow_scope()` | `sql` |
| Health checks that actually probe | `hx.register_health_routes()` | `api` |
| Rate limiting | `hx.rate_limit()` | `api` |
| SSE / WebSocket / connection caps | `hx.sse_stream()`, `hx.connection_slot()` | `api` |
| Cursor pagination | `sql.CursorPageDTO`, `sql.CursorRequestDTO` | `sql` |
| Commands, queries and events | `cqrs.Command`, `cqrs.Query`, `cqrs.HandlerRegistry` | — |
| Running work in the background | `cqrs.background_command`, `cqrs.background_task` | — |
| The worker entrypoint | `cqrs.run_cqrs_worker()`, `cqrs.run_procrastinate_worker()` | — |
| Cron you can edit without a restart | `cqrs.DynamicScheduler`, `cqrs.SqlAlchemyCronJobRepository` | `sql` |
| Distributed locks | `cqrs.RedisLockProvider`, `cqrs.PostgresLockProvider` | `redis` / `sql` |
| An event store and projections | `hexcore.eventsourcing` | `sql` / `mongo` / `redis` |
| Identity and authentication | `darwin.configure_identity()`, `darwin.build_identity_router()` | `darwin` + storage |
| Testing all of the above | `hexcore.testing` | — |

---

## The TypeScript client

`@hexcore-js/darwin-client` is agnostic of UI framework, of runtime and of backend, and has zero
runtime dependencies. What it handles for you:

- **Proactive and reactive refresh, single-flight.** Darwin's access token lives two minutes and
  its refresh *rotates with reuse detection* — a client that refreshes per request logs the user
  out under load. One refresh fires even with N requests in flight.
- **Two transports.** `CookieTransport` (`HttpOnly`, with the HMAC double-submit CSRF handled for
  you) and `BearerTransport` (memory, `localStorage`, or React Native's `AsyncStorage`).
- **A session store** with the exact `useSyncExternalStore` contract, so React needs no adapter
  and Svelte gets a three-line one. SSR handled explicitly.
- **The same six plugins as the server**, with full type inference — `client.twoFactor`,
  `client.oauth`, `client.passkey`, `client.magicLink`, `client.impersonate`,
  `client.organization`.
- **One typed error**, `DarwinError`, discriminated by a `code` generated from the Python
  contract.

```ts
import { createDarwinClient, BearerTransport, memoryStorage } from "@hexcore-js/darwin-client";

const client = createDarwinClient({
  baseUrl: "https://api.example.com",
  transport: new BearerTransport({ storage: memoryStorage() }),
});

const result = await client.signIn("ana@example.com", "correct-horse-battery");
```

→ [Darwin Client documentation](./docs/darwin-client/en/) ·
[documentación en español](./docs/darwin-client/es/)

---

## Working on HexCore with an agent

[`skills/hexcore/`](./skills/hexcore) is a [Claude Code](https://claude.com/claude-code) skill
that covers **both** packages. The repository root is itself a plugin, so
`/plugin marketplace add .` once is all it takes to have it load here. To use it in a project
that consumes HexCore, copy the directory into that project's `.claude/skills/`.

It does two things a documentation dump cannot. It **reads the installed package** instead of
remembering it — `scripts/hexcore_surface.py` parses the facades' `_EXPORTS` with `ast` and the
TypeScript client's entry points, so it answers from whatever versions are actually in front of
it. And it **audits code rather than describing it**: `scripts/hexcore_audit.py` finds the
framework's silent failure modes statically, starting with the Alembic `env.py` that generates
a clean migration and drops a table with data in it.

Its prose is under the same CI gate as `docs/`, so a rename in either package turns it red.

→ [skills/README.md](./skills/README.md)

---

## Versions and support

| Series | Status | |
| :-- | :-- | :-- |
| **9.x** | ✅ **Active** | The only supported series. Adds the event store and Event Sourcing. |
| **8.x** and earlier | ⛔ Deprecated | Migrate to 9.x. 6.x and earlier additionally carry the CORS and rate-limiting defects fixed in 7.0. |

Full table, the silent 2.x bugs and step-by-step migration guides in
[versions and migration](./docs/hexcore/en/versions-and-migration.md) ·
[versiones y migración](./docs/hexcore/es/versiones-y-migracion.md).

## Documentation

All of the monorepo's documentation is consolidated in [`docs/`](./docs/):

```
docs/
├── README.md                  ← index: both packages, both languages
├── ARCHITECTURE_TYPING.md     ← the Python package's strict-typing contract
├── hexcore/
│   ├── en/   ·   es/          ← 20 guides per language, Darwin included
└── darwin-client/
    └── en/   ·   es/          ← 8 guides per language
```

Start at [installation](./docs/hexcore/en/installation.md) and
[quickstart](./docs/hexcore/en/quickstart.md) for the Python side, or at the
[Darwin Client quickstart](./docs/darwin-client/en/quickstart.md) for the frontend one.

**English is the reference version**; `es/` is its translation. If the two ever contradict each
other, English wins. **If a document shows code, there is a test that runs it** — the suite walks
`docs/hexcore/**` and fails when an example names an API that no longer exists.

Each package's `README.md` stays complete and self-sufficient — they are what PyPI and npm
render — and links into `docs/` with absolute GitHub URLs, which are the only ones that resolve
outside the repository.

## Development

```bash
# Python
cd packages/hexcore && uv run pytest -q

# TypeScript
npm install
npm -w @hexcore-js/darwin-client run test
```

CI fails if any test is **skipped**: a skip means an extra is missing from the environment, and
reporting green without having run half the suite is worse than failing.

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the full contribution flow, including the
`packages/` layout, the two independent release pipelines, and why. Security policy in
[`SECURITY.md`](./SECURITY.md); change history in [`CHANGELOG.md`](./CHANGELOG.md).

The agent skill lives in this repository too, under [`skills/`](./skills/) — see
[Working on HexCore with an agent](#working-on-hexcore-with-an-agent).

## License

MIT © David Latosefki. See [`LICENSE`](./LICENSE).
