# Referencia de API

Todo lo que exportan las fachadas, agrupado por tema. Las fachadas son el contrato público: lo
que está acá se mantiene, y lo que se saca pasa por un ciclo de deprecación.

Las rutas largas siguen funcionando y devuelven **el mismo objeto**, así que
`hexcore.cqrs.Command` y `hexcore.domain.cqrs.commands.Command` son idénticos.

---

## `hexcore.fastapi` (48 símbolos) — extra `[api]`

```python
import hexcore.fastapi as hx
```

### Aplicación y arranque

| Símbolo | Qué es |
| :-- | :-- |
| `create_app(lifespan=None, *, features=None, routers=None, health_probes=None, exception_mapping=None, exception_headers=None, **fastapi_kwargs)` | La factory de la app |
| `AppFeatures` | Los interruptores: `cors`, `request_id`, `timing`, `exception_handlers`, `health`, `auth_context`, `csrf` |
| `build_lifespan(*steps, on_error="raise")` | Orquesta arranque y apagado |
| `StartupStep` | El protocolo: `name`, `start()`, `stop()` opcional |
| `SqlEngineStep`, `BeanieStep`, `EventBusStep`, `CacheStep`, `ProcrastinateStep`, `CronSeedStep`, `CallableStep` | Los steps incluidos |

### Salud

| Símbolo | Qué es |
| :-- | :-- |
| `register_health_routes(app, *, path="/health", probes=None, liveness=True, readiness=True, readiness_path=None, response_factory=None)` | Registra `/health` y `/health/ready` |
| `HealthRoutes` | La misma configuración, como objeto, para `AppFeatures(health=...)` |
| `check_health(*, deep=False, probes=None)` | El informe, fuera de una ruta |
| `HealthReport`, `DependencyReport` | El informe y sus filas |
| `Probe(name, check, timeout=2.0, critical=True)` | Una sonda |
| `default_probes()` | Las sondas que HexCore arma según los extras instalados |
| `ResponseFactory` | El tipo del `response_factory` |

### Middlewares y correlación

| Símbolo | Qué es |
| :-- | :-- |
| `RequestIDMiddleware` | Publica y reusa `X-Request-ID` |
| `TimingMiddleware` | Agrega `X-Response-Time` |
| `get_request_id()` | El id del request en curso |
| `install_request_id_logging(logger=None, *, fmt=None)` | Inyecta el id en cada línea de log |
| `RequestIDLogFilter` | El filtro, por si armás el logging a mano |

### Excepciones

| Símbolo | Qué es |
| :-- | :-- |
| `register_exception_handlers(app, *, mapping=None, include_detail=True, headers_for=None)` | Mapea excepciones de dominio a HTTP |
| `DEFAULT_EXCEPTION_STATUS_MAP` | El mapeo base del framework |

### Rate limiting y streaming

| Símbolo | Qué es |
| :-- | :-- |
| `rate_limit(limit, window_seconds=60, *, key=None, cache=None, on_backend_error="allow", namespace=...)` | Dependencia de límite |
| `client_ip_key(request)` | La clave por defecto |
| `sse_stream(source, *, heartbeat_seconds=30.0, **response_kwargs)` | Respuesta SSE con heartbeat |
| `format_sse_event(payload)` | Un evento suelto, formateado |
| `ws_heartbeat(ws, interval=30.0)` | Ping periódico de WebSocket |
| `connection_slot(cache, key, *, max_connections, ttl_seconds=3600)` | Límite de conexiones concurrentes |

### Routers y queries

| Símbolo | Qué es |
| :-- | :-- |
| `build_root_router(prefix, children, *, dependencies=(), tags=None, **router_kwargs)` | Compone un router raíz |
| `mount_routers(app, routers)` | Monta routers, con o sin kwargs |
| `register_query_endpoint(router, *, path, use_case_factory, ...)` | Endpoint de listado y búsqueda |
| `build_query_endpoint(use_case_factory, *, extra_params=None)` | La función, sin registrarla |

### Sesión, UoW y CQRS

| Símbolo | Qué es |
| :-- | :-- |
| `get_session` | Dependencia: una `AsyncSession` |
| `get_sql_uow` | El UoW **sin abrir** |
| `get_sql_uow_open` | El UoW ya abierto |
| `get_nosql_uow` | El UoW de Beanie |
| `configure_cqrs(registry, *, config=None, enqueuer=None)` | Arma el contenedor |
| `CQRSContainer` | `registry`, `serializer`, los tres buses, `build_consumer()` |
| `get_cqrs_container()`, `reset_cqrs()` | Acceso y limpieza |
| `provide_command_bus`, `provide_query_bus`, `provide_event_bus`, `provide_registry` | Los providers, sobreescribibles en tests |

---

## `hexcore.cqrs` (64 símbolos) — sin extras

```python
import hexcore.cqrs as cqrs
```

### Mensajes y handlers

| Símbolo | Qué es |
| :-- | :-- |
| `Command`, `Query`, `DomainEvent` | Los tres tipos de mensaje. Todos `frozen` |
| `AbstractCommandHandler`, `AbstractQueryHandler` | Los contratos de handler |
| `UseCaseCommandHandler` | Adapta un `UseCase` a un handler |
| `HandlerRegistry(*, allow_override=False)` | El registro; `.factory(build)` marca un factory |
| `HandlerFactory` | Lo que devuelve `HandlerRegistry.factory` |

### Buses

| Símbolo | Qué es |
| :-- | :-- |
| `AbstractCommandBus`, `AbstractQueryBus`, `AbstractEventBus` | Los contratos |
| `InMemoryCommandBus`, `InMemoryQueryBus`, `InMemoryEventBus` | Las implementaciones locales |
| `CQRSFactory(config, registry, enqueuer=None)` | Construye los tres, coherentes entre sí |
| `CQRSConfig`, `BusConfig` | La configuración declarativa |

### Middlewares

| Símbolo | Qué es |
| :-- | :-- |
| `AbstractMiddleware`, `NextHandler`, `MiddlewarePipeline` | El contrato y el pipeline |
| `LoggingMiddleware`, `ValidationMiddleware`, `RetryMiddleware` | Los incluidos |
| `TransactionMiddleware(uow_factory=...)` | Abre y comitea un UoW. **No es default** |

### Serialización y errores

| Símbolo | Qué es |
| :-- | :-- |
| `AbstractSerializer`, `PydanticSerializer` | El contrato y la implementación |
| `CQRSError` | La base |
| `HandlerNotFoundError`, `DuplicateHandlerError`, `DeserializationError` | Los casos |

### Background y worker

| Símbolo | Qué es |
| :-- | :-- |
| `background_command(queue="default")`, `background_handler(...)`, `background_task(...)` | Los decoradores |
| `ITaskEnqueuer` | El puerto de la cola |
| `CQRSConsumer(command_bus, event_bus=None, serializer=None)` | El lado worker |
| `run_cqrs_worker(*loops, scheduler=None, on_startup=(), on_shutdown=(), handle_signals=True, drain_timeout=30.0)` | El runner genérico |
| `run_procrastinate_worker(app, *, queues=None, concurrency=4, scheduler=None, ...)` | El runner de Procrastinate |
| `worker_loop(name, run, stop=None)` | Envuelve un par de callables |
| `WorkerDied` | La excepción de muerte mutua |
| `is_worker_execution()`, `worker_execution()` | El contexto de ejecución |

### El sobre (envelope)

| Símbolo | Qué es |
| :-- | :-- |
| `register_envelope_metadata_provider(key, provider)` | Qué agregar al encolar |
| `register_envelope_restorer(key, restorer)` | Cómo reinstalarlo en el worker |
| `AbstractEnvelopeRestorer`, `EnvelopeMetadataProvider` | Los contratos |
| `collect_envelope_metadata()`, `restored_envelope_scope(...)` | Recolectar y restaurar |
| `message_correlation_id()` | El `cid` del mensaje en curso |
| `registered_envelope_keys()`, `unregister_envelope_key(key)`, `clear_envelope_registry()` | Diagnóstico y limpieza |
| `ENVELOPE_METADATA_KEY` | La clave bajo la que viaja |

### Cron y locks

| Símbolo | Qué es |
| :-- | :-- |
| `cron_job(task, cron_expression, *, job_id=None, payload=None, queue=None, is_active=True, description=None)` | Define un job |
| `CronJobDefinition` | Lo que devuelve |
| `ICronJobRepository` | El puerto del repositorio de jobs |
| `SqlAlchemyCronJobRepository`, `CronJobModel`, `CronJobModelMixin` | El backend SQL |
| `create_cron_tables(engine=None, *, model=CronJobModel)` | Crea la tabla |
| `seed_cron_jobs(jobs, *, model=CronJobModel, session_scope=None)` | Siembra, sin pisar lo editado |
| `DynamicScheduler(repository, enqueuer, lock_provider=None, tick_interval_seconds=30, *, catch_up_window_seconds=3600)` | El scheduler |
| `ILockProvider`, `RedisLockProvider`, `PostgresLockProvider` | Los locks distribuidos |

---

## `hexcore.sql` (27 símbolos) — extra `[sql]`

```python
import hexcore.sql as sql
```

| Símbolo | Qué es |
| :-- | :-- |
| `init_engine(url=None, *, pool=None, **engine_kwargs)` | Crea el engine global |
| `dispose_engine()` | Lo cierra |
| `get_engine()`, `get_session_factory()` | Acceso |
| `PoolSettings(size=None, max_overflow=None, pre_ping=True, recycle=1800)` | El pool |
| `normalize_async_dsn(url)` | `postgresql://` → `postgresql+asyncpg://` |
| `session_scope()`, `uow_scope()`, `open_uow_scope()`, `nosql_uow_scope()` | Los context managers |
| `SqlAlchemyUnitOfWork` | El UoW |
| `Base`, `BaseModel`, `NAMING_CONVENTION` | La base declarativa y su convención de nombres |
| `SqlAlchemyRepository`, `BaseSQLAlchemyRepository` | Los repositorios |
| `import_all_models(package)`, `ensure_framework_models_loaded()` | Para el `env.py` de Alembic |
| `QueryRequestDTO`, `QueryResponseDTO` | Listado con `limit`/`offset` |
| `FilterConditionDTO`, `FilterOperator`, `SortConditionDTO`, `SortDirection` | Filtros y orden |
| `CursorRequestDTO`, `CursorPageDTO` | Paginación por cursor |
| `UnsupportedQueryFieldError` | Campo inválido en una query |

---

## `hexcore.darwin` (192 símbolos) — extra `[darwin]`

Demasiados para una tabla útil. La superficie está agrupada así:

| Grupo | Ejemplos |
| :-- | :-- |
| Configuración y contenedor | `IdentityConfig`, `TokenConfig`, `CookieConfig`, `PasswordPolicy`, `configure_identity`, `get_identity_container`, `reset_identity` |
| Comandos | `SignUp`, `SignIn`, `VerifyEmail`, `RefreshSession`, `SignOut`, `SignOutEverywhere`, `ChangePassword`, `AuthenticateToken`, `ListActiveSessions` |
| Contexto | `AuthContext`, `Principal`, `SystemPrincipal`, `Impersonation`, `current_auth`, `require_auth`, `auth_scope`, `system_context` |
| Entidades y VOs | `User`, `IdentitySession`, `Account`, `Verification`, `Email`, `TokenPair`, `AccessTokenClaims` |
| Eventos | `UserRegisteredEvent`, `UserSignedInEvent`, `SessionCreatedEvent`, `SessionReuseDetectedEvent`, `ImpersonationStartedEvent`, … |
| Excepciones | `IdentityError` y sus 15 subclases, más `IDENTITY_EXCEPTION_STATUS_MAP` |
| Permisos | `Role`, `Permission`, `RoleRegistry`, `default_registry` |
| Puertos | `AbstractUserRepository`, `AbstractSessionRepository`, `AbstractClock`, `AbstractPasswordHasher`, `AbstractRevocationList`, `AbstractAuditSink`, … |
| API | `build_identity_router`, `provide_auth`, `require_authenticated`, `require_scopes`, `require_roles`, `require_not_impersonated`, `AuthContextMiddleware`, `CsrfMiddleware` |
| Infraestructura | `JoserfcTokenIssuer`, `Argon2PasswordHasher`, `CookieTransport`, `BearerTransport`, `SystemClock`, `FixedClock`, `StaticKeyStore` |
| Plugins | `DarwinPlugin`, `PluginRegistry`, `HookBinding`, `HookPhase`, `ShortCircuit`, `identity_action` |
| Almacenamiento SQL | `UserMixin`, `SessionMixin`, `IDENTITY_MODELS`, `ensure_identity_schema_loaded`, `create_identity_tables`, `validate_user_model` |

El listado exacto y tipado está en el stub generado
[`hexcore/darwin/__init__.pyi`](../../../packages/hexcore/hexcore/darwin/__init__.pyi), que es la fuente que ven los
type checkers. Ver **[Darwin](./darwin/)** para las guías.

---

## `hexcore.eventsourcing` (53 símbolos) — los extras dependen del adaptador

El event store. Los puertos y el agregado no necesitan extras; cada adaptador exige el suyo
**en el momento exacto en que se lo pide**, que es lo que permite que SQL, Mongo y Redis
convivan en la misma fachada.

| Grupo | Símbolos | Extra |
| :-- | :-- | :-- |
| Agregado | `AggregateRoot`, `when`, `EventRecorder` | — |
| El evento persistido | `StoredEvent`, `EXPECTED_VERSION_ANY`, `EXPECTED_VERSION_NO_STREAM` | — |
| Puertos | `AbstractEventStore`, `AbstractSnapshotStore`, `Snapshot`, `AbstractProjection`, `AbstractCheckpointStore` | — |
| Excepciones | `EventSourcingError`, `ConcurrencyError`, `AggregateNotFoundError`, `UnhandledEventError` | — |
| Aplicación | `EventSourcedRepository`, `Projector`, `EventStoreRelay` | — |
| Configuración | `EventStoreConfig`, `SnapshotConfig`, `ProjectionsConfig`, `EventStoreFactory` | — |
| En memoria | `InMemoryEventStore`, `InMemorySnapshotStore`, `InMemoryCheckpointStore` | — |
| SQLAlchemy | `SqlAlchemyEventStore`, `SqlAlchemySnapshotStore`, `SqlAlchemyCheckpointStore`, `EventStoreModel`, `SnapshotModel`, `ProjectionCheckpointModel`, `EventStoreMixin`, `SnapshotMixin`, `ProjectionCheckpointMixin`, `create_eventstore_tables`, `drop_eventstore_tables` | `[sql]` |
| Beanie | `BeanieEventStore`, `BeanieSnapshotStore`, `BeanieCheckpointStore`, `StoredEventDocument`, `SnapshotDocument`, `ProjectionCheckpointDocument`, `init_eventstore_documents` | `[mongo]` |
| Redis | `RedisEventStore`, `RedisCheckpointStore` | `[redis]` |
| Contenedor y providers | `EventStoreContainer`, `configure_event_store`, `get_event_store_container`, `reset_event_store`, `provide_event_store`, `provide_snapshot_store`, `provide_checkpoint_store`, `provide_projector` | — |

El listado exacto y tipado está en el stub generado
[`hexcore/eventsourcing.pyi`](../../../packages/hexcore/hexcore/eventsourcing.pyi). Ver
**[Event Sourcing](./event-sourcing.md)** para la guía.

---

## Módulos que no tienen fachada

Algunas cosas se importan por su ruta larga porque son de nicho, y darles un nombre corto en la
fachada sugeriría que son parte del camino feliz:

| Ruta | Qué trae |
| :-- | :-- |
| `hexcore.capabilities` | `has_extra`, `require_extra`, `installed_extras`, `EXTRA_DE` |
| `hexcore.config` | `ServerConfig`, `LazyConfig` |
| `hexcore.testing` | Los dobles y helpers de test |
| `hexcore.testing.fixtures` | Las fixtures de pytest |
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
| `hexcore.infrastructure.cache` | `ICache` y sus backends |
| `hexcore.infrastructure.cli` | La app de Typer |

---

## Siguiente

→ **[Versiones y migración](./versiones-y-migracion.md)**.
