# Installation

```sh
pip install hexcore
```

Requires **Python ≥ 3.12**. What that command installs is the core: three dependencies
(`pydantic`, `typer`, `croniter`) and nothing else. Everything heavy lives in **extras**, and the
modules that need them only import them when you actually use them.

```sh
pip install "hexcore[api,sql,procrastinate]"
pip install "hexcore[all]"
```

---

## Core extras

| Extra | Brings | Enables |
| :-- | :-- | :-- |
| `[api]` | FastAPI | `hexcore.fastapi`: `create_app`, lifespan, middlewares, health, rate limiting, streaming |
| `[sql]` | SQLAlchemy, Alembic, asyncpg, aiosqlite | `hexcore.sql`, SQL repositories, SQL cron, `PostgresLockProvider` |
| `[mongo]` | Beanie (pulls in PyMongo) | MongoDB repositories and UoW |
| `[redis]` | redis | `RedisEventBus`, `RedisLockProvider`, Redis cache |
| `[rabbitmq]` | aio-pika, pika | `RabbitMQEventBus` and its worker |
| `[procrastinate]` | Procrastinate | `ProcrastinateEnqueuer`, `run_procrastinate_worker` |
| `[celery]` | Celery | `CeleryEnqueuer`, `run_in_worker_loop` |
| `[all]` | Everything above plus Darwin | — |

## Darwin extras

Darwin, the identity module, splits into **nine** extras. Three are storage, six are features,
and the criterion is the same for all nine: **if the consumer can decide not to have it, it is an
extra.**

| Extra | Brings | Enables |
| :-- | :-- | :-- |
| `[darwin]` | FastAPI, joserfc, argon2-cffi | The core: domain, services, tokens, transports, plugins. **No storage.** |
| `[darwin-sqlalchemy]` | `hexcore[darwin]` + SQLAlchemy, Alembic, asyncpg, aiosqlite | SQL identity storage |
| `[darwin-beanie]` | `hexcore[darwin]` + Beanie | MongoDB identity storage |
| `[darwin-magic-link]` | `hexcore[darwin]` | Single-use link login |
| `[darwin-two-factor]` | `hexcore[darwin]` | TOTP (RFC 6238) with backup codes |
| `[darwin-oauth]` | `hexcore[darwin]` + httpx | Authorization Code + PKCE |
| `[darwin-impersonate]` | `hexcore[darwin]` | "Sign in as" another user, audited |
| `[darwin-passkey]` | `hexcore[darwin]` + webauthn | WebAuthn |
| `[darwin-organization]` | `hexcore[darwin]` | Organizations, members and invitations |

```sh
pip install "hexcore[darwin-sqlalchemy,darwin-two-factor]"
```

### Why storage is separate

A deployment picks **one** backend. Whoever picks Mongo has no reason to install SQLAlchemy,
Alembic and asyncpg — that is ~15 MB and a supply-chain surface they do not use. And the same in
reverse.

### Why every plugin gets its own extra, even when it adds no packages

Four of the six (`magic-link`, `two-factor`, `impersonate`, `organization`) run on stdlib plus
the core, so today they declare zero new dependencies. The extra still earns its place for three
reasons, none of them cosmetic:

1. **It is the stable name** where a future dependency lands without changing the consumer's
   install command. `[darwin-passkey]` did not have `webauthn` until it did.
2. **It makes the command work.** Every plugin extra pulls in `hexcore[darwin]`, so
   `pip install 'hexcore[darwin-two-factor]'` brings the core the plugin needs. Without that
   self-reference the command would install a plugin with no core: a broken import.
3. **It documents the surface** in the one place a consumer reads before installing. A plugin
   missing from that list is a plugin nobody finds.

What the extra deliberately does **not** do is require a storage backend. "One of two" cannot be
expressed in packaging metadata: an extra with both would install SQLAlchemy for the person who
chose Mongo, which is exactly what the split avoids. The choice is resolved at runtime, with an
error that names the missing extra.

---

## Importing without extras

Facade name resolution is **lazy**, so this works on a bare install:

```python
import hexcore.cqrs as cqrs      # ✅ no extras needed
import hexcore.darwin            # ✅ pulls in neither joserfc, argon2 nor sqlalchemy
```

The extra is demanded at the exact moment you ask for the symbol that needs it:

```python
repo = cqrs.SqlAlchemyCronJobRepository()   # ⛔ fails if [sql] is missing
```

…and the error **carries the command**:

```
SqlAlchemyRepository necesita 'sqlalchemy', que HexCore empaqueta en el extra [sql] y no
está instalado.

    pip install 'hexcore[sql]'
```

That message comes from `require_extra`, and it is the reason the function exists: a plain
`ModuleNotFoundError: No module named 'sqlalchemy'` is correct and useless — it does not say
that HexCore ships it under `[sql]`, which is the only thing the consumer needs to know.

There are tests that verify this by blocking packages in `sys.meta_path`: the Darwin core imports
with all six plugins blocked, and each plugin imports with the other five blocked.

### Asking from your own code

```python
from hexcore.capabilities import has_extra, installed_extras, require_extra

if has_extra("redis"):
    ...

require_extra("sqlalchemy", para="MyRepository")
```

`has_extra` asks about the **importable name**, not the extra's name: `argon2-cffi` imports as
`argon2`, `aio-pika` as `aio_pika`, and `py_webauthn` as `webauthn`. It uses
`importlib.util.find_spec` rather than a `try: import`, because importing has effects — it
executes the module, leaves it in `sys.modules`, and can cost hundreds of milliseconds. Asking
whether something is available should not install it into the process.

---

## With `uv`

```sh
uv add hexcore --extra api --extra sql
```

To work on the repository itself:

```sh
uv sync --extra all --group dev
uv run python -m pytest -q
```

---

## Docker

The repository ships a reference `Dockerfile`. For your own app, the minimum is:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir "hexcore[api,sql,procrastinate]"
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

The worker is a **separate** process with the same image and a different command
(`python worker.py`). Do not put it in the same container as the API: the worker runner exits
with `WorkerDied` so the orchestrator restarts it, and that would restart your API too.

---

## Next

→ **[Quickstart](./quickstart.md)** — a complete app and a complete worker.
