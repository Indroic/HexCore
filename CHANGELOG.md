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

### Behavior change

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
