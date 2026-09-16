# FastAPI utilities

Everything here lives in `hexcore.fastapi` and requires `[api]`.

```python
import hexcore.fastapi as hx
```

---

## `create_app()`

```python
app = hx.create_app()   # already a usable app
```

With no arguments it wires up `title`/`version` from `ServerConfig`, CORS from
`config.allow_origins`, `RequestIDMiddleware`, `TimingMiddleware`, domain exceptions mapped to
HTTP, and `GET /health` + `GET /health/ready`.

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

The switches live in **one object**, not eight keyword arguments:

```python
app = hx.create_app(
    features=hx.AppFeatures(cors=False, timing=False),
    routers=[(users_router, {"prefix": "/api/v1"})],
    title="Red API",
)
```

| `AppFeatures` field | Default | Controls |
| :-- | :-- | :-- |
| `cors` | `True` | `CORSMiddleware`, configured from `ServerConfig` |
| `request_id` | `True` | `RequestIDMiddleware` |
| `timing` | `True` | `TimingMiddleware` |
| `exception_handlers` | `True` | Exception-to-HTTP mapping |
| `health` | `True` | The health routes; also accepts a `HealthRoutes` |
| `auth_context` | `False` | Darwin's `AuthContextMiddleware` |
| `csrf` | `False` | Darwin's `CsrfMiddleware` |

The last two default off because they only make sense with identity wired up: enabling them
without `configure_identity()` is middleware looking at a container that does not exist.

---

## `build_lifespan()`

```python
app = hx.create_app(
    lifespan=hx.build_lifespan(
        hx.SqlEngineStep(),
        hx.BeanieStep(documents=MONGO_DOCUMENTS),
        hx.EventBusStep(RealtimeEventBus()),
        hx.ProcrastinateStep(procrastinate_app),
        hx.CronSeedStep(CRON_JOBS),
        hx.CallableStep("warm-caches", warm_validation_cache, on_error="warn"),
    ),
)
```

The guarantees, which are why the helper exists:

1. **Teardown in reverse order**, and only for steps that actually started. A step that failed
   has nothing to close, and calling `stop()` on it hides the original error behind an
   `AttributeError`.
2. **Per-step `on_error`.** A cache warmup should not take down startup, and that is declared
   on the step without relaxing the policy for the whole boot.
3. **One log line per step, with its duration.** A slow startup without this has no clues.
4. **A failing teardown does not block the following ones** and does not mask the exception
   that caused the shutdown.

| Step | On startup | On shutdown |
| :-- | :-- | :-- |
| `SqlEngineStep(url=None, *, pool=None, **kwargs)` | `init_engine(...)` | `dispose_engine()` |
| `BeanieStep(documents=None)` | `init_beanie` with those documents | — |
| `EventBusStep(bus)` | Installs and starts the bus | Stops it |
| `CacheStep(backend)` | Installs the cache backend | Closes it |
| `ProcrastinateStep(app)` | Opens the Procrastinate connection | Closes it |
| `CronSeedStep(jobs, *, create_tables=False)` | `seed_cron_jobs(jobs)` | — |
| `CallableStep(name, start, stop=None)` | Your coroutine | The `stop` one, if given |

`on_error` is `"raise"` (default) or `"warn"`, per step or for the whole lifespan. Writing
your own means implementing the `StartupStep` protocol: a `name`, an `async def start()`, and
optionally an `async def stop()`.

---

## Health checks

```python
hx.register_health_routes(app)                    # /health and /health/ready
hx.register_health_routes(app, path="/_status")
```

| Route | What it is | What it does |
| :-- | :-- | :-- |
| `GET /health` | **Liveness** | 200, touching nothing |
| `GET /health/ready` | **Readiness** | Probes dependencies; 503 with detail on failure |

Liveness **not** probing dependencies is the important decision: if it did, a Redis outage
would make Kubernetes restart a perfectly healthy app — and restarting it does not fix Redis.

Readiness runs `SELECT 1` against the engine and `ping` against Redis and Mongo, **all probes
concurrent, each with its own timeout**, reporting per-dependency latency.

```python
hx.register_health_routes(app, probes=[
    hx.Probe("sql", check_database),
    hx.Probe("cache", check_redis, timeout=1.0, critical=False),
])
```

`critical=False` reports `degraded` rather than `down`: without Redis the app serves more
slowly, it does not stop serving, and killing the instance over it makes the incident worse.

An app already in production — with its own response shape and a typed client generated from
its OpenAPI — can adopt just the readiness route:

```python
hx.register_health_routes(app, liveness=False, readiness_path="/_ready")
hx.register_health_routes(app, response_factory=lambda r: {"ok": r.status != "down"})
```

The status code is still decided by the report, which is what the orchestrator reads. Outside
a route: `report = await hx.check_health(deep=True)`, with `report.status`
(`"up" | "degraded" | "down"`), `report.dependencies` and `report.http_status()`.

---

## Rate limiting

```python
@router.get("/reports", dependencies=[Depends(hx.rate_limit(10, 60))])
async def reports(): ...

per_user = hx.rate_limit(100, 3600, key=lambda r: r.state.user_id)
```

It sits on the `ICache` port, not on Redis directly, so it works with `MemoryCache` in tests.
Returns **429 with `Retry-After`**.

The policy for a downed backend is explicit, because there is no universally correct default:

```python
hx.rate_limit(10, 60, on_backend_error="allow")   # default
hx.rate_limit(10, 60, on_backend_error="deny")
```

`"allow"` is the default because on most routes a limit is capacity protection, not security,
and turning a cache outage into a full outage is worse. On authentication routes the answer
inverts — Darwin's `sign_in_rate_limit` uses `"deny"`, because a Redis outage should not
become unlimited credential stuffing.

Behind a proxy the IP the app sees is the proxy's:

```python
from hexcore.infrastructure.api.rate_limit import forwarded_ip_key

hx.rate_limit(10, 60, key=forwarded_ip_key(trusted_proxies={"10.0.0.1"}, trust_hops=1))
```

`trusted_proxies` is mandatory: without the list, `X-Forwarded-For` is written by the client
and the limit is bypassed by changing a header.

---

## Correlated request IDs

```python
import logging

logging.basicConfig(level=logging.INFO)          # first: configure logging
hx.install_request_id_logging(fmt="%(asctime)s [%(request_id)s] %(message)s")
```

`RequestIDMiddleware` **reuses the incoming header when there is one** — breaking the
gateway's chain loses the trace — and publishes it to a `ContextVar` and `request.state`.
`install_request_id_logging()` injects it into every log line, which is half the value:
without that, having the header correlates nothing.

⚠️ **Order matters.** It instruments the handlers that **already exist**. In a process where
nobody configured logging yet there are none, so the call has nothing to do — and it warns
with a `RuntimeWarning` rather than staying silent.

`hx.get_request_id()` reads it from anywhere in the request.

---

## Domain exceptions to HTTP

```python
hx.register_exception_handlers(app, mapping={TicketNotFound: 404})
```

`hx.DEFAULT_EXCEPTION_STATUS_MAP` is the framework's base mapping. `create_app` merges it with
identity's and then with yours (`exception_mapping=`), so **your map wins**. `include_detail`
accepts `False` to avoid leaking the exception message, or a callable deciding per exception.
`headers_for` adds per-exception headers — what Darwin uses for `WWW-Authenticate` on its
401s.

---

## Streaming

```python
@router.get("/events")
async def events():
    return hx.sse_stream(my_generator(), heartbeat_seconds=30)
```

The heartbeat is an SSE comment clients ignore and proxies count as traffic: without it, a
load balancer with an idle timeout cuts the connection. `X-Accel-Buffering: no` is added too,
without which nginx buffers the events and the stream arrives in chunks.

```python
async with hx.connection_slot(cache, f"ws:{user_id}", max_connections=3) as granted:
    if not granted:
        await ws.close(code=1013)
        return
    await ws.accept()
    async with hx.ws_heartbeat(ws, interval=30):
        ...
```

`connection_slot` releases the slot **even if the block raises or is cancelled**. Leaking one
on a bad disconnect leaves the user unable to reconnect until the TTL expires, and it is the
classic bug of these limits.

---

## Router composition

```python
admin = hx.build_root_router(
    "/admin",
    {"/users": users_router, "/reports": reports_router},
    dependencies=[Depends(require_admin)],
    tags=["admin"],
)

hx.mount_routers(app, [admin, (public_router, {"prefix": "/v1"})])
```

`children` accepts a **dict** `{prefix: router}` or a **sequence**, and that is not sugar: a
dict cannot have two `""` keys, so a root whose children already carry their own prefix — the
normal case when each feature declares its full routes — cannot be expressed as a map.

```python
api_v1 = hx.build_root_router("/api/v1", [users_router, tickets_router])
api_v1 = hx.build_root_router("/api/v1", [users_router, ("/reports", reports_router)])
```

---

## List and search endpoints

```python
hx.register_query_endpoint(
    router,
    path="/tickets",
    use_case_factory=lambda: QueryEntitiesUseCase(repo),
    dependencies=[Depends(get_current_user)],
)
```

Generates a `GET` with `limit`, `offset`, `search`, `search_fields`, `filters`
(`field:operator:value`) and `sort` (`field:asc|desc`) as query parameters, documented in the
OpenAPI schema. An invalid field returns a **structured 422**:

```json
{"detail": {"message": "…", "field": "no_such_field", "allowed": ["title", "status"]}}
```

`hx.build_query_endpoint` returns the function without registering it, to mount yourself.

The query use cases live at `hexcore.application.use_cases.query`: `QueryEntitiesUseCase`,
`ListEntitiesUseCase`, `SearchEntitiesUseCase`. They are the one place a repository is
injected into a use case directly — a read-side helper, not a pattern to copy for mutations.

---

## Dependencies and providers

| Dependency | Yields |
| :-- | :-- |
| `hx.get_session` | An `AsyncSession` |
| `hx.get_sql_uow` | The UoW **not entered** — the use case does its own `async with` |
| `hx.get_sql_uow_open` | The UoW already entered |
| `hx.get_nosql_uow` | The Beanie UoW |

These are FastAPI dependencies: they work in an endpoint and nowhere else. Workers, cron,
scripts and seeds use the scopes in `core.md`.

```python
container = hx.configure_cqrs(registry, enqueuer=enqueuer)   # once, at startup


@router.post("/tickets")
async def create(cmd: CreateTicket, bus=Depends(hx.provide_command_bus)):
    return await bus.dispatch(cmd)
```

`hx.provide_command_bus`, `hx.provide_query_bus`, `hx.provide_event_bus`,
`hx.provide_registry` and `hx.get_cqrs_container` exist as **functions** for exactly one
reason: so tests can replace them via `app.dependency_overrides`. `hx.reset_cqrs()` clears the
container between cases.

`container.build_consumer()` builds the worker's consumer on **the same** buses and serializer,
so there is no second source of truth between the web process and the worker.
