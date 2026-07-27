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

1. **`AbstractCommandBus`**: Despacha inteniones de mutación (`Command`) a un único `AbstractCommandHandler`. Los commands modifican el estado del sistema y se ejecutan (por defecto) dentro de una transacción de base de datos (Unit of Work).
2. **`AbstractQueryBus`**: Despacha intenciones de lectura (`Query`) a un único `AbstractQueryHandler`. Retornan un resultado sin mutar el estado.
3. **`EventBus`**: Distribuye eventos de dominio (`DomainEvent`) a múltiples suscriptores asíncronamente (vía `subscribe`/`publish`).

La configuración de CQRS se activa mediante el `CQRSConfig` en tu `ServerConfig`:

```python
from hexcore.config import ServerConfig
from hexcore.application.cqrs.config import CQRSConfig, BusConfig

config = ServerConfig(
    cqrs=CQRSConfig(
        command_bus=BusConfig(
            # Por defecto incluye TransactionMiddleware
            middlewares=["hexcore.infrastructure.cqrs.middlewares.TransactionMiddleware"]
        ),
        # Puedes sustituir el backend en memoria por uno distribuido (Ej: Celery, Procrastinate)
        # backend="mi_app.infrastructure.ProcrastinateCommandBus" 
    )
)
```

---

### Guía de Migración: De Casos de Uso Clásicos a CQRS

Si ya tienes una aplicación escrita con la abstracción `UseCase` de HexCore, puedes migrar progresivamente a CQRS sin reescribir todo tu código, utilizando los adaptadores incluidos.

#### Paso 1: Usar el adaptador `UseCaseCommandHandler`

En lugar de instanciar un UseCase directamente en tu endpoint, envuélvelo en un comando:

```python
from hexcore.application.cqrs.adapters import UseCaseCommandHandler
from hexcore.application.cqrs.registry import HandlerRegistry

# 1. Tienes tu UseCase legado
class CreateUserUseCase(UseCase[CreateUserCommand, UserDTO]):
    async def execute(self, request: CreateUserCommand) -> UserDTO:
        # logica legacy
        pass

# 2. Lo registras en el registry de CQRS utilizando el adaptador
registry = HandlerRegistry()
registry.register_command(
    CreateUserCommand, 
    UseCaseCommandHandler(CreateUserUseCase())
)
```

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

#### 2. Crear el Enqueuer (Adaptador)

Implementa la interfaz genérica `ITaskEnqueuer` usando la SDK de tu Task Queue favorito:

```python
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer

class ProcrastinateEnqueuer(ITaskEnqueuer):
    async def enqueue_command(self, command_name: str, payload: dict, queue: str) -> None:
        await process_cqrs_command.defer_async(payload=payload)
        
    async def enqueue_handler(self, handler_name: str, payload: dict, queue: str) -> None:
        await process_cqrs_handler.defer_async(handler_name=handler_name, payload=payload)
    
    async def enqueue_event(self, event_name: str, payload: dict, queue: str) -> None:
        pass # Usualmente no se usa directamente si utilizas @background_handler
        
    async def enqueue_task(self, task_name: str, payload: dict, queue: str) -> None:
        await process_generic_task.defer_async(task_name=task_name, payload=payload)
```

#### 2. Configurar tus Buses con un Adaptador Oficial

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

En el entrypoint de tu worker, usa la función utilitaria `register_hexcore_celery_tasks` para autoconfigurar las rutas en una sola línea:

```python
from hexcore.infrastructure.workers.consumer import CQRSConsumer
from hexcore.infrastructure.task_queues.celery_adapter import register_hexcore_celery_tasks

consumer = CQRSConsumer(
    command_bus=command_bus, # Tu CommandBus configurado
    event_bus=event_bus, 
    serializer=serializer
)

# ¡Magia! Registra las tareas 'hexcore.process_command', 'hexcore.process_handler', etc.
register_hexcore_celery_tasks(app, consumer)
```

---

## Referencias

- [CONTRIBUTING.md](./CONTRIBUTING.md): Pautas de colaboración.
- [CHANGELOG.md](./CHANGELOG.md): Historial de cambios.
- [DOCS.md](./DOCS.md): Documentación básica de clases, funciones y ejemplos.
