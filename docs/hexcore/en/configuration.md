# Configuration

All configuration lives in one pydantic object, `ServerConfig`, which HexCore resolves lazily
from a module in your project. **There is no I/O and no configuration resolution at import
time**, so you can change where it reads from before anything reads it.

---

## The file

Define a `config.py` at the project root:

```python
from hexcore.config import ServerConfig

config = ServerConfig(
    app_title="Red API",
    app_version="1.4.0",
    async_sql_database_url="postgresql+asyncpg://user:pass@localhost/red",
    repository_discovery_paths={
        "myapp.features.users.infrastructure.repositories",
        "myapp.features.billing.infrastructure.repositories",
    },
)
```

You can export an **instance** named `config`, a **class** named `config` deriving from
`ServerConfig`, or a class named `ServerConfig` deriving from the base. All three are accepted.

---

## How it is resolved (`LazyConfig`)

`LazyConfig` looks for the configuration module in this order and keeps the first one that
resolves:

| # | Source | Example |
| :-- | :-- | :-- |
| 1 | `HEXCORE_CONFIG_MODULE` (single module) | `HEXCORE_CONFIG_MODULE=myapp.settings.prod` |
| 2 | `HEXCORE_CONFIG_MODULES` (comma-separated list) | `HEXCORE_CONFIG_MODULES=myapp.base,myapp.prod` |
| 3 | `LazyConfig.set_config_modules([...])` | In code, before startup |
| 4 | The `config` module at the project root | The default |

If nothing valid is found, it falls back to `ServerConfig()` with every default.

```python
from hexcore.config import LazyConfig

LazyConfig.set_config_modules(["myapp.settings.testing"])
LazyConfig.clear_cache()          # force a fresh resolution

config = LazyConfig.get_config()  # cached from here on
```

`clear_cache()` is what tests use to switch configuration between cases: without it, the first
resolution stays cached on the class for the whole process.

---

## The fields

### Server and project

| Field | Default | What it does |
| :-- | :-- | :-- |
| `base_dir` | `Path(".")` | Project root |
| `host` | `"localhost"` | — |
| `port` | `8000` | Also feeds the CORS default outside `debug` |
| `debug` | `True` | Changes the `allow_origins` default |
| `app_title` | `"HexCore API"` | The FastAPI title used by `create_app()` |
| `app_version` | `"0.1.0"` | The FastAPI version used by `create_app()` |

### Databases

| Field | Default |
| :-- | :-- |
| `sql_database_url` | `"sqlite:///./db.sqlite3"` |
| `async_sql_database_url` | `"sqlite+aiosqlite:///./db.sqlite3"` |
| `mongo_uri` | `"mongodb://localhost:27017/euphoria_db"` |
| `mongo_db_name` | `"euphoria_db"` |
| `redis_uri` | `"redis://localhost:6379/0"` |
| `redis_cache_duration` | `300` (seconds) |

`sql_database_url` is the **synchronous** DSN, used by Alembic's `env.py`;
`async_sql_database_url` is what `init_engine()` consumes. Having both is not redundancy:
Alembic runs migrations synchronously and the app runs asynchronously.

### Security (CORS)

| Field | Default |
| :-- | :-- |
| `allow_origins` | derived — see below |
| `allow_credentials` | `True` |
| `allow_methods` | `["*"]` |
| `allow_headers` | `["*"]` |

⚠️ **This is the field most worth understanding.** `allow_origins` is derived in an
`mode="after"` validator, not in the class body:

- If you **do not** pass it: under `debug` it becomes `["*"]`; outside `debug`,
  `["http://localhost:<port>"]`.
- If you pass it explicitly — even `[]` — it is respected as-is.

And there is an invariant that holds **always**, not just in production: **`"*"` together with
`allow_credentials=True` is never valid.** The browser cannot receive `*` with credentials, so
Starlette **reflects the attacker's `Origin`** and adds
`Access-Control-Allow-Credentials: true`. Any origin can then read authenticated responses using
the victim's session cookie, with no XSS required.

Depending on how you asked for it:

- If you did **not** declare `allow_credentials`, it is lowered to `False` and a warning is
  emitted. Without that header the browser will not expose the response, so the reflection is
  harmless.
- If you declared **both**, the app **does not start**: you explicitly asked for something the
  CORS specification does not permit, and guessing which one you meant would be worse than
  failing.

```python
config = ServerConfig(
    allow_origins=["https://my-frontend.com"],   # the only combination that works with cookies
    allow_credentials=True,
)
```

### Injectable infrastructure

| Field | Default | Port |
| :-- | :-- | :-- |
| `cache_backend` | `MemoryCache()` | `ICache` |
| `event_bus` | `InMemoryEventBus()` | `EventBus` |

These are instances, not dotted paths: you replace them with the real object, built with your own
parameters.

### Repository discovery

```python
config = ServerConfig(
    repository_discovery_paths={
        "myapp.features.users.infrastructure.repositories",
    }
)
```

Discovery is **explicit and folder-agnostic**. If the set is empty no module is loaded, and the
Unit of Work fails to build with a diagnostic error instead of guessing paths. This is a
deliberate change from v1: guessing by folder convention tied the framework to one project
layout, and failed silently when the layout was different.

### Optional modules

| Field | Type | Default |
| :-- | :-- | :-- |
| `cqrs` | `CQRSConfig \| None` | `None` (disabled) |
| `darwin` | `IdentityConfig \| None` | `None` (disabled) |

```python
from hexcore.application.cqrs.config import BusConfig, CQRSConfig
from hexcore.config import ServerConfig

config = ServerConfig(
    cqrs=CQRSConfig(
        command_bus=BusConfig(
            middlewares=["hexcore.infrastructure.cqrs.middlewares.LoggingMiddleware"],
        ),
    ),
)
```

Both are typed `t.Any` in the model, and that is not sloppiness: annotating them properly would
force `hexcore.config` to import those modules, which loads half the framework — including the
CLI.

⚠️ **Darwin's signing key does not live in `ServerConfig`.** Every `ServerConfig` field has a
default, and a signing secret with a default is the worst thing an auth library can ship: half
the deployments would end up signing with the same example value. It lives in
`IdentityConfig.secret_key` as a `SecretStr` with **no default**, read from
`HEXCORE_DARWIN_SECRET_KEY`.

---

## Removed names

Passing `ServerConfig` a name that was removed **fails with remediation** instead of being
ignored:

```python
ServerConfig(event_dispatcher=bus)
# ValueError: ServerConfig ya no acepta 'event_dispatcher': se eliminó en 7.0 y estaba
# deprecado desde 5.0. Usá 'event_bus'.
```

Without that validator, pydantic **silently discards** keyword arguments it does not know: anyone
migrating with `event_dispatcher=` would keep the default bus without noticing, and the symptom
would surface much later as "my events never arrive". `extra="forbid"` is not used to achieve the
same thing, because it would reject *any* unknown key, and some consumers pass their own keyword
arguments on purpose.

---

## Environment variables

| Variable | Purpose |
| :-- | :-- |
| `HEXCORE_CONFIG_MODULE` | The configuration module (highest priority) |
| `HEXCORE_CONFIG_MODULES` | Several candidate modules, comma-separated |
| `HEXCORE_DARWIN_SECRET_KEY` | Darwin's signing key |
| `HEXCORE_TEST_MONGO_URI` | Repository test suite only: the real Mongo for the `mongo` tests |

---

## Next

→ **[SQL layer](./sql.md)** — engine, sessions and unit of work.
