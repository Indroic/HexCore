# FastAPI utilities

Everything in this document lives in `hexcore.fastapi` and requires the `[api]` extra.

```python
import hexcore.fastapi as hx
```

---

## `create_app()`

```python
from hexcore.fastapi import create_app

app = create_app()   # already a usable app
```

With no arguments it wires up:

- `title` and `version` from `ServerConfig` (`app_title`, `app_version`).
- CORS from `config.allow_origins`.
- `RequestIDMiddleware` (`X-Request-ID`).
- `TimingMiddleware` (`X-Response-Time`).
- Domain exceptions mapped to HTTP.
- `GET /health` and `GET /health/ready`.

### The signature

```python
create_app(
    lifespan=None,
    *,
    features: AppFeatures | None = None,
    routers: Sequence[MountableRouter] | None = None,
    health_probes: Sequence[Probe] | None = None,
    exception_mapping: dict[type[Exception], int] | None = None,
    exception_headers: HeadersFactory | None = None,
    **fastapi_kwargs,          # forwarded verbatim to FastAPI(...)
) -> FastAPI
```

### `AppFeatures`

The switches live in **one object**, not eight keyword arguments:

```python
from hexcore.fastapi import AppFeatures, create_app

app = create_app(
    features=AppFeatures(cors=False, timing=False),
    routers=[(users_router, {"prefix": "/api/v1"})],
    title="Red API",
)
```

| Field | Default | Controls |
| :-- | :-- | :-- |
| `cors` | `True` | The `CORSMiddleware`, configured from `ServerConfig` |
| `request_id` | `True` | `RequestIDMiddleware` |
| `timing` | `True` | `TimingMiddleware` |
| `exception_handlers` | `True` | Exception-to-HTTP mapping |
| `health` | `True` | The health routes. Also accepts a `HealthRoutes` |
| `auth_context` | `False` | Darwin's `AuthContextMiddleware` |
| `csrf` | `False` | Darwin's `CsrfMiddleware` |

The last two default to off because they only make sense with identity wired up: enabling them
without `configure_identity()` would be middleware looking at a container that does not exist.

---

## `build_lifespan()`

Orchestrates startup and shutdown as a list of **steps**:

```python
from hexcore.fastapi import (
    BeanieStep, CacheStep, CallableStep, CronSeedStep, EventBusStep,
    ProcrastinateStep, SqlEngineStep, build_lifespan, create_app,
)

app = create_app(
    lifespan=build_lifespan(
        SqlEngineStep(),
        BeanieStep(documents=MONGO_DOCUMENTS),
        EventBusStep(RealtimeEventBus()),
        ProcrastinateStep(procrastinate_app),
        CronSeedStep(CRON_JOBS),
        CallableStep("warm-caches", warm_validation_cache, on_error="warn"),
    ),
)
```

The guarantees, which are the reason the helper exists:

1. **Teardown in reverse order**, and only for steps that actually started. A step that failed has
   nothing to close, and calling `stop()` on it hides the original error behind an
   `AttributeError`.
2. **Per-step `on_error`.** A cache warmup should not take down startup, and that is declared on
   the step without relaxing the policy for the whole boot.
3. **One log line per step, with its duration.** A slow startup without this is a slow startup
   with no clues.
4. **A failing teardown does not block the following ones** and does not mask the exception that
   caused the shutdown.

### The bundled steps

| Step | On startup | On shutdown |
| :-- | :-- | :-- |
| `SqlEngineStep(url=None, *, pool=None, **engine_kwargs)` | `init_engine(...)` | `dispose_engine()` |
| `BeanieStep(documents=None)` | `init_beanie` with those documents | — |
| `EventBusStep(bus)` | Installs and starts the bus | Stops it |
| `CacheStep(backend)` | Installs the cache backend | Closes it |
| `ProcrastinateStep(app)` | Opens the Procrastinate connection | Closes it |
| `CronSeedStep(jobs, *, create_tables=False)` | `seed_cron_jobs(jobs)` | — |
| `CallableStep(name, start, stop=None)` | Your coroutine | The `stop` one, if you gave it |

`on_error` accepts `"raise"` (default) or `"warn"`, and can be set per step or for the whole
lifespan: `build_lifespan(*steps, on_error="warn")`.

Writing your own means implementing the `StartupStep` protocol: a `name` attribute, an
`async def start()`, and optionally an `async def stop()`.

---

## Health checks

```python
from hexcore.fastapi import Probe, register_health_routes

register_health_routes(app)                    # /health and /health/ready
register_health_routes(app, path="/_status")   # or wherever you want
```

| Route | What it is | What it does |
| :-- | :-- | :-- |
| `GET /health` | **Liveness** | 200, touching nothing |
| `GET /health/ready` | **Readiness** | Probes dependencies; 503 with detail if something fails |

Liveness **not** probing dependencies is the important decision: if it did, a Redis outage would
make Kubernetes restart a perfectly healthy app — and restarting it does not fix Redis.

Readiness runs `SELECT 1` against the engine, `ping` against Redis and Mongo, **all probes
concurrent and each with its own timeout**, and reports per-dependency latency.

### Your own probes

```python
register_health_routes(app, probes=[
    Probe("sql", check_database),
    Probe("cache", check_redis, timeout=1.0, critical=False),
])
```

A dependency with `critical=False` reports `degraded` rather than `down`: without Redis the app
serves more slowly, it does not stop serving, and killing the instance over it makes the incident
worse.

### If your app already publishes its own `/health`

The two routes are registered separately, so an app already in production — with its own response
shape and a typed client generated from its OpenAPI — can adopt the readiness route, which is the
part nobody wants to write by hand, without touching the contract it already published:

```python
register_health_routes(app, liveness=False)
register_health_routes(app, liveness=False, readiness_path="/_ready")
```

And if what must be preserved is the **body shape**, `response_factory` adapts it without giving
up the probes. The status code is still decided by the report, which is what the orchestrator
reads:

```python
register_health_routes(
    app,
    response_factory=lambda r: {"ok": r.status != "down", "checks": r.dependencies},
)
```

The same from `create_app`, without disabling the whole feature:

```python
from hexcore.fastapi import AppFeatures, HealthRoutes, create_app

app = create_app(features=AppFeatures(health=HealthRoutes(liveness=False)))
```

### Outside a route

```python
from hexcore.fastapi import check_health

report = await check_health(deep=True)
report.status            # "up" | "degraded" | "down"
report.dependencies      # list[DependencyReport], with latency_ms and detail
report.http_status()     # 200 or 503
```

---

## Rate limiting

```python
from fastapi import Depends
from hexcore.fastapi import rate_limit

@router.get("/reports", dependencies=[Depends(rate_limit(10, 60))])
async def reports(): ...

per_user = rate_limit(100, 3600, key=lambda r: r.state.user_id)
```

It sits on the `ICache` port, not on Redis directly, so it works with `MemoryCache` in tests. It
returns **429 with `Retry-After`**.

### The policy for a downed backend is explicit

```python
rate_limit(10, 60, on_backend_error="allow")   # default: a Redis outage does not take down the API
rate_limit(10, 60, on_backend_error="deny")
```

There is no universally correct default, so the decision is yours. `"allow"` is the default
because on most routes a limit is capacity protection, not security, and turning a cache outage
into a full outage is worse. On authentication routes the answer inverts: Darwin's
`sign_in_rate_limit` uses `"deny"`, because a Redis outage should not become unlimited credential
stuffing.

### The key

`client_ip_key` is the default. Behind a proxy, the IP the app sees is the proxy's, so:

```python
from hexcore.infrastructure.api.rate_limit import forwarded_ip_key

rate_limit(10, 60, key=forwarded_ip_key(trusted_proxies={"10.0.0.1"}, trust_hops=1))
```

`trusted_proxies` is mandatory: without the list, `X-Forwarded-For` is written by the client and
the limit is bypassed by changing a header.

---

## Correlated request IDs

```python
import logging

from hexcore.fastapi import get_request_id, install_request_id_logging

logging.basicConfig(level=logging.INFO)          # first: configure logging
install_request_id_logging(fmt="%(asctime)s [%(request_id)s] %(message)s")
```

`RequestIDMiddleware` **reuses the incoming header when there is one** — breaking the gateway's
chain means losing the trace — and publishes it to a `ContextVar` and to `request.state`.
`install_request_id_logging()` injects it into **every log line**, which is half the value:
without that, having the header correlates nothing.

⚠️ **Order matters.** `install_request_id_logging()` instruments the handlers that **already
exist**. In a process where nobody configured logging yet there are none, so the call has nothing
to do — and it warns with a `RuntimeWarning` instead of staying silent.

From anywhere in the request:

```python
rid = get_request_id()
```

---

## Domain exceptions to HTTP

```python
from hexcore.fastapi import register_exception_handlers

register_exception_handlers(app, mapping={TicketNotFound: 404})
```

`DEFAULT_EXCEPTION_STATUS_MAP` holds the framework's base mapping. `create_app` merges it with
identity's and then with yours (`exception_mapping=`), so your map **wins**.

`include_detail` accepts `False` to avoid leaking the exception message to the client, or a
callable that decides what to expose per exception. `headers_for` adds per-exception headers:
that is what Darwin uses for the `WWW-Authenticate` on its 401s.

---

## Streaming: SSE, WebSocket and connection caps

```python
from hexcore.fastapi import sse_stream

@router.get("/events")
async def events():
    return sse_stream(my_generator(), heartbeat_seconds=30)
```

The heartbeat is an SSE comment that clients ignore and proxies count as traffic: without it, a
load balancer with an idle timeout cuts the connection. `X-Accel-Buffering: no` is added too,
without which nginx buffers the events and the stream arrives in chunks.

```python
from hexcore.fastapi import connection_slot, ws_heartbeat

async with connection_slot(cache, f"ws:{user_id}", max_connections=3) as granted:
    if not granted:
        await ws.close(code=1013)
        return
    await ws.accept()
    async with ws_heartbeat(ws, interval=30):
        ...
```

`connection_slot` releases the slot **even if the block raises or is cancelled**. Leaking a slot
on a bad disconnect leaves the user unable to reconnect until the TTL expires, and it is the
classic bug of these limits.

---

## Router composition

```python
from fastapi import Depends
from hexcore.fastapi import build_root_router, mount_routers

admin = build_root_router(
    "/admin",
    {"/users": users_router, "/reports": reports_router},
    dependencies=[Depends(require_admin)],
    tags=["admin"],
)

mount_routers(app, [admin, (public_router, {"prefix": "/v1"})])
```

`children` accepts a **dict** `{prefix: router}` or a **sequence**, and that is not sugar: a dict
cannot have two `""` keys, so a root with several children that **already carry their own
prefix** — the normal case when each feature declares its full routes — cannot be expressed as a
map.

```python
# users_router is already APIRouter(prefix="/users"), tickets_router likewise.
api_v1 = build_root_router("/api/v1", [users_router, tickets_router])

# They can be mixed: a bare router is equivalent to ("", router).
api_v1 = build_root_router("/api/v1", [users_router, ("/reports", reports_router)])
```

---

## List and search endpoints

```python
from fastapi import Depends
from hexcore.fastapi import register_query_endpoint

register_query_endpoint(
    router,
    path="/tickets",
    use_case_factory=lambda: QueryEntitiesUseCase(repo),
    dependencies=[Depends(get_current_user)],
)
```

It generates a `GET` with `limit`, `offset`, `search`, `search_fields`, `filters`
(`field:operator:value`) and `sort` (`field:asc|desc`) as query parameters, documented in the
OpenAPI schema.

An invalid field returns a **structured 422**:

```json
{"detail": {"message": "…", "field": "no_such_field", "allowed": ["title", "status"]}}
```

`build_query_endpoint` returns the function without registering it, in case you want to mount it
yourself.

---

## Session and UoW dependencies

| Dependency | Yields |
| :-- | :-- |
| `hx.get_session` | An `AsyncSession` |
| `hx.get_sql_uow` | The UoW **not entered** — the use case does its own `async with` |
| `hx.get_sql_uow_open` | The UoW already entered |
| `hx.get_nosql_uow` | The Beanie UoW |

---

## CQRS providers

```python
from fastapi import Depends
from hexcore.fastapi import configure_cqrs, provide_command_bus

container = configure_cqrs(registry, enqueuer=enqueuer)   # once, at startup

@router.post("/tickets")
async def create(cmd: CreateTicket, bus=Depends(provide_command_bus)):
    return await bus.dispatch(cmd)
```

| Provider | Yields |
| :-- | :-- |
| `provide_command_bus` | The command bus |
| `provide_query_bus` | The query bus |
| `provide_event_bus` | The event bus |
| `provide_registry` | The `HandlerRegistry` |
| `get_cqrs_container` | The whole container |

They exist as **functions** for exactly one reason: so you can replace them with
`app.dependency_overrides` in tests. `reset_cqrs()` clears the container between cases.

And `container.build_consumer()` builds the worker's consumer on **the same** buses and the same
serializer, so there is no second source of truth between the web process and the worker.

---

## Next

→ **[CQRS architecture](./cqrs.md)**.
