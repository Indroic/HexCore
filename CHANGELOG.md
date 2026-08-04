## 6.2.1 (2026-08-04)

### Fix

- declara 6.x como serie activa en la política de soporte

## 6.2.0 (2026-07-30)

### Feat

- exporta ResponseFactory desde la fachada hexcore.fastapi
- las rutas de health se adoptan por partes

### Fix

- declara 6.x como serie activa en la política de soporte
- declara 6.x como serie activa en la política de soporte
- install_request_id_logging avisa en vez de ser un no-op silencioso

## 6.1.0 (2026-07-30)

### Feat

- CronJobDefinition lleva description, y el seed la persiste
- build_root_router acepta una secuencia de hijos, no sólo un mapa

### Fix

- declara 6.x como serie activa en la política de soporte
- declara 6.x como serie activa en la política de soporte

## 6.0.2 (2026-07-30)

### Fix

- declara 6.x como serie activa en la política de soporte
- mueve REMOVED_IN a 7.0 y lo blinda con un test

## 6.0.1 (2026-07-30)

### Fix

- declara 6.x como serie activa en la política de soporte

## 6.0.0 (2026-07-30)

### Feat

- emite DeprecationWarning en toda la superficie de API anterior a 5.0

## 5.0.0 (2026-07-29)

### Feat

- fachadas hexcore.cqrs, hexcore.sql y hexcore.fastapi

### Fix

- **task_queues**: un event loop persistente por proceso worker de Celery

### Refactor

- borra MiddlewareConfig y las constantes muertas de la CLI

## 4.0.0 (2026-07-29)

### Feat

- **testing**: publica hexcore.testing con dobles, helpers y fixtures
- **api,dtos**: paginación por cursor y mejoras a build_query_endpoint
- **api**: create_app() con cero configuración en el camino feliz
- **api**: build_lifespan composable con teardown garantizado en orden inverso
- **api**: health checks que sondean las dependencias de verdad
- **api**: utilidades de SSE, heartbeat de WebSocket y límite de conexiones
- **api**: rate limiting como dependencia sobre el puerto ICache
- **api**: providers FastAPI del CQRS con una sola fuente de verdad
- **api**: register_exception_handlers para mapear excepciones de dominio a HTTP
- **workers**: run_cqrs_worker con muerte mutua y drenaje ordenado
- **cqrs**: tabla, repositorio y seed de cron_jobs de serie
- **api**: middlewares de request-id/timing y composición declarativa de routers

### Fix

- **cqrs**: no descartes un enqueuer presente pero falsy

## 3.0.0 (2026-07-29)

### Feat

- **uow**: session_scope y uow_scope para código fuera de FastAPI
- **sql**: capa de sesión configurable, con expire_on_commit=False y DSN normalizado
- **cqrs**: event_bus y serializer opcionales en CQRSConsumer
- **task_queues**: register_hexcore_*_tasks idempotente
- **cqrs**: HandlerRegistry thread-safe de verdad y marcador explícito de factory
- **cqrs**: on_error configurable en los lock providers y logs distinguibles
- **cqrs**: catch-up real en DynamicScheduler y deduplicación por ocurrencia
- **cqrs**: CQRSFactory propaga enqueuer y serializer a los buses in-memory

### Fix

- **task_queues**: enqueue_event lanza NotImplementedError en vez de perder el evento
- **cqrs**: TransactionMiddleware fuera del default y con uow_factory obligatorio
- **cqrs**: PostgresLockProvider purga las filas de lock expiradas
- **cqrs**: el worker ejecuta los background commands en vez de reencolarlos
- **cqrs**: resuelve los __qualname__ anidados en serializer y consumer

## 2.5.0 (2026-07-28)

### Feat

- refactor repositories and make heavy dependencies optional

### Fix

- install anyio in pytest workflow to support async tests
- add anyio to dev dependencies to fix pytest async execution in CI
- make dummy classes generic and skip tests when optional dependencies are missing

## 2.4.0 (2026-07-27)

### Feat

- Add Redis and PostgreSQL Event Buses for real-time Pub/Sub
- Task queues adapters (Celery, Procrastinate) for smart routing

## 2.3.0 (2026-07-27)

### Feat

- smart routing decorators and utilities for background tasks

## 2.2.0 (2026-07-27)

### Fix

- **test**: mock aio_pika to prevent ModuleNotFoundError in CI

### Refactor

- make sqlalchemy engine initialization fully lazy for workers

## 2.1.0 (2026-07-27)

### Feat

- add core CQRS abstractions, pipelines, and factory

### Refactor

- migrate IEventDispatcher to EventBus and ensure backward compatibility

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
