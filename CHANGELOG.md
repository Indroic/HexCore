## Unreleased

### Fix

- **cqrs**: el worker ya **ejecuta** los `@background_command` que saca de la cola en
  vez de reencolarlos. Antes, pasarle al `CQRSConsumer` el mismo bus que usa el
  proceso web —lo natural, y lo que sugería la documentación— producía un bucle
  infinito silencioso: la cola crecía sin límite y el handler no corría jamás.
  Nuevo contextvar `hexcore.domain.cqrs.context.IN_WORKER` (P0-1)
- **cqrs**: mismo arreglo para los suscriptores marcados con `@background_handler`
  cuando el evento llega por `CQRSConsumer.process_event` (P0-2)
- **cqrs**: los FQN con `__qualname__` anidado (clases dentro de clases, tasks como
  `@staticmethod`) ya se resuelven bien. Antes `rsplit(".", 1)` producía un module
  path inválido y el mensaje se encolaba correctamente para **fallar en el worker**.
  Nuevo helper `hexcore.domain.cqrs.resolution.resolve_dotted`, usado por
  `PydanticSerializer.deserialize` y por `_resolve_callable` del consumer (P0-3)
- **cqrs**: `@background_command` / `@background_handler` / `@background_task` ahora
  rechazan en tiempo de decoración los objetos con `<locals>` en su `__qualname__`:
  una función definida dentro de otra función nunca será importable desde el worker
  (P0-3)
- **cqrs**: `PostgresLockProvider` ya no filtra filas indefinidamente. Con una clave
  por `(job_id, minuto)` y 7 jobs eran ~10.000 filas/día **para siempre** en la BD
  principal. Ahora purga lo expirado en `setup()` y cada `purge_every` adquisiciones
  (100 por defecto), crea un índice sobre `expires_at`, y expone `purge_expired()`
  (P0-4)

### Feat

- **cqrs**: `DynamicScheduler` implementa el catch-up que la versión anterior sólo
  insinuaba: decide por "¿hubo alguna ocurrencia entre la última ejecución y ahora?"
  en vez de `croniter.match(expr, minuto_actual)`. Con eso un minuto saltado por
  drift del tick ya no pierde la ejecución, y `update_last_run` deduplica de verdad
  cuando `tick_interval_seconds < 60`. Nuevo `catch_up_window_seconds` (1h) para
  acotar el catch-up, aviso `RuntimeWarning` si el tick es sub-minuto y no hay
  `lock_provider`, primer tick sin esperar el intervalo, `stop()` interrumpible al
  instante, y `except` que loguean traceback en vez de tragarse el error (P1-1)
- **api**: `register_exception_handlers(app, mapping=..., include_detail=...)` mapea las
  excepciones de dominio a HTTP. El default cubre `InactiveEntityException`→409,
  `DeserializationError`→400, `HandlerNotFoundError`→501 y `ValueError`→422 — este último
  antes sólo se capturaba **dentro** de `build_query_endpoint`, así que una query
  construida a mano devolvía 500 (F5)
- **api**: providers CQRS en `hexcore.infrastructure.api.cqrs`: `configure_cqrs()`,
  `provide_command_bus` / `provide_query_bus` / `provide_event_bus` / `provide_registry`
  y `CQRSContainer.build_consumer()`, que construye el consumer del worker sobre los
  mismos buses que la app web — una sola fuente de verdad. Son funciones para poder
  sobreescribirlos con `app.dependency_overrides` en los tests (F6)
- **api**: `rate_limit(limit, window_seconds, key=..., cache=...)` como dependencia,
  sobre el puerto `ICache` (funciona con `MemoryCache` en tests). Devuelve **429 con
  `Retry-After`**, y la política ante un backend caído es explícita
  (`on_backend_error="allow" | "deny"`) en vez de una decisión enterrada en un `except`
  (F12)
- **api**: `sse_stream()` con heartbeat y cabeceras anti-buffering, `format_sse_event()`,
  `ws_heartbeat()` y `connection_slot()` en `hexcore.infrastructure.api.streaming`.
  `connection_slot` libera el slot aunque el bloque lance o se cancele, que es el bug
  clásico de los límites de conexión por usuario (F14)
- **api**: `check_health(deep=...)` y `register_health_routes(app)`. `{path}` es liveness
  (200 sin I/O) y `{path}/ready` es readiness: sondea SQL, Redis y Mongo con timeout
  propio por sonda y devuelve **503** con el detalle y la latencia por dependencia. Una
  dependencia no crítica reporta `degraded` en vez de `down` (F16)
- **cqrs**: implementación SQL del cron de serie en
  `hexcore.infrastructure.cqrs.cron_sql` (extra `[sql]`): `CronJobModel` /
  `CronJobModelMixin`, `SqlAlchemyCronJobRepository`, `seed_cron_jobs()` idempotente y no
  destructivo, `create_cron_tables()` y `cron_job()`, que deriva el `task_name` de
  `__cqrs_task_name__` en vez de escribirlo a mano. El modelo no hereda de `BaseModel[T]`
  y el repositorio no hereda de `BaseSQLAlchemyRepository`, para que ni el UoW ni el
  auto-discovery los traten como dominio (F7)
- **workers**: `run_cqrs_worker(*loops, scheduler=..., on_startup=..., on_shutdown=...)`
  y el atajo `run_procrastinate_worker(app, queues=..., concurrency=...)`. Si cualquiera
  de los bucles muere se cancela el resto y el proceso sale con `WorkerDied`, para que el
  orquestador lo reinicie completo; incluye manejo de SIGTERM/SIGINT para drenaje
  ordenado (F8)
- **api**: `RequestIDMiddleware` y `TimingMiddleware` de serie, en
  `hexcore.infrastructure.api.middlewares`. El request-id se reusa del header entrante
  si viene, se expone por `ContextVar` (`get_request_id()`) y por `request.state`, y hay
  un `RequestIDLogFilter` + `install_request_id_logging()` que lo inyectan en cada línea
  de log — sin eso, tener el header no correlaciona nada (F11)
- **api**: `build_root_router(prefix, children, dependencies=..., tags=...)` y
  `mount_routers(app, routers)` en `hexcore.infrastructure.api.routing`, para declarar
  la composición de routers en vez de escribir un `*_root_router.py` por área (F13)
- **sql**: capa de sesión usable en producción. `init_engine(url=None, *, pool=PoolSettings(), **engine_kwargs)`
  y `dispose_engine()`; `PoolSettings` (`size`, `max_overflow`, `pre_ping`, `recycle`)
  con `pre_ping=True` por defecto; normalización del DSN (`postgresql://` →
  `postgresql+asyncpg://`, expuesta como `normalize_async_dsn`); URL explícita para
  workers, scripts y tests; y lock alrededor de los globals `_engine`/`_session_factory`
  (F2)
- **uow**: `session_scope`, `uow_scope`, `open_uow_scope` y `nosql_uow_scope` en
  `hexcore.infrastructure.uow` — scopes para workers, cron, scripts y seeds, donde
  antes sólo había dependencias de FastAPI. `session_scope` no construye el UoW, así
  que no paga el auto-discovery de repositorios para leer una tabla de infraestructura
  (F3)
- **api**: nueva dependencia `get_sql_uow_open` para endpoints que quieren el UoW ya
  abierto (F3)
- **cqrs**: `CQRSConsumer(command_bus)` — `event_bus` y `serializer` pasan a ser
  opcionales. Un worker sólo-comandos ya no necesita `cast(Any, None)`, y si llega un
  evento sin event bus el error dice qué hacer. `serializer` cae en
  `PydanticSerializer` (P2-2)
- **task_queues**: `register_hexcore_procrastinate_tasks` y
  `register_hexcore_celery_tasks` son idempotentes y devuelven `bool`; se añade
  `is_registered(app)` y `force=True`. Antes llamarlas dos veces reventaba porque la
  cola rechaza nombres duplicados, así que cada aplicación tenía que protegerlas con
  un flag de módulo (P1-6)
- **cqrs**: `HandlerRegistry` es realmente thread-safe: el docstring lo afirmaba pero
  no había ningún lock, y `resolve_*` hace lazy-init con escritura en el dict, así que
  dos hilos podían instanciar el mismo handler dos veces (relevante con el
  free-threading de Python 3.14). Nuevo `HandlerRegistry.factory(...)` como marcador
  explícito de factory, para quitar la ambigüedad con los handlers que implementan
  `__call__` (P1-5)
- **cqrs**: `RedisLockProvider` y `PostgresLockProvider` aceptan
  `on_error: "skip" | "raise"`. Antes capturaban `Exception` y devolvían `False`
  siempre, así que una caída de Redis apagaba el cron **entero** con un `logger.error`
  por job y por tick indistinguible del caso normal. Ahora "no pude decidir" se loguea
  como `critical` y "el lock estaba tomado" como `debug` (P1-2)
- **cqrs**: `CQRSFactory` acepta un `enqueuer` y lo propaga junto al serializer a los
  buses in-memory, así que la vía oficial de construcción ya sirve para Smart Routing
  sin cablear los buses a mano. Si hay `@background_command` registrados y falta el
  enqueuer, falla **al construir** con el nombre de los comandos afectados, no en el
  primer dispatch. El serializer se cachea para que buses y consumer compartan
  instancia (P0-5)

### Behavior change

- **sql**: el session factory de HexCore pasa a `expire_on_commit=False`. Con el
  default de SQLAlchemy (`True`) los atributos de las entidades expiran al comitear y
  el siguiente acceso dispara un lazy-load sobre una sesión cerrada
  (`MissingGreenlet` / `DetachedInstanceError`) — y la documentación ya enseñaba
  `async_sessionmaker(engine, expire_on_commit=False)`, o sea que doc e
  implementación no coincidían. Quien necesite `True` debe construir su propio
  `async_sessionmaker` (F2)
- **api**: `get_sql_uow` ya **no entra** al UoW: cede el UoW sin abrir la transacción,
  para que el use case controle su propio `async with uow:` sin anidar contextos. Si
  dependías del comportamiento anterior, usá `get_sql_uow_open` (F3)
- **cqrs**: `TransactionMiddleware` deja de ser el middleware por defecto de
  `CQRSConfig.command_bus`, y `TransactionMiddleware()` sin `uow_factory` ahora lanza
  `ValueError` en vez de adivinar. El default armaba la sesión con el session factory
  *interno* de HexCore en vez del engine de la aplicación, y comiteaba después del
  handler — así que un handler que ya comitea (el patrón que enseña la doc para los
  use cases) comiteaba dos veces (P0-6)
- **task_queues**: `enqueue_event` de los adaptadores de Procrastinate y Celery lanza
  `NotImplementedError` con instrucciones en vez de ser un `pass` que perdía el
  evento sin traza (P1-3)

### Test

- **cqrs**: los tests del consumer parcheaban `_resolve_callable` sin restaurarlo y
  contaminaban cualquier test posterior del mismo proceso; ahora usan `monkeypatch`
  (P3-2)

## 2.0.6 (2026-06-25)

### Fix

- **cli**: error de typer provocaba bloqueo completo de la api

## 2.0.5 (2026-05-19)

### Fix

- **utils.py**: valor de un campo serializado se eliminaba al momento de aplciars elos serializadores cuando la Key A era igual que la Key B
- **UseCase**: solo para hacer un bump version

## 2.0.4 (2026-05-02)

### Refactor

- **UseCase**: delete bound for accept any return type

## 2.0.3 (2026-04-08)

### Fix

- **aplication**: fix final de tipados

## 2.0.2 (2026-04-08)

### Fix

- **aplication**: firma erronea en el use case base

## 2.0.1 (2026-04-08)

### Fix

- **query**: harden query validation and sorting behavior

## 2.0.0 (2026-04-08)

### Breaking

- release major 2.0.0 after versioning reset and tag cleanup

### Feat

- **feat:add-project-templates-for-init-command-and-folder-agnostic**: hexcore

### Fix

- **query**: harden query validation and sorting behavior

## 1.7.0 (2026-04-08)

### Feat

- **repositories**: add repository module normalization and priority handling

## 1.6.8 (2026-03-30)

### Fix

- **repositories**: ignore alias duplicates during repository discovery

### Refactor

- **uow**: better repositories discover

## 1.6.7 (2026-03-30)

### Fix

- **repositories**: harden repository discovery and uow injection

## 1.6.6 (2026-03-28)

### Fix

- **infrastructure.repositories**: finally fix of row mapping objects

## 1.6.5 (2026-03-28)

### Fix

- **infrastructure.repositories**: best robust for row-like sqlalchemy objects in to_entity_from_model_or_document util

## 1.6.4 (2026-03-28)

### Fix

- **repositories**: support Row mapping in to_entity utility and add tests

## 1.6.3 (2026-03-28)

### Fix

- **uow**: avoid duplicate rollback in async session lifecycle

## 1.6.2 (2026-03-28)

### Fix

- **domain.uow,-infrastructure.uow**: corregir manejo de rollback en caso de error y optimizar cierre de sesión

## 1.6.1 (2026-03-28)

### Fix

- **infrastructure.repositories.orms.sqalchemy.session**: fallo al cerrar la conexion mientras se realizaba una transaccion

## 1.6.0 (2026-03-28)

### Feat

- **domain.reposotiries,-infrastructure.repositories.implementations-and-orms-utils**: implement limit/offset pagination in repository methods

## 1.5.1 (2026-03-27)

### Fix

- **infrastructure.uow**: set the inject repositories in the init def

## 1.5.0 (2026-03-27)

### Feat

- **infrastructure.uow**: add the auto repo register in the uow

## 1.4.2 (2026-03-26)

### Fix

- **config.py**: only build new version

## 1.4.1 (2026-03-26)

### Fix

- **README.md**: readme.md

## 1.4.0 (2026-03-26)

### Fix

- **hexcore.domain.uow.IUnitOfWork**: delete iunitofwork function

## v1.3.2 (2025-10-05)

## v1.3.1b (2025-10-05)

## v1.3.1a (2025-10-05)

## 1.3.1 (2025-09-15)

### Fix

- **cli.py**: fix files schemes

## 1.3.0 (2025-09-15)

### Fix

- **pyproject.toml**: add ruff obligatory module

## 1.2.0 (2025-09-15)

### Feat

- **cli.py**: new argument in init_project

## 1.1.0 (2025-09-15)

### Fix

- **stubs**: fix stubs files maker

### Refactor

- rename ORM/ODM repo implements
- delete Permissions Enum and SQLTenant

## 1.0.2 (2025-09-15)

### Fix

- fix returns types

## 1.0.1 (2025-09-14)

### Fix

- add pyi files fixer, fix bug config loader, add new cli fow execute the scripts
