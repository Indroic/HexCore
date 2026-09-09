# API reference

Everything the facades export, grouped by topic. The facades are the public contract: what is
here is maintained, and what leaves goes through a deprecation cycle.

The long paths keep working and return **the same object**, so `hexcore.cqrs.Command` and
`hexcore.domain.cqrs.commands.Command` are identical.

---

## `hexcore.fastapi` (48 symbols) — extra `[api]`

```python
import hexcore.fastapi as hx
```

### Application and startup

| Symbol | What it is |
| :-- | :-- |
| `create_app(lifespan=None, *, features=None, routers=None, health_probes=None, exception_mapping=None, exception_headers=None, **fastapi_kwargs)` | The app factory |
| `AppFeatures` | The switches: `cors`, `request_id`, `timing`, `exception_handlers`, `health`, `auth_context`, `csrf` |
| `build_lifespan(*steps, on_error="raise")` | Orchestrates startup and shutdown |
| `StartupStep` | The protocol: `name`, `start()`, optional `stop()` |
| `SqlEngineStep`, `BeanieStep`, `EventBusStep`, `CacheStep`, `ProcrastinateStep`, `CronSeedStep`, `CallableStep` | The bundled steps |

### Health

| Symbol | What it is |
| :-- | :-- |
| `register_health_routes(app, *, path="/health", probes=None, liveness=True, readiness=True, readiness_path=None, response_factory=None)` | Registers `/health` and `/health/ready` |
| `HealthRoutes` | The same configuration as an object, for `AppFeatures(health=...)` |
| `check_health(*, deep=False, probes=None)` | The report, outside a route |
| `HealthReport`, `DependencyReport` | The report and its rows |
| `Probe(name, check, timeout=2.0, critical=True)` | A single probe |
| `default_probes()` | The probes HexCore assembles from the installed extras |
| `ResponseFactory` | The type of `response_factory` |

### Middleware and correlation

| Symbol | What it is |
| :-- | :-- |
| `RequestIDMiddleware` | Publishes and reuses `X-Request-ID` |
| `TimingMiddleware` | Adds `X-Response-Time` |
| `get_request_id()` | The current request's ID |
| `install_request_id_logging(logger=None, *, fmt=None)` | Injects the ID into every log line |
| `RequestIDLogFilter` | The filter, if you build logging by hand |

### Exceptions

| Symbol | What it is |
| :-- | :-- |
| `register_exception_handlers(app, *, mapping=None, include_detail=True, headers_for=None)` | Maps domain exceptions to HTTP |
| `DEFAULT_EXCEPTION_STATUS_MAP` | The framework's base mapping |

### Rate limiting and streaming

| Symbol | What it is |
| :-- | :-- |
| `rate_limit(limit, window_seconds=60, *, key=None, cache=None, on_backend_error="allow", namespace=...)` | Rate-limit dependency |
| `client_ip_key(request)` | The default key |
| `sse_stream(source, *, heartbeat_seconds=30.0, **response_kwargs)` | SSE response with heartbeat |
| `format_sse_event(payload)` | A single formatted event |
| `ws_heartbeat(ws, interval=30.0)` | Periodic WebSocket ping |
| `connection_slot(cache, key, *, max_connections, ttl_seconds=3600)` | Concurrent connection cap |

### Routers and queries

| Symbol | What it is |
| :-- | :-- |
| `build_root_router(prefix, children, *, dependencies=(), tags=None, **router_kwargs)` | Composes a root router |
| `mount_routers(app, routers)` | Mounts routers, with or without kwargs |
| `register_query_endpoint(router, *, path, use_case_factory, ...)` | List and search endpoint |
| `build_query_endpoint(use_case_factory, *, extra_params=None)` | The function, unregistered |

### Session, UoW and CQRS

| Symbol | What it is |
| :-- | :-- |
| `get_session` | Dependency: an `AsyncSession` |
| `get_sql_uow` | The UoW **not entered** |
| `get_sql_uow_open` | The UoW already entered |
| `get_nosql_uow` | The Beanie UoW |
| `configure_cqrs(registry, *, config=None, enqueuer=None)` | Builds the container |
| `CQRSContainer` | `registry`, `serializer`, the three buses, `build_consumer()` |
| `get_cqrs_container()`, `reset_cqrs()` | Access and cleanup |
| `provide_command_bus`, `provide_query_bus`, `provide_event_bus`, `provide_registry` | The providers, overridable in tests |

---

## `hexcore.cqrs` (64 symbols) — no extras

```python
import hexcore.cqrs as cqrs
```

### Messages and handlers

| Symbol | What it is |
| :-- | :-- |
| `Command`, `Query`, `DomainEvent` | The three message kinds. All frozen |
| `AbstractCommandHandler`, `AbstractQueryHandler` | The handler contracts |
| `UseCaseCommandHandler` | Adapts a `UseCase` into a handler |
| `HandlerRegistry(*, allow_override=False)` | The registry; `.factory(build)` marks a factory |
| `HandlerFactory` | What `HandlerRegistry.factory` returns |

### Buses

| Symbol | What it is |
| :-- | :-- |
| `AbstractCommandBus`, `AbstractQueryBus`, `AbstractEventBus` | The contracts |
| `InMemoryCommandBus`, `InMemoryQueryBus`, `InMemoryEventBus` | The local implementations |
| `CQRSFactory(config, registry, enqueuer=None)` | Builds all three, consistent with each other |
| `CQRSConfig`, `BusConfig` | The declarative configuration |

### Middleware

| Symbol | What it is |
| :-- | :-- |
| `AbstractMiddleware`, `NextHandler`, `MiddlewarePipeline` | The contract and the pipeline |
| `LoggingMiddleware`, `ValidationMiddleware`, `RetryMiddleware` | The bundled ones |
| `TransactionMiddleware(uow_factory=...)` | Opens and commits a UoW. **Not the default** |

### Serialization and errors

| Symbol | What it is |
| :-- | :-- |
| `AbstractSerializer`, `PydanticSerializer` | The contract and the implementation |
| `CQRSError` | The base |
| `HandlerNotFoundError`, `DuplicateHandlerError`, `DeserializationError` | The cases |

### Background and worker

| Symbol | What it is |
| :-- | :-- |
| `background_command(queue="default")`, `background_handler(...)`, `background_task(...)` | The decorators |
| `ITaskEnqueuer` | The queue port |
| `CQRSConsumer(command_bus, event_bus=None, serializer=None)` | The worker side |
| `run_cqrs_worker(*loops, scheduler=None, on_startup=(), on_shutdown=(), handle_signals=True, drain_timeout=30.0)` | The generic runner |
| `run_procrastinate_worker(app, *, queues=None, concurrency=4, scheduler=None, ...)` | The Procrastinate runner |
| `worker_loop(name, run, stop=None)` | Wraps a pair of callables |
| `WorkerDied` | The mutual-death exception |
| `is_worker_execution()`, `worker_execution()` | The execution context |

### The envelope

| Symbol | What it is |
| :-- | :-- |
| `register_envelope_metadata_provider(key, provider)` | What to attach when enqueuing |
| `register_envelope_restorer(key, restorer)` | How to reinstall it in the worker |
| `AbstractEnvelopeRestorer`, `EnvelopeMetadataProvider` | The contracts |
| `collect_envelope_metadata()`, `restored_envelope_scope(...)` | Collect and restore |
| `message_correlation_id()` | The `cid` of the message in flight |
| `registered_envelope_keys()`, `unregister_envelope_key(key)`, `clear_envelope_registry()` | Diagnostics and cleanup |
| `ENVELOPE_METADATA_KEY` | The key it travels under |

### Cron and locks

| Symbol | What it is |
| :-- | :-- |
| `cron_job(task, cron_expression, *, job_id=None, payload=None, queue=None, is_active=True, description=None)` | Defines a job |
| `CronJobDefinition` | What it returns |
| `ICronJobRepository` | The job repository port |
| `SqlAlchemyCronJobRepository`, `CronJobModel`, `CronJobModelMixin` | The SQL backend |
| `create_cron_tables(engine=None, *, model=CronJobModel)` | Creates the table |
| `seed_cron_jobs(jobs, *, model=CronJobModel, session_scope=None)` | Seeds, without overwriting edits |
| `DynamicScheduler(repository, enqueuer, lock_provider=None, tick_interval_seconds=30, *, catch_up_window_seconds=3600)` | The scheduler |
| `ILockProvider`, `RedisLockProvider`, `PostgresLockProvider` | The distributed locks |

---

## `hexcore.sql` (27 symbols) — extra `[sql]`

```python
import hexcore.sql as sql
```

| Symbol | What it is |
| :-- | :-- |
| `init_engine(url=None, *, pool=None, **engine_kwargs)` | Creates the global engine |
| `dispose_engine()` | Disposes it |
| `get_engine()`, `get_session_factory()` | Access |
| `PoolSettings(size=None, max_overflow=None, pre_ping=True, recycle=1800)` | The pool |
| `normalize_async_dsn(url)` | `postgresql://` → `postgresql+asyncpg://` |
| `session_scope()`, `uow_scope()`, `open_uow_scope()`, `nosql_uow_scope()` | The context managers |
| `SqlAlchemyUnitOfWork` | The UoW |
| `Base`, `BaseModel`, `NAMING_CONVENTION` | The declarative base and its naming convention |
| `SqlAlchemyRepository`, `BaseSQLAlchemyRepository` | The repositories |
| `import_all_models(package)`, `ensure_framework_models_loaded()` | For Alembic's `env.py` |
| `QueryRequestDTO`, `QueryResponseDTO` | Listing with `limit`/`offset` |
| `FilterConditionDTO`, `FilterOperator`, `SortConditionDTO`, `SortDirection` | Filters and sorting |
| `CursorRequestDTO`, `CursorPageDTO` | Cursor pagination |
| `UnsupportedQueryFieldError` | Invalid field in a query |

---

## `hexcore.darwin` (192 symbols) — extra `[darwin]`

Too many for a useful table. The surface is grouped like this:

| Group | Examples |
| :-- | :-- |
| Configuration and container | `IdentityConfig`, `TokenConfig`, `CookieConfig`, `PasswordPolicy`, `configure_identity`, `get_identity_container`, `reset_identity` |
| Commands | `SignUp`, `SignIn`, `VerifyEmail`, `RefreshSession`, `SignOut`, `SignOutEverywhere`, `ChangePassword`, `AuthenticateToken`, `ListActiveSessions` |
| Context | `AuthContext`, `Principal`, `SystemPrincipal`, `Impersonation`, `current_auth`, `require_auth`, `auth_scope`, `system_context` |
| Entities and value objects | `User`, `IdentitySession`, `Account`, `Verification`, `Email`, `TokenPair`, `AccessTokenClaims` |
| Events | `UserRegisteredEvent`, `UserSignedInEvent`, `SessionCreatedEvent`, `SessionReuseDetectedEvent`, `ImpersonationStartedEvent`, … |
| Exceptions | `IdentityError` and its 15 subclasses, plus `IDENTITY_EXCEPTION_STATUS_MAP` |
| Permissions | `Role`, `Permission`, `RoleRegistry`, `default_registry` |
| Ports | `AbstractUserRepository`, `AbstractSessionRepository`, `AbstractClock`, `AbstractPasswordHasher`, `AbstractRevocationList`, `AbstractAuditSink`, … |
| API | `build_identity_router`, `provide_auth`, `require_authenticated`, `require_scopes`, `require_roles`, `require_not_impersonated`, `AuthContextMiddleware`, `CsrfMiddleware` |
| Infrastructure | `JoserfcTokenIssuer`, `Argon2PasswordHasher`, `CookieTransport`, `BearerTransport`, `SystemClock`, `FixedClock`, `StaticKeyStore` |
| Plugins | `DarwinPlugin`, `PluginRegistry`, `HookBinding`, `HookPhase`, `ShortCircuit`, `identity_action` |
| SQL storage | `UserMixin`, `SessionMixin`, `IDENTITY_MODELS`, `ensure_identity_schema_loaded`, `create_identity_tables`, `validate_user_model` |

The exact, typed listing is in the generated stub
[`hexcore/darwin/__init__.pyi`](../../hexcore/darwin/__init__.pyi), which is the source type
checkers read. See **[Darwin](./darwin/)** for the guides.

---

## Modules without a facade

Some things are imported by their long path because they are niche, and giving them a short name
on a facade would suggest they are part of the happy path:

| Path | What it holds |
| :-- | :-- |
| `hexcore.capabilities` | `has_extra`, `require_extra`, `installed_extras`, `EXTRA_DE` |
| `hexcore.config` | `ServerConfig`, `LazyConfig` |
| `hexcore.testing` | The testing doubles and helpers |
| `hexcore.testing.fixtures` | The pytest fixtures |
| `hexcore.domain.base` | `BaseEntity` |
| `hexcore.domain.events` | `DomainEvent`, `EntityCreatedEvent`, `EntityUpdatedEvent`, `EntityDeletedEvent`, `EventBus` |
| `hexcore.domain.repositories` | `IBaseRepository` |
| `hexcore.domain.uow` | `IUnitOfWork` |
| `hexcore.application.use_cases.base` | `UseCase` |
| `hexcore.application.use_cases.query` | `QueryEntitiesUseCase`, `ListEntitiesUseCase`, `SearchEntitiesUseCase` |
| `hexcore.infrastructure.repositories.implementations` | `SqlAlchemyRepository`, `BeanieRepository` |
| `hexcore.infrastructure.uow` | `SqlAlchemyUnitOfWork`, `BeanieUnitOfWork` |
| `hexcore.infrastructure.cqrs.redis_bus` | `RedisEventBus` |
| `hexcore.infrastructure.cqrs.postgres_bus` | `PostgresEventBus` |
| `hexcore.infrastructure.cqrs.rabbitmq` | `RabbitMQEventBus` |
| `hexcore.infrastructure.task_queues.procrastinate_adapter` | `ProcrastinateEnqueuer`, `register_hexcore_procrastinate_tasks` |
| `hexcore.infrastructure.task_queues.celery_adapter` | `CeleryEnqueuer`, `register_hexcore_celery_tasks`, `run_in_worker_loop` |
| `hexcore.infrastructure.cache` | `ICache` and its backends |
| `hexcore.infrastructure.cli` | The Typer app |

---

## Next

→ **[Versions and migration](./versions-and-migration.md)**.
