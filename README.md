# HexCore [![PyPI Downloads](https://static.pepy.tech/personalized-badge/hexcore?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/hexcore)
HexCore es un módulo base reutilizable para proyectos Python que implementan arquitectura hexagonal y event handling.

---

## Skills del Proyecto

Este repositorio cuenta con un conjunto de skills adicionales para extender y personalizar funcionalidades en VS Code y otros entornos compatibles. Puedes encontrarlas en:

- [Repositorio de Skills de HexCore](https://github.com/Indroic/hexcore-skill)

---

## ¿Qué provee HexCore?

- **Clases base y abstracciones** para entidades, repositorios, servicios y unidad de trabajo (UoW), siguiendo los principios de DDD y arquitectura hexagonal.
- **Interfaces y contratos** para caché, eventos y manejo de dependencias, desacoplando la lógica de negocio de la infraestructura.
- **Utilidades para event sourcing y event dispatching** listas para usar en cualquier proyecto.
- **Estructura flexible** para que puedas construir microservicios o aplicaciones monolíticas desacopladas y testeables.

---

## Instalación

```sh
pip install hexcore
```

## Templates de Proyecto (CLI)

HexCore incluye templates base para bootstrap de proyectos:

```sh
hexcore init mi_proyecto --template hexagonal
hexcore init mi_proyecto --template vertical-slice
```

- `hexagonal`: crea `src/domain`, `src/application`, `src/infrastructure`.
- `vertical-slice`: crea `src/features`, `src/shared/domain`, `src/shared/application`, `src/shared/infrastructure`.

En ambos templates se generan:
- `config.py` en raíz con `repository_discovery_paths` de ejemplo.
- estructura de migraciones con Alembic.

---

## Configuración v2 (Folder-Agnostic)

Desde v2, HexCore usa configuración explícita y no aplica fallback implícito para descubrir repositorios.

### 1. Configuración visible en raíz

Define un archivo `config.py` en la raíz del proyecto:

```python
from hexcore.config import ServerConfig

config = ServerConfig(
    repository_discovery_paths={
        "myapp.features.users.infrastructure.repositories",
        "myapp.features.billing.infrastructure.repositories",
    }
)
```

### 2. Prioridad para cargar configuración

`LazyConfig` resuelve módulos en este orden:

1. `HEXCORE_CONFIG_MODULE`
2. `HEXCORE_CONFIG_MODULES` (lista separada por comas)
3. módulos configurados por `LazyConfig.set_config_modules(...)`
4. `config` por defecto (raíz del proyecto)

### 3. Regla de discovery en v2

- Si `repository_discovery_paths` está vacío, no se cargan módulos de repositorios.
- UoW falla con error explícito para evitar comportamiento ambiguo.

---

## Pautas de Colaboración

¡Gracias por tu interés en contribuir a HexCore! Para mantener una colaboración organizada y eficiente, sigue estas pautas:

### 1. Código de Conducta
Mantén siempre una comunicación respetuosa y profesional. Revisa el [Código de Conducta](CODE_OF_CONDUCT.md) antes de interactuar.

### 2. Cómo Contribuir
- **Forkea** el repositorio y crea una rama para tu contribución (`feature/nombre`, `fix/nombre`, etc.).
- Realiza tus cambios en la rama y asegúrate de que el código funcione correctamente.
- Escribe una descripción clara y detallada en tu pull request (PR).
- Relaciona los issues relevantes en tu PR si aplica.

### 3. Estilo y Formato de Código
- Sigue la guía de estilos de Python ([PEP8](https://pep8.org/)).
- Usa comentarios cuando sea necesario para clarificar el propósito del código.
- Idealmente, incluye pruebas unitarias para nuevas funciones y arreglos.

### 4. Revisión de Pull Requests
- Todos los PR serán revisados antes de ser aceptados. Se pueden solicitar cambios o aclaraciones.
- Responde a los comentarios de los revisores para facilitar el proceso.

### 5. Issues
- Describe claramente los problemas que encuentres.
- Proporciona información relevante (logs, versiones, pasos para reproducir, etc.).

### 6. Comunicación
- Usa los issues y las discusiones para preguntas, sugerencias o propuestas.
- Si tienes dudas sobre cómo empezar, puedes abrir un issue para orientación.

### 7. Licencia
Al contribuir, aceptas que tu código será distribuido bajo la licencia del repositorio.

---

## Documentación Básica

### Estructura principal

HexCore se organiza con los siguientes submódulos y carpetas:

- **src/domain/**: Módulos de dominio, entidades, repositorios, servicios, objetos de valor, eventos, enums y excepciones.
  ```
  src/domain/{modulo}/
    ├─ __init__.py
    ├─ entities.py
    ├─ repositories.py
    ├─ services.py
    ├─ value_objects.py
    ├─ events.py
    ├─ enums.py
    └─ exceptions.py
  ```
- **src/application/**: Casos de uso (UseCase) y DTOs para orquestar la lógica de negocio.
- **src/infrastructure/**: Implementaciones técnicas (ORM/ODM, CLI, caché, base de datos, repositorios, unit of work).
- **src/infrastructure/database/models/**: Modelos SQLAlchemy para base de datos relacional.
- **src/infrastructure/database/documents/**: Documentos Beanie para MongoDB.
- **tests/**: Pruebas para módulos de dominio e infraestructura.

---

### Abstracciones de Entidades y Eventos

#### BaseEntity

Clase base para entidades de dominio. Provee atributos comunes y gestión de eventos.

```python
from hexcore.domain.base import BaseEntity

class User(BaseEntity):
    id: UUID
    name: str
```

#### DomainEvent y eventos de entidad

Abstracciones para eventos de dominio y para ciclo de vida de entidades.

```python
from hexcore.domain.events import DomainEvent, EntityCreatedEvent

class UserCreatedEvent(EntityCreatedEvent[User]):
    pass

user = User(...)
event = UserCreatedEvent(entity_id=user.id, payload={"name": user.name})
```

---

### Implementaciones de Repositorios

#### SQLAlchemyCommonImplementationsRepo

Repositorio genérico para modelos SQLAlchemy con métodos CRUD reutilizables.

```python
class SQLAlchemyCommonImplementationsRepo(BaseSQLAlchemyRepository[T], HasBasicArgs[T, M], t.Generic[T, M]):
    # Métodos principales: get_by_id, list_all, save, delete
    ...
```

**Ejemplo:**

```python
class UserRepository(SQLAlchemyCommonImplementationsRepo[UserEntity, UserModel]):
    def __init__(self, uow):
        super().__init__(
            entity_cls=UserEntity,
            model_cls=UserModel,
            not_found_exception=UserNotFoundException,
            fields_resolvers=None,
            fields_serializers=None,
            uow=uow
        )
```

#### BeanieODMCommonImplementationsRepo

Repositorio genérico para documentos Beanie ODM (MongoDB) con métodos CRUD reutilizables.

```python
class BeanieODMCommonImplementationsRepo(IBaseRepository[T], HasBasicArgs[T, D], t.Generic[T, D]):
    # Métodos principales: get_by_id, list_all, save, delete
    ...
```

**Ejemplo:**

```python
class UserRepository(BeanieODMCommonImplementationsRepo[UserEntity, UserDocument]):
    def __init__(self, uow):
        super().__init__(
            entity_cls=UserEntity,
            document_cls=UserDocument,
            not_found_exception=UserNotFoundException,
            fields_resolvers=None,
            fields_serializers=None,
            uow=uow
        )
```

---

### Inicialización y Descubrimiento de Documentos Beanie

Para inicializar y registrar automáticamente todos los documentos Beanie:

```python
from hexcore.infrastructure.repositories.orms.beanie.utils import init_beanie_documents

await init_beanie_documents()
```

---

### Conversión entre modelos/documentos y entidades

Ambos repositorios utilizan `to_entity_from_model_or_document` para convertir modelos ORM/ODM en entidades del dominio, aplicando resolvers para atributos complejos.

---

---

## Arquitectura CQRS en HexCore

HexCore v2 integra de forma nativa soporte para el patrón **CQRS (Command Query Responsibility Segregation)**, permitiendo separar conceptual y técnicamente las operaciones de escritura (Commands) de las de lectura (Queries). 

### ¿Cómo funciona el CQRS en HexCore?

El sistema se basa en 3 buses principales, configurables e independientes:

1. **`AbstractCommandBus`**: Despacha inteniones de mutación (`Command`) a un único `AbstractCommandHandler`. Los commands modifican el estado del sistema. La transacción la gestiona el handler (el patrón que enseñan los ejemplos de use case); si preferís que la gestione el bus, añadí `TransactionMiddleware` explícitamente con su `uow_factory`.
2. **`AbstractQueryBus`**: Despacha intenciones de lectura (`Query`) a un único `AbstractQueryHandler`. Retornan un resultado sin mutar el estado.
3. **`EventBus`**: Distribuye eventos de dominio (`DomainEvent`) a múltiples suscriptores asíncronamente (vía `subscribe`/`publish`).

La configuración de CQRS se activa mediante el `CQRSConfig` en tu `ServerConfig`:

```python
from hexcore.config import ServerConfig
from hexcore.application.cqrs.config import CQRSConfig, BusConfig

config = ServerConfig(
    cqrs=CQRSConfig(
        command_bus=BusConfig(
            # Sin middlewares por defecto. Los que no necesitan configuración se
            # pueden declarar por dotted path:
            middlewares=["hexcore.infrastructure.cqrs.middlewares.LoggingMiddleware"]
        ),
        # Puedes sustituir el backend en memoria por uno distribuido (Ej: Celery, Procrastinate)
        # backend="mi_app.infrastructure.ProcrastinateCommandBus" 
    )
)
```

> **`TransactionMiddleware` no es el default.** Comitea después del handler, así que
> con un handler que ya gestiona su propia transacción comitearías dos veces. Y
> necesita un `uow_factory` construido con *tu* engine, cosa que no se puede expresar
> como dotted path: instancialo a mano y pasá el pipeline al bus.
>
> ```python
> TransactionMiddleware(uow_factory=lambda: SqlAlchemyUnitOfWork(session=session_factory()))
> ```

---

### Guía de Migración: De Casos de Uso Clásicos a CQRS

Si ya tienes una aplicación escrita con la abstracción `UseCase` de HexCore, puedes migrar progresivamente a CQRS sin reescribir todo tu código, utilizando los adaptadores incluidos.

#### Paso 1: Usar el adaptador `UseCaseCommandHandler`

En lugar de instanciar un UseCase directamente en tu endpoint, envuélvelo en un comando:

```python
import hexcore.cqrs as cqrs

# 1. Tienes tu UseCase legado, con sus dependencias
class CreateUserUseCase(UseCase[CreateUserCommand, UserDTO]):
    def __init__(self, uow: IUnitOfWork) -> None:
        self.uow = uow

    async def execute(self, request: CreateUserCommand) -> UserDTO:
        # logica legacy
        ...

# 2. Lo registras en el registry de CQRS utilizando el adaptador
registry = cqrs.HandlerRegistry()
registry.register_command_handler(
    CreateUserCommand,
    cqrs.UseCaseCommandHandler(CreateUserUseCase(uow)),
)

# O con un factory, si quieres resolver las dependencias en el momento del dispatch:
registry.register_command_handler(
    CreateUserCommand,
    cqrs.HandlerRegistry.factory(
        lambda: cqrs.UseCaseCommandHandler(CreateUserUseCase(build_uow()))
    ),
)
```

> El método es `register_command_handler` (y `register_query_handler`), no
> `register_command`.

#### Paso 2: Consumirlo desde el endpoint usando el CommandBus

```python
@router.post("/users")
async def create_user(
    cmd: CreateUserCommand, 
    # factory inyectado por dependencias
    bus: AbstractCommandBus = Depends(get_command_bus)
):
    # El bus despacha el comando al UseCase legacy de forma transparente
    result = await bus.dispatch(cmd)
    return result
```

#### Paso 3 (Final): Refactor a Handler Puro

Cuando estés listo, convierte tu UseCase directamente en un `AbstractCommandHandler`:

```python
from hexcore.domain.cqrs import AbstractCommandHandler

class CreateUserHandler(AbstractCommandHandler[CreateUserCommand, UserDTO]):
    def __init__(self, uow: IUnitOfWork):
        self.uow = uow

    async def handle(self, command: CreateUserCommand) -> UserDTO:
        # Lógica refactorizada
        return dto
```

---

### Guía: Almacenamiento Híbrido para Queries (Mongo, Redis, SQL)

La mayor ventaja de CQRS es optimizar las lecturas. HexCore permite que tus **Commands** escriban en una base de datos relacional (SQLAlchemy) fuertemente normalizada, mientras que los **Queries** leen de vistas desnormalizadas súper rápidas en MongoDB o Redis.

#### 1. Sincronización a través del EventBus (La Proyección)

Cuando un Command modifica SQL, dispara un Evento de Dominio. Un handler de eventos intercepta este evento y actualiza el "Read Model" en MongoDB o Redis.

```python
from hexcore.domain.events import EventBus, DomainEvent

class UserCreatedEvent(DomainEvent):
    user_id: str
    full_name: str
    email: str

async def project_user_to_mongodb(event: UserCreatedEvent):
    """Proyecta el evento en la BD de lectura (MongoDB)"""
    doc = UserReadDocument(
        id=event.user_id, 
        name=event.full_name, 
        email=event.email
    )
    await doc.insert() # usando Beanie (Mongo)

# Registrar la proyección
event_bus.subscribe(UserCreatedEvent, project_user_to_mongodb)
```

#### 2. Query Handler leyendo del Read Model

Tu QueryHandler nunca toca SQL, simplemente ataca directamente a Mongo o Redis para máxima velocidad.

```python
from hexcore.domain.cqrs import AbstractQueryHandler, Query

class GetUserQuery(Query[UserReadDTO]):
    user_id: str

class GetUserQueryHandler(AbstractQueryHandler[GetUserQuery, UserReadDTO]):
    async def handle(self, query: GetUserQuery) -> UserReadDTO:
        # Consulta ultra rápida a la colección de lectura en MongoDB
        doc = await UserReadDocument.get(query.user_id)
        
        # O desde Redis:
        # data = await redis_client.get(f"user:{query.user_id}")
        
        if not doc:
            raise UserNotFoundException()
        return UserReadDTO(**doc.dict())
```

Con este esquema, alcanzas una alta escalabilidad: tus endpoints GET son despachados por el `QueryBus` respondiendo en milisegundos desde Mongo/Redis, y tus operaciones POST/PUT/DELETE van por el `CommandBus` transaccionando con ACID en SQL.

#### 3. Definición de Modelos de Lectura (Proyecciones)

Una pregunta frecuente es: **¿HexCore genera automáticamente estos modelos de lectura?** 
La respuesta es **No**. El patrón CQRS sugiere que tus modelos de lectura estén diseñados *específicamente* para lo que tus interfaces visuales (UI) o APIs van a consultar. Por lo tanto, debes definir estos modelos manualmente.

**Si usas MongoDB (Beanie) para lecturas:**
Debes crear un documento manual optimizado. Por ejemplo, en lugar de tener joins, puedes embeber datos:
```python
from beanie import Document

# Modelo desnormalizado optimizado para la lectura
class UserReadDocument(Document):
    id: str  # ID referenciado de la tabla SQL
    name: str
    email: str
    total_purchases_cache: int = 0  # Dato pre-calculado por eventos

    class Settings:
        name = "users_read_projections"
```

**Si usas PostgreSQL/MySQL (SQLAlchemy) para lecturas:**
Si prefieres mantenerte 100% en SQL pero aislando lecturas, puedes crear tablas específicas para proyecciones (Materialized Views o tablas planas):
```python
from sqlalchemy.orm import declarative_base
from sqlalchemy import Column, String, Integer

Base = declarative_base()

class UserReadProjection(Base):
    __tablename__ = 'users_read_projection'
    
    # Modelo totalmente plano sin relaciones ForeignKey complejas
    id = Column(String, primary_key=True)
    full_name = Column(String)
    email = Column(String)
    total_purchases_cache = Column(Integer, default=0)
```
En ambos casos, es tu **EventBus** (o un consumidor como Procrastinate) el encargado de instanciar estos modelos manuales y persistirlos cada vez que se detecte un cambio en los modelos de escritura.

---

### Integración con Task Queues (Smart Routing)

HexCore v2 hace que la delegación de tareas a **Celery**, **Procrastinate** o **ARQ** sea increíblemente sencilla y mágica a través del patrón de **Smart Routing**.

Ya no necesitas instanciar buses separados para código síncrono y asíncrono. HexCore enruta automáticamente tus comandos y eventos hacia las colas de background usando simples decoradores.

#### 1. Decoradores de Background

HexCore ofrece 3 decoradores esenciales en `hexcore.domain.cqrs.decorators` para cubrir todos los casos de uso:

1. **`@background_command(queue="...")`**: Aplícalo sobre una clase `Command`. Todo el comando y su handler se ejecutarán asíncronamente en el Worker. Ideal para operaciones pesadas iniciadas por el usuario (ej. Generar un reporte PDF masivo).
2. **`@background_handler(queue="...")`**: Aplícalo sobre una función que maneje un evento (`DomainEvent`). Permite que un solo evento dispare algunas acciones síncronas rápidas y otras asíncronas lentas (ej. Enviar emails).
3. **`@background_task(queue="...")`**: Aplícalo sobre funciones o utilidades genéricas que no pertenecen al modelo estricto de CQRS (ej. Limpiar base de datos, tareas tipo CRON).

**Ejemplos de uso:**

```python
from hexcore.domain.cqrs.decorators import background_command, background_handler, background_task
from hexcore.domain.cqrs.commands import Command

# 1. Comando de ejecución asíncrona obligatoria
@background_command(queue="high_priority")
class SendEmailCommand(Command):
    user_id: str
    template: str

# 2. Handler de evento asíncrono
@background_handler(queue="analytics")
async def send_analytics_on_user_created(event: UserCreatedEvent):
    # Lógica costosa...
    pass

# 3. Tarea genérica (Non-CQRS)
@background_task(queue="maintenance")
async def clean_old_records_task(days_retention: int):
    # Limpieza de base de datos...
    pass
```

#### 2. Usar un Enqueuer (Adaptador)

No hace falta escribirlo: HexCore trae `ProcrastinateEnqueuer` y `CeleryEnqueuer` listos, y
registran las tareas del consumidor con los nombres `hexcore.process_command`,
`hexcore.process_handler` y `hexcore.process_task`.

```python
from hexcore.infrastructure.task_queues.procrastinate_adapter import ProcrastinateEnqueuer

enqueuer = ProcrastinateEnqueuer(procrastinate_app)
```

Si necesitas otro broker, implementa `ITaskEnqueuer` (4 métodos). Dos advertencias:

- **`enqueue_event` no es un `pass`.** Una cola de tareas no puede hacer fan-out a "todos
  los suscriptores", así que los adaptadores oficiales lanzan `NotImplementedError` en vez
  de perder el evento en silencio. Para ejecutar un suscriptor concreto en background usa
  `@background_handler` (el EventBus llamará a `enqueue_handler`); para fan-out real usa
  `RedisEventBus` o `PostgresEventBus`.
- Si envuelves corutinas en un worker síncrono (Celery), no uses `asyncio.run()` por tarea:
  cierra el event loop y deja el pool del `AsyncEngine` atado a un loop muerto. HexCore usa
  un loop persistente por proceso, expuesto como `run_in_worker_loop(coro)`.

#### 3. Configurar tus Buses con un Adaptador Oficial

HexCore provee adaptadores *plug & play* para **Celery** y **Procrastinate**. Simplemente importa el enqueuer, pásale tu app y configúralo en los buses de memoria.

Si además deseas persistencia o distribución de Eventos entre múltiples workers/servidores (Pub/Sub), puedes cambiar el `InMemoryEventBus` por `RedisEventBus`, `PostgresEventBus` o `RabbitMQEventBus`:

```python
from hexcore.application.cqrs.in_memory_buses import InMemoryCommandBus
from hexcore.infrastructure.task_queues.celery_adapter import CeleryEnqueuer
from hexcore.infrastructure.cqrs.redis_bus import RedisEventBus
from celery import Celery
import redis.asyncio as redis

# 1. Adaptador de Task Queue (Para Comandos asíncronos y Event Handlers asíncronos)
app = Celery("my_app", broker="redis://localhost:6379/0")
enqueuer = CeleryEnqueuer(app)
serializer = PydanticSerializer()

command_bus = InMemoryCommandBus(registry=registry, enqueuer=enqueuer, serializer=serializer)

# 2. Event Bus (Para enviar los Eventos por la red)
redis_client = redis.from_url("redis://localhost:6379/0")
event_bus = RedisEventBus(
    redis_client=redis_client,
    serializer=serializer,
    stream_name="hexcore:events",
    group_name="api_workers",
    enqueuer=enqueuer  # <-- Importante para inyectarle la habilidad de Smart Routing
)
```

> **Tip:** También dispones de `PostgresEventBus(pool, serializer, channel_name)` que usa `LISTEN/NOTIFY` nativo si quieres 0 dependencias externas aparte de tu BD de siempre.

#### 4. Ejecutar tareas genéricas

Para encolar la tarea genérica (`@background_task`), la llamas indirectamente pasándola por el enqueuer:

```python
# Así se encola una tarea genérica sin CQRS:
await enqueuer.enqueue_task(
    task_name=clean_old_records_task.__cqrs_task_name__, 
    payload={"days_retention": 30},
    queue=clean_old_records_task.__cqrs_queue__
)
```

#### 5. Levantar el Worker (Consumidor Universal)

En el entrypoint de tu worker, `register_hexcore_*_tasks` autoconfigura las rutas
`hexcore.process_command`, `hexcore.process_handler` y `hexcore.process_task`:

```python
import hexcore.cqrs as cqrs
from hexcore.infrastructure.task_queues.celery_adapter import register_hexcore_celery_tasks

# Le pasas el MISMO bus que usa el proceso web: el consumer marca el mensaje como
# "viene del worker", así que el bus lo ejecuta en vez de reencolarlo.
consumer = cqrs.CQRSConsumer(command_bus, event_bus)

register_hexcore_celery_tasks(app, consumer)  # idempotente: llamarla dos veces no revienta
```

Un worker que sólo procesa comandos puede omitir el event bus: `cqrs.CQRSConsumer(command_bus)`.

Y el entrypoint completo del worker (con scheduler, muerte mutua y SIGTERM) es una llamada:

```python
await cqrs.run_procrastinate_worker(
    procrastinate_app,
    queues=["default", "reactive"],
    scheduler=cqrs.DynamicScheduler(repo, enqueuer, lock_provider=lock),
    on_startup=[lambda: cqrs.seed_cron_jobs(CRON_JOBS)],
)
```

#### 6. Ejecutar un comando "aquí y ahora"

No hay una API separada para esto, y es a propósito: el contrato es que **el bus decide
por contexto**.

- Fuera de un worker, un `@background_command` se encola.
- Dentro de un worker (es decir, cuando el mensaje viene del `CQRSConsumer`) el **mismo**
  bus lo ejecuta localmente. Por eso puedes —y debes— compartir un único bus entre la app
  web y el worker.
- Si un handler despacha a propósito otro `@background_command`, ese sí se encola: el
  contexto de worker se consume en el primer dispatch.

Si necesitas comprobarlo desde tu código, `cqrs.is_worker_execution()` responde si el
mensaje en curso viene de una cola.

---

## Tareas Periódicas Dinámicas (Cronjobs en Caliente)

HexCore incluye un **`DynamicScheduler`** que te permite programar tareas (`@background_task`) para que se ejecuten periódicamente. La ventaja clave es que lee la configuración desde un repositorio (como tu Base de Datos), permitiendo activar, desactivar o cambiar los horarios **sin necesidad de reiniciar tus servidores**.

### 1. Usa el repositorio SQL de serie (o implementa el tuyo)

Si tu cron vive en SQL —el caso normal— no escribas nada: HexCore trae la tabla, el
repositorio y el seed (extra `[sql]`).

```python
import hexcore.cqrs as cqrs

CRON_JOBS = [
    cqrs.cron_job(clean_old_records_task, "*/5 * * * *", payload={"days_retention": 30}),
    cqrs.cron_job(cerrar_caja, "0 3 * * *"),
]

await cqrs.create_cron_tables()        # o una migración de Alembic
await cqrs.seed_cron_jobs(CRON_JOBS)   # idempotente, y NO pisa lo editado en BD

repo = cqrs.SqlAlchemyCronJobRepository()
```

`cron_job()` deriva el `task_name` de `__cqrs_task_name__`: escribirlo a mano es cómo se
acaba con un cron que encola una tarea ya renombrada, y el fallo aparece en el worker.

Si tu configuración vive en otro sitio (Mongo, Redis, un YAML), implementa
`ICronJobRepository`:

```python
from datetime import datetime

import hexcore.cqrs as cqrs


class MiCronRepository(cqrs.ICronJobRepository):
    async def get_active_jobs(self) -> list[cqrs.CronJobDefinition]:
        return [
            cqrs.CronJobDefinition(
                job_id="clean-db",
                task_name="mi_app.tasks.clean_old_records_task",  # un @background_task
                cron_expression="*/5 * * * *",
                payload={"days_retention": 30},
                queue="maintenance",
            )
        ]

    async def update_last_run(self, job_id: str, run_time: datetime) -> None:
        # Importa implementarlo: es lo que deduplica el encolado entre ticks.
        ...
```

### 2. Levanta el Scheduler
En un proceso en background de tu API o en un microservicio separado, arranca el Scheduler inyectándole tu Enqueuer favorito (Celery, Procrastinate):

```python
import hexcore.cqrs as cqrs

scheduler = cqrs.DynamicScheduler(
    repository=repo,
    enqueuer=enqueuer,
    tick_interval_seconds=60,
)

# Lo normal es dejar que el runner lo supervise junto al worker: si uno de los dos
# muere, se cancela el otro y el proceso sale para que el orquestador lo reinicie.
await cqrs.run_procrastinate_worker(procrastinate_app, scheduler=scheduler)
```

El Scheduler evalúa las expresiones con `croniter` y delega la carga pesada al enqueuer. Tu
Worker no necesita saber de horarios, sólo ejecuta las tareas cuando le llegan.

**Cómo decide si toca ejecutar.** No compara contra el minuto actual, sino que busca si hubo
alguna ocurrencia entre la última ejecución (`last_run_at`) y ahora. Dos consecuencias que
importan:

- Un minuto saltado por drift del tick **no** pierde la ejecución.
- `update_last_run` deduplica de verdad, así que un `tick_interval_seconds < 60` no duplica
  el encolado dentro del mismo proceso. Entre réplicas sí hace falta lock, y el scheduler
  emite un `RuntimeWarning` si detecta tick sub-minuto sin `lock_provider`.

`catch_up_window_seconds` (1 hora por defecto) acota el catch-up: un scheduler que estuvo
caído una semana no dispara ocurrencias antiguas.

### 3. Distributed Locks (Evitar ejecuciones dobles)

Si corres tu aplicación en múltiples contenedores o réplicas (ej. Kubernetes), podrías tener múltiples instancias del `DynamicScheduler` ejecutándose al mismo tiempo. Para evitar que el mismo cronjob se encole dos veces en el mismo minuto, HexCore soporta **Locks Distribuidos**.

Puedes inyectar un proveedor de locks (`ILockProvider`) usando Redis o PostgreSQL (si lo usas como tu DB). Al inyectarlo, el Scheduler bloqueará atómicamente la tarea a través de toda tu red.

#### Usando Redis
```python
from hexcore.infrastructure.cqrs.redis_lock import RedisLockProvider
import redis.asyncio as redis

redis_client = redis.from_url("redis://localhost:6379/0")
lock_provider = RedisLockProvider(redis_client)

scheduler = DynamicScheduler(
    repository=repo, 
    enqueuer=enqueuer, 
    lock_provider=lock_provider
)
```

#### Usando PostgreSQL (asyncpg)
Si usas Procrastinate o bases de datos SQL y no quieres levantar Redis:

```python
from hexcore.infrastructure.cqrs.postgres_lock import PostgresLockProvider

lock_provider = PostgresLockProvider(my_asyncpg_pool)
await lock_provider.setup() # Crea la tabla y el índice, y purga lo expirado

scheduler = DynamicScheduler(
    repository=repo, 
    enqueuer=enqueuer, 
    lock_provider=lock_provider
)
```

> El provider purga las filas expiradas solo (en `setup()` y cada 100 adquisiciones), así
> que la tabla de locks no crece sin límite. `purge_expired()` es pública si prefieres
> purgar desde un job propio, y `purge_every=0` desactiva la purga automática.

#### Qué pasa si el lock no responde

Si Redis (o Postgres) se cae, `acquire_lock` no puede decidir, y las dos respuestas
posibles son malas de formas distintas. La decisión es tuya y explícita:

```python
RedisLockProvider(redis_client, on_error="skip")   # default: no correr. El cron se detiene.
RedisLockProvider(redis_client, on_error="raise")  # propagar, para que el supervisor lo vea.
```

En los logs, "no pude decidir" es `critical` y "el lock estaba tomado por otra réplica"
—el caso normal— es `debug`.

---

## Referencias

- [CONTRIBUTING.md](./CONTRIBUTING.md): Pautas de colaboración.
- [CHANGELOG.md](./CHANGELOG.md): Historial de cambios.
- [DOCS.md](./DOCS.md): Documentación básica de clases, funciones y ejemplos.
