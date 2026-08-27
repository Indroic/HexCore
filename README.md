# HexCore [![PyPI Downloads](https://static.pepy.tech/personalized-badge/hexcore?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/hexcore)

Núcleo reutilizable para aplicaciones Python con **arquitectura hexagonal**, **DDD**, **CQRS**
y **tareas en background**. HexCore trae las abstracciones (entidades, repositorios, UoW,
buses) *y* la infraestructura que normalmente reescribe cada proyecto: la capa de sesión SQL,
las factories de FastAPI, el runner del worker, el cron dinámico y las utilidades de test.

El objetivo de diseño es que el camino feliz sea **cero configuración**: `create_app()` sin
argumentos da una app usable, `init_engine()` sin argumentos da un engine de producción
correcto.

```python
# main.py — el arranque completo de una app HexCore
from hexcore.fastapi import build_lifespan, create_app, SqlEngineStep

app = create_app(
    lifespan=build_lifespan(SqlEngineStep()),
    routers=[usuarios_router, tickets_router],
)
```

```python
# worker.py — el worker completo, con cron, muerte mutua y SIGTERM
import hexcore.cqrs as cqrs

await cqrs.run_procrastinate_worker(
    procrastinate_app,
    queues=["default", "reactive"],
    scheduler=cqrs.DynamicScheduler(repo, enqueuer, lock_provider=lock),
    on_startup=[lambda: cqrs.seed_cron_jobs(CRON_JOBS)],
)
```

---

## Índice

- [Instalación](#instalación)
- [Los tres imports](#los-tres-imports)
- [Qué trae, de un vistazo](#qué-trae-de-un-vistazo)
- [Capa SQL: engine, sesiones y scopes](#capa-sql-engine-sesiones-y-scopes)
- [Utilidades FastAPI](#utilidades-fastapi)
- [Arquitectura CQRS](#arquitectura-cqrs)
- [Task Queues (Smart Routing)](#task-queues-smart-routing)
- [Tareas periódicas dinámicas (cron en caliente)](#tareas-periódicas-dinámicas-cron-en-caliente)
- [Utilidades de test](#utilidades-de-test)
- [Repositorios y entidades](#repositorios-y-entidades)
- [Configuración](#configuración)
- [Darwin: identidad](#darwin-identidad)
- [Templates de proyecto (CLI)](#templates-de-proyecto-cli)
- [Versiones y soporte](#versiones-y-soporte) ← **la API anterior a 5.0 se eliminó en 7.0**
- [Guía de migración a 5.x](#guía-de-migración-a-5x)
- [Contribuir](#contribuir)

---

## Instalación

```sh
pip install hexcore
```

HexCore no arrastra dependencias pesadas: todo lo que no es el núcleo va en **extras**, y los
módulos que las necesitan sólo las importan cuando los usás.

| Extra | Trae | Habilita |
| :-- | :-- | :-- |
| `[api]` | FastAPI, Uvicorn | `hexcore.fastapi`: `create_app`, lifespan, middlewares, health, rate limit, streaming |
| `[sql]` | SQLAlchemy, asyncpg, Alembic | `hexcore.sql`, repositorios SQL, cron en SQL, `PostgresLockProvider` |
| `[mongo]` | Beanie, PyMongo | Repositorios y UoW de MongoDB |
| `[redis]` | redis | `RedisEventBus`, `RedisLockProvider`, `RedisCache` |
| `[procrastinate]` | Procrastinate | `ProcrastinateEnqueuer`, `run_procrastinate_worker` |
| `[rabbitmq]` | aio-pika | `RabbitMQEventBus` y su worker |
| `[celery]` | Celery | `CeleryEnqueuer`, `run_in_worker_loop` |
| `[darwin]` | FastAPI, joserfc, argon2-cffi | Darwin: dominio, servicios, tokens, transportes, plugins. **Sin almacenamiento.** |
| `[darwin-sqlalchemy]` | `hexcore[darwin]` + SQLAlchemy, Alembic, asyncpg, aiosqlite | Almacenamiento de identidad en SQL |
| `[darwin-beanie]` | `hexcore[darwin]` + Beanie | Almacenamiento de identidad en MongoDB |
| `[darwin-magic-link]` | `hexcore[darwin]` | Login por link de un solo uso |
| `[darwin-two-factor]` | `hexcore[darwin]` | TOTP (RFC 6238) con códigos de respaldo |
| `[darwin-oauth]` | `hexcore[darwin]` + httpx | Authorization Code + PKCE |
| `[darwin-impersonate]` | `hexcore[darwin]` | «Entrar como» otro usuario, auditado |
| `[darwin-passkey]` | `hexcore[darwin]` + webauthn | WebAuthn |
| `[darwin-organization]` | `hexcore[darwin]` | Organizaciones, miembros e invitaciones |
| `[all]` | todo lo anterior | — |

```sh
pip install "hexcore[api,sql,procrastinate]"
pip install "hexcore[all]"
```

> `import hexcore.cqrs` funciona sin ningún extra: la resolución de nombres es perezosa, así
> que `hexcore.cqrs.SqlAlchemyCronJobRepository` sólo exige `[sql]` en el momento en que lo
> pedís.

---

## Los tres imports

Hay un módulo fachada por tarea. Reexportan lo público **sin mover nada de sitio**: las rutas
largas siguen funcionando y devuelven el mismo objeto.

```python
import hexcore.fastapi as hx    # create_app, build_lifespan, providers, middlewares, health
import hexcore.cqrs as cqrs     # Command, Query, handlers, decoradores, buses, worker, cron
import hexcore.sql as sql       # init_engine, session_scope, uow_scope, Base, DTOs de query
```

Las fachadas exponen **sólo los nombres canónicos** (`AbstractCommandBus`,
`AbstractSerializer`, …). Los alias históricos `I*` **se eliminaron en 7.0** — ver
[Versiones y soporte](#versiones-y-soporte) para la tabla de reemplazos.

---

## Qué trae, de un vistazo

| Necesitás | API | Extra |
| :-- | :-- | :-- |
| Una app FastAPI cableada | `hx.create_app()`, `hx.AppFeatures` | `api` |
| Orquestar el arranque y el apagado | `hx.build_lifespan()` + steps | `api` |
| Engine y sesiones SQL | `sql.init_engine()`, `sql.dispose_engine()`, `sql.PoolSettings` | `sql` |
| Sesión o UoW fuera de un request | `sql.session_scope()`, `sql.uow_scope()` | `sql` |
| Request-id correlacionado en los logs | `hx.RequestIDMiddleware`, `hx.install_request_id_logging()` | `api` |
| Excepciones de dominio → HTTP | `hx.register_exception_handlers()` | `api` |
| Health checks que sondean de verdad | `hx.register_health_routes()`, `hx.check_health()`, `hx.HealthRoutes` | `api` |
| Rate limiting | `hx.rate_limit()` | `api` |
| SSE / WebSocket / límite de conexiones | `hx.sse_stream()`, `hx.ws_heartbeat()`, `hx.connection_slot()` | `api` |
| Composición de routers | `hx.build_root_router()`, `hx.mount_routers()` | `api` |
| Endpoints de listado y búsqueda | `hx.register_query_endpoint()` | `api` |
| Paginación por cursor | `sql.CursorPageDTO`, `sql.CursorRequestDTO` | `sql` |
| Commands, Queries y eventos | `cqrs.Command`, `cqrs.Query`, `cqrs.HandlerRegistry` | — |
| Ejecutar en background | `cqrs.background_command`, `cqrs.background_handler`, `cqrs.background_task` | — |
| Entrypoint del worker | `cqrs.run_cqrs_worker()`, `cqrs.run_procrastinate_worker()` | — |
| Cron editable en caliente | `cqrs.DynamicScheduler`, `cqrs.SqlAlchemyCronJobRepository` | `sql` |
| Locks distribuidos | `cqrs.RedisLockProvider`, `cqrs.PostgresLockProvider` | `redis` / `sql` |
| Identidad y autenticación | `darwin.configure_identity()`, `darwin.build_identity_router()` | `darwin` + `darwin-sqlalchemy` o `darwin-beanie` |
| Login sin contraseña (magic link) | `MagicLinkPlugin` | `darwin-magic-link` |
| Segundo factor (TOTP) | `TwoFactorPlugin` | `darwin-two-factor` |
| OAuth (Google, GitHub, …) | `OAuthPlugin` | `darwin-oauth` |
| Impersonación auditada | `ImpersonatePlugin` | `darwin-impersonate` |
| Passkeys (WebAuthn) | `PasskeyPlugin` | `darwin-passkey` |
| Organizaciones y miembros | `OrganizationPlugin` | `darwin-organization` |
| Testear todo lo anterior | `hexcore.testing` | — |

---

## Capa SQL: engine, sesiones y scopes

```python
import hexcore.sql as sql

sql.init_engine()          # en el arranque; sin argumentos usa config.async_sql_database_url
await sql.dispose_engine() # en el apagado
```

`init_engine()` sin argumentos ya produce un engine correcto para producción. Lo que **no** es
configurable porque es la única respuesta correcta:

- **`expire_on_commit=False`.** Con el default de SQLAlchemy (`True`) los atributos de las
  entidades expiran al comitear, y el siguiente acceso dispara un lazy-load sobre una sesión
  cerrada → `MissingGreenlet` / `DetachedInstanceError`.
- **Normalización del DSN.** Un `DATABASE_URL` de PaaS viene como `postgresql://…` y
  `create_async_engine` no lo acepta. Se traduce a `postgresql+asyncpg://` solo. Si tu DSN ya
  declara driver, se respeta.

Lo que sí se configura va en un objeto de settings, no en una lista de keywords:

```python
sql.init_engine(
    url="postgresql://user:pass@host/db",           # opcional; se normaliza
    pool=sql.PoolSettings(size=20, max_overflow=10, recycle=1800),
    echo=True,                                      # cualquier kwarg de create_async_engine
)
```

`pool.pre_ping` va en `True` por defecto a propósito: un pool sin pre-ping contra Postgres
detrás de un balanceador entrega conexiones muertas al primer failover.

### Fuera de un request

`get_session`/`get_sql_uow` son dependencias de FastAPI. Para workers, tasks, cron, scripts y
seeds hay scopes:

```python
async with sql.session_scope() as session:      # sesión pelada
    rows = (await session.execute(select(CronJobModel))).scalars().all()

async with sql.uow_scope() as uow:              # UoW SIN abrir
    await CerrarTicketUseCase(uow).execute(request)

async with sql.open_uow_scope() as uow:         # UoW ya abierto
    await uow.tickets.save(ticket)
    await uow.commit()
```

`session_scope` no construye el UoW a propósito: construirlo corre el auto-discovery e
instancia **todos** los repositorios de dominio, un coste absurdo para leer una tabla de
infraestructura.

**Convención de transacción.** `uow_scope` y la dependencia `hx.get_sql_uow` ceden el UoW
**sin entrar** en él, para que el use case controle su propio `async with self.uow:` sin anidar
contextos. Si querés el UoW ya abierto, usá `open_uow_scope` / `hx.get_sql_uow_open`.

---

## Utilidades FastAPI

### `create_app()`

```python
from hexcore.fastapi import create_app

app = create_app()   # ya es una app usable
```

Sin argumentos cablea: `title`/`version` desde `ServerConfig`, CORS desde
`config.allow_origins`, middleware `X-Request-ID`, middleware de timing, mapeo de excepciones
de dominio a HTTP, y las rutas `/health` y `/health/ready`.

Los interruptores van en **un solo objeto**, no en ocho keywords:

```python
from hexcore.fastapi import AppFeatures, create_app

app = create_app(
    features=AppFeatures(cors=False, timing=False),
    routers=[(usuarios_router, {"prefix": "/api/v1"})],
    title="Red API",          # cualquier kwarg de FastAPI se reenvía tal cual
)
```

### `build_lifespan()`

```python
from hexcore.fastapi import (
    BeanieStep, CallableStep, CronSeedStep, EventBusStep,
    ProcrastinateStep, SqlEngineStep, build_lifespan, create_app,
)

app = create_app(
    lifespan=build_lifespan(
        SqlEngineStep(),
        BeanieStep(documents=MONGO_DOCUMENTS),
        EventBusStep(RealtimeEventDispatcher()),
        ProcrastinateStep(procrastinate_app),
        CronSeedStep(CRON_JOBS),
        CallableStep("warm-caches", warm_validation_cache, on_error="warn"),
    ),
)
```

Garantías que son la razón de existir del helper:

- **Teardown en orden inverso**, y sólo de los steps que **sí** arrancaron.
- `on_error="warn"` **por step**: un warmup de caché no debe tumbar el arranque, y eso se
  declara en el step sin relajar la política de todo el arranque.
- Un log por step con su duración.
- Un teardown que falla no impide los siguientes ni tapa la excepción que provocó el apagado.

### Health checks

```python
from hexcore.fastapi import Probe, register_health_routes

register_health_routes(app)                      # /health y /health/ready
register_health_routes(app, path="/_status")     # o donde quieras
```

- `/health` → **liveness**: 200 sin tocar nada. Es lo correcto: si sondeara dependencias, un
  Redis caído provocaría que Kubernetes reiniciara una app perfectamente sana.
- `/health/ready` → **readiness**: `SELECT 1` al engine, `ping` a Redis y a Mongo, con timeout
  propio por sonda y todas concurrentes. Devuelve **503** con el detalle y la latencia por
  dependencia.

Una dependencia no crítica reporta `degraded` en vez de `down` (sin Redis la app sirve más
lento, no deja de servir):

```python
register_health_routes(app, probes=[
    Probe("sql", check_database),
    Probe("cache", check_redis, timeout=1.0, critical=False),
])
```

#### Si tu app ya publica su propio `/health`

Las dos rutas se registran por separado, así que una app **ya en producción** —con su forma de
respuesta y un cliente tipado generado desde su OpenAPI— puede quedarse con la readiness, que es
la parte que no se escribe a mano, sin tocar el contrato que ya publicó:

```python
register_health_routes(app, liveness=False)          # sólo /health/ready
register_health_routes(app, liveness=False, readiness_path="/_ready")
```

Y si lo que hay que conservar es la **forma del cuerpo**, `response_factory` la adapta sin
renunciar a las sondas. El status code lo sigue decidiendo el informe, que es lo que lee el
orquestador:

```python
register_health_routes(
    app,
    response_factory=lambda r: {"ok": r.status != "down", "checks": r.dependencies},
)
```

Lo mismo desde `create_app`, sin apagar la feature entera:

```python
from hexcore.fastapi import AppFeatures, HealthRoutes, create_app

app = create_app(features=AppFeatures(health=HealthRoutes(liveness=False)))
```

### Rate limiting

```python
from fastapi import Depends
from hexcore.fastapi import rate_limit

@router.get("/reports", dependencies=[Depends(rate_limit(10, 60))])
async def reports(): ...

por_usuario = rate_limit(100, 3600, key=lambda r: r.state.user_id)
```

Se apoya en el puerto `ICache`, no en Redis directamente, así que funciona con `MemoryCache` en
tests. Devuelve **429 con `Retry-After`**, y la política ante un backend caído es explícita:

```python
rate_limit(10, 60, on_backend_error="allow")  # default: un Redis caído no tumba la API
rate_limit(10, 60, on_backend_error="deny")
```

### Request-id correlacionado

```python
import logging

from hexcore.fastapi import get_request_id, install_request_id_logging

logging.basicConfig(level=logging.INFO)          # primero: configurá el logging
install_request_id_logging(fmt="%(asctime)s [%(request_id)s] %(message)s")
```

`RequestIDMiddleware` reusa el header entrante si viene (romper la cadena del gateway es perder
la traza) y lo publica en un `ContextVar` y en `request.state`. `install_request_id_logging()`
lo inyecta en **cada línea de log**, que es la mitad del valor: sin eso, tener el header no
correlaciona nada.

> **El orden importa.** `install_request_id_logging()` instrumenta los handlers que **ya
> existen**. En un proceso donde nadie configuró el logging todavía no hay ninguno, así que la
> llamada no tiene nada que hacer — y avisa con un `RuntimeWarning` en vez de quedarse callada.

### Streaming: SSE, WebSocket y límite de conexiones

```python
from hexcore.fastapi import connection_slot, sse_stream, ws_heartbeat

@router.get("/events")
async def events():
    return sse_stream(mi_generador(), heartbeat_seconds=30)
```

El heartbeat es un comentario SSE que los clientes ignoran y los proxies cuentan como tráfico:
sin él, un balanceador con idle timeout corta la conexión. Se añade también
`X-Accel-Buffering: no`, sin el cual nginx acumula los eventos y el stream llega a bloques.

```python
async with connection_slot(cache, f"ws:{user_id}", max_connections=3) as granted:
    if not granted:
        await ws.close(code=1013)
        return
    await ws.accept()
    async with ws_heartbeat(ws, interval=30):
        ...
```

`connection_slot` libera el slot **aunque el bloque lance o se cancele**: filtrar un slot al
desconectarse mal deja al usuario sin poder reconectar hasta que expire, y es el bug clásico de
estos límites.

### Composición de routers

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

`children` acepta también una **secuencia**, y no es azúcar: un dict no puede tener dos claves
`""`, así que un raíz con varios hijos que **ya traen su propio prefijo** —lo normal cuando cada
feature declara sus rutas completas— no se puede expresar con un mapa.

```python
# usuarios_router y tickets_router ya son APIRouter(prefix="/usuarios") y (prefix="/tickets").
api_v1 = build_root_router("/api/v1", [usuarios_router, tickets_router])

# Se pueden mezclar: un router pelado equivale a ("", router).
api_v1 = build_root_router("/api/v1", [usuarios_router, ("/reports", reports_router)])
```

### Endpoints de listado y búsqueda

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

Soporta `limit`/`offset`, `search`, `search_fields`, `filters` (`campo:operador:valor`) y `sort`
(`campo:asc|desc`). Un campo inválido devuelve un **422 estructurado** con `field` y `allowed`,
no un string crudo.

Para listados grandes, donde `OFFSET 100000` obliga a escanear 100.000 filas, hay paginación
por cursor:

```python
import hexcore.sql as sql

page = await repo.query_cursor(sql.CursorRequestDTO(limit=50, sort_field="created_at"))
page.items, page.next_cursor
```

El cursor es opaco (base64url de `(sort_key, id)`) a propósito: si fuera legible, los clientes
lo construirían a mano y quedaría congelado como API pública.

### Providers CQRS

```python
from fastapi import Depends
from hexcore.fastapi import configure_cqrs, provide_command_bus

container = configure_cqrs(registry, enqueuer=enqueuer)   # una vez, al arrancar

@router.post("/tickets")
async def crear(cmd: CrearTicket, bus=Depends(provide_command_bus)):
    return await bus.dispatch(cmd)
```

Existen como funciones por una única razón: poder sobreescribirlas con
`app.dependency_overrides` en los tests. Y `container.build_consumer()` construye el consumer
del worker sobre **los mismos** buses y el mismo serializer, así que no hay dos fuentes de
verdad entre la web y el worker.

---

## Arquitectura CQRS

HexCore integra CQRS de forma nativa, separando las operaciones de escritura (Commands) de las
de lectura (Queries).

Tres buses configurables e independientes:

1. **`AbstractCommandBus`** — despacha intenciones de mutación (`Command`) a un único handler.
   La transacción la gestiona el handler; si preferís que la gestione el bus, añadí
   `TransactionMiddleware` con su `uow_factory`.
2. **`AbstractQueryBus`** — despacha intenciones de lectura (`Query`) y retorna un resultado sin
   mutar estado.
3. **`AbstractEventBus`** — distribuye eventos de dominio a múltiples suscriptores.

```python
import hexcore.cqrs as cqrs

class CrearTicket(cqrs.Command):
    titulo: str

class CrearTicketHandler:
    async def handle(self, cmd: CrearTicket) -> str: ...

registry = cqrs.HandlerRegistry()
registry.register_command_handler(CrearTicket, CrearTicketHandler())

bus = cqrs.InMemoryCommandBus(registry=registry)
await bus.dispatch(CrearTicket(titulo="Algo se rompió"))
```

Para resolver dependencias en el momento del dispatch, registrá un factory:

```python
registry.register_command_handler(
    CrearTicket,
    cqrs.HandlerRegistry.factory(lambda: CrearTicketHandler(build_uow())),
)
```

`HandlerRegistry.factory()` es un marcador explícito. Sin él, un handler que implemente
`__call__` sería indistinguible de un factory.

### Configuración declarativa

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

`middlewares` admite sólo middlewares construibles sin argumentos: se instancian con `cls()`.
Los que necesitan configuración se instancian a mano y se le pasa el `MiddlewarePipeline` al
bus. `options` se reenvía al **bus**, no a los middlewares.

> **`TransactionMiddleware` no es el default.** Comitea después del handler, así que con un
> handler que ya gestiona su transacción comitearías dos veces. Y necesita un `uow_factory`
> construido con *tu* engine, que no se puede expresar como dotted path:
>
> ```python
> cqrs.TransactionMiddleware(uow_factory=lambda: SqlAlchemyUnitOfWork(session=session_factory()))
> ```

> **`RetryMiddleware` y el retry de la cola se multiplican.** Si la cola reintenta 3 veces y el
> middleware 3 veces dentro de cada intento, el handler corre hasta 12 veces, no 6. Con un
> handler no idempotente eso son 12 cobros. Elegí uno: el de la cola para
> `@background_command` (persiste el intento y sobrevive a un reinicio del worker), éste para
> comandos síncronos. El middleware avisa si detecta las dos cosas juntas.

### Migrar desde `UseCase`

Si ya tenés una aplicación escrita con la abstracción `UseCase`, podés migrar
progresivamente con el adaptador incluido:

```python
import hexcore.cqrs as cqrs

class CreateUserUseCase(UseCase[CreateUserCommand, UserDTO]):
    def __init__(self, uow: IUnitOfWork) -> None:
        self.uow = uow

    async def execute(self, request: CreateUserCommand) -> UserDTO: ...

registry.register_command_handler(
    CreateUserCommand,
    cqrs.UseCaseCommandHandler(CreateUserUseCase(uow)),
)
```

Cuando estés listo, el use case pasa a ser un handler puro:

```python
from hexcore.domain.cqrs import AbstractCommandHandler

class CreateUserHandler(AbstractCommandHandler[CreateUserCommand, UserDTO]):
    def __init__(self, uow) -> None:
        self.uow = uow

    async def handle(self, command: CreateUserCommand) -> UserDTO: ...
```

### Almacenamiento híbrido para lecturas

Los Commands escriben en SQL normalizado; las Queries leen de proyecciones desnormalizadas en
Mongo o Redis, sincronizadas por el EventBus.

```python
async def project_user_to_mongodb(event: UserCreatedEvent) -> None:
    await UserReadDocument(id=event.user_id, name=event.full_name).insert()

event_bus.subscribe(UserCreatedEvent, project_user_to_mongodb)
```

HexCore **no** genera los modelos de lectura: el patrón pide que estén diseñados
específicamente para lo que tu UI o API consulta, así que los defines vos. También tenés
`PostgresEventBus(pool, serializer, channel_name)` con `LISTEN/NOTIFY` nativo si no querés
Redis.

---

## Task Queues (Smart Routing)

No necesitás buses separados para código síncrono y asíncrono. HexCore enruta comandos y
eventos hacia las colas con decoradores.

```python
import hexcore.cqrs as cqrs

@cqrs.background_command(queue="high_priority")
class SendEmailCommand(cqrs.Command):
    user_id: str
    template: str

@cqrs.background_handler(queue="analytics")
async def on_user_created(event: UserCreatedEvent) -> None: ...

@cqrs.background_task(queue="maintenance")
async def clean_old_records_task(days_retention: int) -> None: ...
```

Los tres decoradores **rechazan al decorar** cualquier objeto definido dentro de otra función:
su `__qualname__` lleva `<locals>` y el worker nunca podría importarlo. Fallar al importar es
mejor que fallar en el primer job.

### El enqueuer

```python
from hexcore.infrastructure.task_queues.procrastinate_adapter import ProcrastinateEnqueuer

enqueuer = ProcrastinateEnqueuer(procrastinate_app)
```

HexCore trae `ProcrastinateEnqueuer` y `CeleryEnqueuer`. Si necesitás otro broker, implementá
`ITaskEnqueuer` (4 métodos), con dos advertencias:

- **`enqueue_event` no es un `pass`.** Una cola de tareas no puede hacer fan-out a "todos los
  suscriptores", así que los adaptadores oficiales lanzan `NotImplementedError` en vez de
  perder el evento en silencio. Para ejecutar un suscriptor concreto en background usá
  `@background_handler`; para fan-out real, `RedisEventBus` o `PostgresEventBus`.
- **No uses `asyncio.run()` por tarea** en un worker síncrono: cierra el event loop y deja el
  pool del `AsyncEngine` atado a un loop muerto (`Event loop is closed`). El adaptador de
  Celery usa un loop persistente por proceso, expuesto como `run_in_worker_loop(coro)`.

### Los buses con Smart Routing

La forma recomendada es la factory, que propaga enqueuer y serializer y falla al construir si
faltan:

```python
factory = cqrs.CQRSFactory(cqrs.CQRSConfig(), registry, enqueuer=enqueuer)
command_bus = factory.create_command_bus()
event_bus = factory.create_event_bus()
```

Para eventos distribuidos entre réplicas, sustituí el bus in-memory:

```python
from hexcore.infrastructure.cqrs.redis_bus import RedisEventBus

event_bus = RedisEventBus(
    redis_client=redis_client,
    serializer=cqrs.PydanticSerializer(),
    stream_name="hexcore:events",
    group_name="api_workers",
    enqueuer=enqueuer,   # necesario para que el bus pueda enrutar a background
)
```

### El worker

```python
import hexcore.cqrs as cqrs
from hexcore.infrastructure.task_queues.procrastinate_adapter import (
    register_hexcore_procrastinate_tasks,
)

# El MISMO bus que usa el proceso web: el consumer marca el mensaje como "viene del
# worker", así que el bus lo ejecuta en vez de reencolarlo.
consumer = cqrs.CQRSConsumer(command_bus, event_bus)
register_hexcore_procrastinate_tasks(procrastinate_app, consumer)  # idempotente

await cqrs.run_procrastinate_worker(procrastinate_app, queues=["default"], concurrency=4)
```

Un worker sólo-comandos puede omitir el event bus: `cqrs.CQRSConsumer(command_bus)`.

`run_cqrs_worker` es la versión genérica, para cualquier broker:

```python
await cqrs.run_cqrs_worker(
    cqrs.worker_loop("mi-broker", mi_consumidor.run, mi_consumidor.stop),
    scheduler=scheduler,
    on_startup=[init_todo],
    on_shutdown=[cerrar_todo],
)
```

Si **cualquiera** de los bucles muere, se cancela el resto y el proceso sale con `WorkerDied`
para que el orquestador lo reinicie completo: correr con un bucle caído —encolar sin consumir,
o al revés— es peor que caerse. `SIGTERM`/`SIGINT` se traducen a drenaje ordenado.

### Ejecutar un comando "aquí y ahora"

No hay API separada, y es a propósito: **el bus decide por contexto**.

- Fuera de un worker, un `@background_command` se encola.
- Dentro de un worker (el mensaje viene del `CQRSConsumer`) el **mismo** bus lo ejecuta
  localmente. Por eso podés —y debés— compartir un único bus entre la web y el worker.
- Si un handler despacha a propósito otro `@background_command`, ése sí se encola: el contexto
  de worker se consume en el primer dispatch.

`cqrs.is_worker_execution()` responde si el mensaje en curso viene de una cola.

---

## Tareas periódicas dinámicas (cron en caliente)

`DynamicScheduler` lee su configuración de un repositorio, así que podés activar, desactivar o
cambiar horarios **sin reiniciar nada**.

```python
import hexcore.cqrs as cqrs

CRON_JOBS = [
    cqrs.cron_job(clean_old_records_task, "*/5 * * * *", payload={"days_retention": 30}),
    cqrs.cron_job(cerrar_caja, "0 3 * * *"),
]

await cqrs.create_cron_tables()        # o una migración de Alembic
await cqrs.seed_cron_jobs(CRON_JOBS)   # idempotente, y NO pisa lo editado en BD

scheduler = cqrs.DynamicScheduler(
    repository=cqrs.SqlAlchemyCronJobRepository(),
    enqueuer=enqueuer,
    lock_provider=lock_provider,
)
```

Si tu cron vive en SQL no escribas nada: HexCore trae la tabla, el repositorio y el seed. Para
otro origen (Mongo, Redis, un YAML), implementá `cqrs.ICronJobRepository`.

`cron_job()` deriva el `task_name` de `__cqrs_task_name__`: escribirlo a mano es cómo se acaba
con un cron que encola una tarea ya renombrada, y el fallo aparece en el worker.

Cada job lleva además una `description` que viaja a la tabla. El scheduler no la usa: es lo que
un panel le muestra al operador para que sepa **qué hace** un cron antes de desactivarlo — con
sólo `task_name` y una expresión cron no se distingue apagar algo inofensivo de dejar de
facturar. Por defecto sale de la primera línea del docstring de la tarea, que suele ser
exactamente eso:

```python
@cqrs.background_task(queue="billing")
async def emitir_facturas() -> None:
    """Emite las facturas del mes y las envía por email."""
    ...

cqrs.cron_job(emitir_facturas, "0 6 1 * *")
# description == "Emite las facturas del mes y las envía por email."

cqrs.cron_job(cerrar_caja, "0 3 * * *", description="Cierra la caja del día.")
```

> Si tu tabla es anterior a esta columna, la migración es un `add_column` nullable —
> `create_cron_tables()` lo trae escrito en su docstring. Un modelo propio que no la tenga no
> rompe: el seed inserta lo que la tabla admita.

**Cómo decide si toca ejecutar.** No compara contra el minuto actual, sino que busca si hubo
alguna ocurrencia entre `last_run_at` y ahora:

- Un minuto saltado por drift del tick **no** pierde la ejecución.
- `update_last_run` deduplica de verdad, así que un `tick_interval_seconds < 60` no duplica
  dentro del mismo proceso. Entre réplicas hace falta lock, y el scheduler emite un
  `RuntimeWarning` si detecta tick sub-minuto sin `lock_provider`.
- `catch_up_window_seconds` (1 hora) acota el catch-up: un scheduler caído una semana no
  dispara ocurrencias antiguas.

### Locks distribuidos

```python
import hexcore.cqrs as cqrs

lock_provider = cqrs.RedisLockProvider(redis_client)

# O sobre Postgres, si no querés levantar Redis:
lock_provider = cqrs.PostgresLockProvider(asyncpg_pool)
await lock_provider.setup()   # crea tabla e índice, y purga lo expirado
```

El provider de Postgres purga las filas expiradas solo (en `setup()` y cada 100
adquisiciones), así que la tabla no crece sin límite.

**Si el lock no responde**, hay dos respuestas posibles y las dos son malas de formas
distintas, así que la decisión es tuya y explícita:

```python
cqrs.RedisLockProvider(client, on_error="skip")   # default: no correr; el cron se detiene
cqrs.RedisLockProvider(client, on_error="raise")  # propagar, para que el supervisor lo vea
```

En los logs, "no pude decidir" es `critical`; "el lock estaba tomado por otra réplica" —el caso
normal— es `debug`.

---

## Utilidades de test

```python
from hexcore.testing import FakeLockProvider, InMemoryTaskEnqueuer, build_test_buses, override_cqrs

buses = build_test_buses()
buses.registry.register_command_handler(SendEmailCommand, SendEmailHandler())

await buses.command_bus.dispatch(SendEmailCommand(user_id="1", template="welcome"))
assert buses.enqueuer.command_names == ["SendEmailCommand"]
assert buses.enqueuer.commands[0].queue == "high_priority"
```

`build_test_buses()` monta los tres buses **con** enqueuer y serializer, que es el error más
común al testear CQRS: montarlos sin ellos y que el primer `@background_command` lance
`RuntimeError`.

```python
with override_cqrs(app, command_bus=buses.command_bus):
    response = client.post("/tickets", json={...})
```

`override_cqrs` guarda el valor previo de cada override, así que se puede anidar y restaura
aunque el bloque lance — `app.dependency_overrides` es un dict de instancia, y un override que
no se limpia se filtra a todos los tests que reusen la app.

`FakeLockProvider` tiene tres modos: concede siempre, niega siempre, y `shared=True`, que se
comporta como un lock real en memoria para probar dos schedulers concurrentes.

Fixtures de pytest, activables desde tu `conftest.py`:

```python
pytest_plugins = ["hexcore.testing.fixtures"]
```

Trae `anyio_backend`, `task_enqueuer`, `lock_provider`, `cqrs_buses`, `sqlite_engine`,
`sqlite_session` y `uow`.

---

## Repositorios y entidades

### Entidades y eventos

```python
from hexcore.domain.base import BaseEntity
from hexcore.domain.events import EntityCreatedEvent

class User(BaseEntity):
    id: UUID
    name: str

class UserCreatedEvent(EntityCreatedEvent[User]):
    pass
```

### Repositorios genéricos

`SqlAlchemyRepository` y `BeanieRepository` traen los métodos CRUD (`get_by_id`, `list_all`,
`query_all`, `query_cursor`, `save`, `delete`):

```python
from hexcore.infrastructure.repositories.implementations import SqlAlchemyRepository

class UserRepository(SqlAlchemyRepository[UserEntity, UserModel]):
    @property
    def entity_cls(self): return UserEntity

    @property
    def not_found_exception(self): return UserNotFoundException
```

El UoW los inyecta solo, descubriéndolos desde `repository_discovery_paths`. La conversión
modelo → entidad la hace `to_entity_from_model_or_document`, aplicando resolvers para atributos
complejos.

### Documentos Beanie

```python
from hexcore.infrastructure.repositories.orms.beanie.utils import init_beanie_documents

await init_beanie_documents()
```

O declarativamente, con `hx.BeanieStep(documents=[...])` en el lifespan.

---

## Configuración

Define un `config.py` en la raíz del proyecto:

```python
from hexcore.config import ServerConfig

config = ServerConfig(
    app_title="Red API",
    app_version="5.0.0",
    async_sql_database_url="postgresql+asyncpg://user:pass@localhost/red",
    repository_discovery_paths={
        "myapp.features.users.infrastructure.repositories",
        "myapp.features.billing.infrastructure.repositories",
    },
)
```

`LazyConfig` resuelve el módulo de configuración en este orden:

1. `HEXCORE_CONFIG_MODULE`
2. `HEXCORE_CONFIG_MODULES` (lista separada por comas)
3. `LazyConfig.set_config_modules(...)`
4. `config` en la raíz del proyecto

Desde v2, el discovery de repositorios es **explícito**: si `repository_discovery_paths` está
vacío no se carga ningún módulo, y el UoW falla con un error diagnóstico en vez de adivinar.

HexCore no ejecuta I/O ni resuelve la configuración en import time, así que podés llamar a
`LazyConfig.set_config_modules()` antes de que nada la lea.

---

## Darwin: identidad

Darwin es el módulo de identidad nativo de HexCore. Registro, verificación de mail, login,
sesiones con refresh rotativo, revocación, impersonación auditada, y un sistema de plugins
que agrega segundo factor, OAuth, magic links, passkeys y organizaciones sin que el núcleo
los conozca. Todo sale de un solo import:

```python
from hexcore.darwin import (
    IdentityConfig,
    build_identity_router,
    configure_identity,
    identity_startup_steps,
)
```

El arranque completo con SQL:

```python
from hexcore.darwin import (
    IdentityConfig,
    build_identity_router,
    configure_identity,
    identity_startup_steps,
)
from hexcore.fastapi import build_lifespan, create_app, SqlEngineStep, AppFeatures

cfg = IdentityConfig()                         # defaults razonables; ver campos abajo
configure_identity(cfg)                        # una vez, al arrancar

app = create_app(
    features=AppFeatures(auth_context=True, csrf=True),
    lifespan=build_lifespan(SqlEngineStep(), *identity_startup_steps()),
    routers=[build_identity_router()],
)
```

`configure_identity(config, **componentes)` se llama una vez al arrancar. Los componentes
son los puertos a inyectar (`users=`, `clock=`, `key_store=`, `plugins=`, …); sin ellos, el
contenedor usa los defaults de producción. `identity_startup_steps()` devuelve la lista de
pasos para desempaquetar en `build_lifespan`: `IdentityStep` —que valida configuración,
resuelve el backend de almacenamiento y levanta las llaves de firma— y `SessionReaperStep`
—que purga sesiones expiradas en background—. `build_identity_router()` devuelve un
`APIRouter` para `create_app(routers=[...])`.

Cada plugin tiene extra propio incluso cuando no suma paquetes (cuatro de los seis corren con
stdlib más el núcleo). Los extras existen por tres razones: son el nombre estable donde una
dependencia futura entra sin cambiarle el comando de instalación al consumidor
(`[darwin-passkey]` no tenía `webauthn` hasta que lo tuvo); hacen que
`pip install 'hexcore[darwin-two-factor]'` funcione, porque cada extra de plugin arrastra
`hexcore[darwin]`; y documentan la superficie en el único lugar que el consumidor lee antes
de instalar. Lo que el extra deliberadamente **no** hace es exigir un backend de
almacenamiento: «uno de dos» no se expresa en metadata de empaquetado, así que la elección se
resuelve en runtime con `resolve_storage_backend`, que da un error nombrando el extra que
falta.

### El router del núcleo

`build_identity_router(prefix="/auth", tags=("auth",), sign_in_rate_limit=(5, 300), include_sign_up=True)`
monta: `POST /sign-up` (201), `POST /verify-email`, `POST /sign-in`, `POST /refresh`,
`POST /sign-out`, `POST /sign-out-everywhere`, `GET /me`, `GET /sessions`.

Un solo endpoint por operación sirve los dos transportes. El endpoint resuelve el transporte
una vez y le delega la emisión: el cliente web recibe `Set-Cookie` y ningún token en el
cuerpo; el cliente nativo recibe los tokens en el cuerpo y ningún `Set-Cookie`. Duplicar las
rutas duplicaría también los chequeos de seguridad, y la copia que se olvida de uno es la que
se explota.

El `sign_in_rate_limit` default usa `on_backend_error="deny"` — al revés que el default de
`rate_limit` del framework, y a propósito: un Redis caído no debería convertirse en
credential stuffing ilimitado.

> **`POST /sign-up` es un oráculo de enumeración** si lo dejás público tal cual: responde 409
> cuando el mail ya existe. Sirve el caso administrativo; el público conviene escribirlo en
> la app, donde la respuesta es siempre la misma y la diferencia va en el mail que se manda.

### Dualidad de transporte

El token va atado al transporte (`aud`/`tt` distinto), así que una cookie no se puede
replayear como Bearer esquivando CSRF y `SameSite`. `CookieTransport` usa cookies `__Host-`
con `HttpOnly`, `Secure` y `SameSite=Lax`, más chequeo anti-CSRF explícito. `BearerTransport`
devuelve los tokens en el cuerpo y espera `Authorization: Bearer <token>`.
`TransportResolver` decide cuál aplica mirando el request.

### JWT + DB híbrido

El access token es un JWT con `exp` corto (minutos). No se toca la base para validarlo — sólo
se verifican firma, `exp`, audience y transporte. La revocación opera en tres capas:

1. `exp` corto: el token vive poco; si se lo roban, se lo roba por poco.
2. Denylist de `sid` en `ICache`: `SignOut` bloquea el `sid` y un token vigente de esa sesión
   se rechaza la próxima vez, sin esperar a que expire.
3. Contador de generación por usuario: `SignOutEverywhere` incrementa el contador, y todos
   los tokens emitidos con la generación anterior se rechazan sin enumerarlos.

El refresh token **sí** toca la base: rota la sesión atómicamente y hace detección de reuso.
Un refresh robado revoca la familia entera en el primer intento.

El algoritmo está pineado por allowlist, nunca por el `alg` del token. `joserfc` sobre
`pyjwt` justamente porque su API obliga a pasar la lista de algoritmos permitidos: el default
seguro es estructural, no documental.

### Actor vs Subject

La sesión persiste `actor_user_id` **y** `subject_user_id`, no un solo `user_id`.
`AuthContext` expone `actor` (quien ejecuta) y `subject` (a quién afecta). Es lo que hace
auditable la impersonación: un contexto impersonado sin los dos principales no se puede
construir, porque la validación del modelo lo rechaza. Fuera de una impersonación, actor y
subject son el mismo usuario.

### El actor cruza la cola

Cuando un comando se encola durante un request autenticado, el actor viaja en un sobre firmado
(`AuthEnvelopeCodec`) atado al mensaje (`cid`, `mt`). El worker **re-valida la fila de
`session`** en vez de confiar en el `exp`: un token válido en el momento del encolado puede
estar revocado para cuando el worker lo procesa. `IdentityConfig.worker_context_ttl` (default:
24 h) acota la ventana.

### Backends de almacenamiento

`IdentityConfig.storage` acepta `"sqlalchemy"`, `"beanie"` o `None`. Cuando es `None`, se
detecta por los extras instalados. La detección **se niega si están los dos instalados**: es
ambiguo y adivinar sería peor. Los repositorios se resuelven por **contrato de nombre
neutro**: cada backend expone `UserRepository`, `SessionRepository`, `AccountRepository`,
`VerificationRepository` con el mismo nombre, así que el núcleo nunca nombra un backend.

### Alembic y `ensure_identity_schema_loaded`

**Si usás SQL, esto es lo más importante que podés leer.** `ensure_identity_schema_loaded(plugins=[...])`
hay que llamarlo desde el `env.py` de Alembic, y hay que pasarle la lista de plugins activos.
Si falta un plugin ahí, `alembic revision --autogenerate` emite `op.drop_table` sobre sus
tablas — las crea, las ve, las borra. `IdentityStep` verifica al arrancar y loguea la lista
exacta que hay que copiar.

El `env.py` que genera la CLI ya lleva una `DARWIN_PLUGINS: list[str] = []` con el
comentario. Llenalo con los nombres de tus plugins activos:

```python
# env.py — fragmento relevante
from hexcore.darwin import ensure_identity_schema_loaded

DARWIN_PLUGINS: list[str] = ["two_factor", "passkey"]  # los que uses
ensure_identity_schema_loaded(plugins=DARWIN_PLUGINS)
```

Lo mismo aplica con el esquema SQL de cada plugin: `PLUGIN_MODELS` (SQL) y
`PLUGIN_DOCUMENTS` (Beanie) exponen sus modelos para que Alembic los vea.

### Plugins

Los plugins se registran en un `PluginRegistry` que se pasa a `configure_identity`. Cada
plugin aporta —según lo que necesite— un router, comandos CQRS, hooks sobre los flujos del
núcleo, y opcionalmente tablas propias.

```python
from hexcore.darwin import PluginRegistry, configure_identity, IdentityConfig
from hexcore.darwin.plugins.two_factor import TwoFactorPlugin
from hexcore.darwin.plugins.magic_link import MagicLinkPlugin

plugins = PluginRegistry([
    TwoFactorPlugin(issuer="Mi App"),
    MagicLinkPlugin(),
])
configure_identity(IdentityConfig(), plugins=plugins)
```

Los routers de los plugins se montan aparte del del núcleo:

```python
app = create_app(
    features=AppFeatures(auth_context=True, csrf=True),
    lifespan=build_lifespan(SqlEngineStep(), *identity_startup_steps()),
    routers=[build_identity_router(), *plugins.routers()],
)
```

**`MagicLinkPlugin`** — login por link de un solo uso. Reusa la tabla `verification` del
núcleo (no aporta tabla propia). `POST /request` responde igual exista o no el mail: al revés
sería un oráculo de enumeración en una ruta sin autenticación. El default limita a 3 pedidos
cada 15 minutos por IP; sin eso, la ruta es un amplificador de mail gratuito. El TTL del link
es de 15 minutos — corto a propósito, porque es una credencial de portador que viaja por mail
y queda en el historial del cliente y en los logs del proveedor.

**`TwoFactorPlugin`** — TOTP como segundo factor, con el sign-in partido en dos pasos. Un
hook en `user.sign_in.authenticated` corre con la contraseña ya validada y la sesión todavía
no creada, y lanza `TwoFactorRequiredError` sin emitir nada. El TOTP es `hmac` de la stdlib
y el cifrado del secreto reusa el JWE de `joserfc`. El rate limit del endpoint de canje
(`(10, 300)`) **no se apaga**: es la ruta donde se prueban códigos de 6 dígitos, y el techo
por fila (`MAX_FAILED_ATTEMPTS`) sólo protege a un usuario inscripto — el límite por IP es lo
que corta a quien rota entre cuentas.

**`OAuthPlugin`** — Authorization Code con PKCE obligatorio (`S256`, nunca `plain`). Reusa la
tabla `account` del núcleo y aporta una tabla propia sólo para el `state` en vuelo. Por
default **no vincula por coincidencia de mail** (`LinkPolicy.NEVER`): es la toma de cuentas
más común de OAuth. `allowed_redirect_uris` **hay que declararlo en producción**: sin la
lista no se valida nada, y un `redirect_uri` libre deja que un atacante se lleve el código de
la víctima.

**`ImpersonatePlugin`** — «entrar como» otro usuario. No aporta tabla: la fila de `session`
ya lleva `actor_user_id`, `subject_user_id`, `impersonation_reason` e
`impersonation_expires_at` desde la Fase 3. La sesión impersonada tiene un techo de 60
minutos no renovable (el núcleo rechaza el refresh), no hay cadenas (impersonar estando
impersonando está prohibido), y `has_scope` consulta al actor, nunca al subject: impersonar
no presta permisos. `rate_limit` existe aunque la ruta esté autenticada: si un operador queda
comprometido, el límite convierte «impersonar a toda la base» en algo que tarda y se nota.

**`PasskeyPlugin`** — WebAuthn. Lo que se guarda es la clave pública: un dump de la base no
sirve para autenticarse ni acá ni en otro sitio, y el origen está atado por el navegador.
Cambiar el `rp_id` **invalida todas las credenciales registradas**, porque el navegador las
ata al dominio. El contador de firmas se usa para detectar autenticadores clonados: un
contador que dejó de avanzar (pero antes sí lo hacía) rechaza la autenticación y corta la
sesión. `origins` es obligatorio con el verificador por defecto.

**`OrganizationPlugin`** — organizaciones, miembros con tres roles (`owner` > `admin` >
`member`), e invitaciones. Las tres invariantes que sostiene: una organización nunca queda sin
`owner` (contado en la base, no en memoria, para resistir peticiones concurrentes); nadie
asciende a alguien por encima de sí mismo ni actúa sobre un par o un superior; y la
invitación está atada al mail verificado del invitado (sin eso, reenviar el link le da el rol
a cualquiera que lo reciba).

Para el detalle de arquitectura y el plan de desarrollo completo, ver
[`docs/ARCHITECTURE_DARWIN.md`](./docs/ARCHITECTURE_DARWIN.md). Para el sistema de tipos y
los stubs generados, [`docs/ARCHITECTURE_TYPING.md`](./docs/ARCHITECTURE_TYPING.md).

---

## Templates de proyecto (CLI)

```sh
hexcore init mi_proyecto --template hexagonal
hexcore init mi_proyecto --template vertical-slice
```

- `hexagonal` → `src/domain`, `src/application`, `src/infrastructure`.
- `vertical-slice` → `src/features`, `src/shared/{domain,application,infrastructure}`.

Ambos generan `config.py` en la raíz con `repository_discovery_paths` de ejemplo y la
estructura de migraciones con Alembic.

---

## Versiones y soporte

| Serie | Estado | Qué significa |
| :-- | :-- | :-- |
| **6.x** | ✅ **Activa** | La única soportada. Recibe features y correcciones. |
| **5.x** | ⛔ **Deprecada** | Sigue funcionando y los alias anteriores a 5.0 todavía están presentes, pero no recibe correcciones. Incluye los defectos de seguridad de CORS y rate limiting corregidos en 7.0. |
| **4.x** | ⛔ **Deprecada** | Aplicación **parcial**: le faltan el fix del event loop de Celery, las fachadas y la documentación alineada. |
| **3.x** | ⛔ **Deprecada** | Aplicación **parcial**: tiene las correcciones P0/P1 pero ninguna de las factories de FastAPI. |
| **2.x** | ⛔ **Deprecada** | Contiene los bugs silenciosos corregidos en 5.x (ver abajo). |
| **1.x** | ⛔ **Deprecada** | Sin soporte de ningún tipo. |

**Todo lo anterior a 6.0 está deprecado. Migrá a 6.x.**

Los alias anteriores a 5.0 avisaban que se eliminaban "en 6.0". 6.0.0 salió sin eliminarlos —
se prefirió mover la fecha antes que romper retroactivamente a quien ya había actualizado
confiando en que seguían. **En 7.0 se eliminan de verdad**, con dos majors completos de aviso
acumulados. La tabla de reemplazos está más abajo.

> La fila de la serie 7.x se agrega al publicarla: `tests/test_documentation_examples.py`
> verifica que la serie marcada como activa sea la de `pyproject.toml`, así que la tabla y la
> versión no pueden desincronizarse. Ese test es el que detectó que 6.0.0 salió con 5.x todavía
> marcada como activa.

3.0.0 y 4.0.0 existen sólo porque el trabajo se mergeó por fases y cada merge disparó un bump
automático: **no son releases pensadas para usarse**, son cortes intermedios de la misma
migración. 5.0.0 es la primera versión completa.

6.0.0 es el mismo caso: la disparó un PR de documentación con un commit `feat!:`. No hay
**ninguna** ruptura de API entre 5.x y 6.x — los alias anteriores a 5.0 siguen importables y
siguen avisando.

### Por qué 2.x y anteriores no deberían estar en producción

No es una cuestión de estilo: 2.x tiene defectos que no lanzan excepción y no aparecen en logs
de error, así que un proyecto puede estar afectado sin saberlo.

| Defecto en ≤ 2.x | Síntoma |
| :-- | :-- |
| El worker **reencolaba** los `@background_command` en vez de ejecutarlos | Bucle infinito silencioso: la cola crece sin límite y el handler no corre jamás |
| FQN partido con `rsplit(".", 1)` | Un `Command` en una clase contenedora, o una task como `@staticmethod`, se encola bien y **falla en el worker**, donde el mensaje ya no se recupera |
| `PostgresLockProvider` nunca purgaba | ~10.000 filas/día **para siempre** en la BD principal |
| `expire_on_commit` sin pasar | `MissingGreenlet` / `DetachedInstanceError` al leer una entidad tras `commit()` |
| `enqueue_event` era un `pass` | El evento se pierde sin traza |
| `DynamicScheduler` comparaba con el minuto actual | Con `tick=60s` se salta minutos y el job no corre; con `tick<60s` se duplica |
| Los lock providers devolvían `False` ante cualquier error | Una caída de Redis **apaga el cron entero**, con un log indistinguible del caso normal |
| `asyncio.run()` por tarea en Celery | `Event loop is closed` con un `AsyncEngine` compartido |
| `HandlerRegistry` decía ser thread-safe sin ningún lock | Doble instanciación del handler bajo concurrencia |

La superficie de API de v1/v2 **se eliminó en 7.0**. Estuvo deprecada y emitiendo
`DeprecationWarning` desde 5.0, o sea dos majors completos de aviso.

Si venís de 6.x o anterior y usabas alguno de estos nombres, el reemplazo es mecánico: son
renombres, no cambios de comportamiento.

### API removida y su reemplazo

| Removido en 7.0 (era v1/v2) | Usá en su lugar |
| :-- | :-- |
| `ICommandBus`, `IQueryBus`, `IEventBus` | `AbstractCommandBus`, `AbstractQueryBus`, `AbstractEventBus` |
| `ICommandHandler`, `IQueryHandler` | `AbstractCommandHandler`, `AbstractQueryHandler` |
| `IMiddleware` | `AbstractMiddleware` |
| `ISerializer` | `AbstractSerializer` |
| `IEventDispatcher` | `EventBus` |
| `EventBus.register()` / `.dispatch()` | `EventBus.subscribe()` / `.publish()` |
| `ServerConfig.event_dispatcher` | `ServerConfig.event_bus` |
| `SQLAlchemyCommonImplementationsRepo` | `SqlAlchemyRepository` |
| `BeanieODMCommonImplementationsRepo` | `BeanieRepository` |
| `NoSqlUnitOfWork` | `BeanieUnitOfWork` |
| `reset_sqlalchemy_engine()` | `dispose_engine()` |
| `MiddlewareConfig` | **Eliminado en 3.0.** Era código muerto: nunca se leía. |

Pasar `event_dispatcher=` a `ServerConfig` **falla con un error que dice qué usar**, en vez de
ignorarse en silencio: pydantic descarta los kwargs que no conoce, y quedarte con el bus por
defecto sin enterarte se manifestaría mucho después como "mis eventos no llegan".

Si todavía estás en 6.x, corré tus tests con los warnings visibles para ver qué te falta migrar
antes de subir a 7.0:

```sh
python -m pytest -W "default::DeprecationWarning"
```

---

## Guía de migración a 5.x

La API de 2.x sigue funcionando. Lo que sí cambió de **comportamiento** —y por tanto puede
requerir acción— es esto:

### 1. `expire_on_commit=False` en el session factory de HexCore

**Qué cambió.** `get_session_factory()` pasa a `expire_on_commit=False`.

**Por qué.** Con el default de SQLAlchemy (`True`) los atributos expiran al comitear y el
siguiente acceso dispara un lazy-load sobre una sesión cerrada (`MissingGreenlet` /
`DetachedInstanceError`). La documentación de HexCore ya enseñaba `False`, así que doc e
implementación no coincidían.

**Acción.** Ninguna en el caso normal: es el comportamiento que casi todo el mundo quería. Si
dependías de que las entidades se refrescaran tras el commit, construí tu propio
`async_sessionmaker(engine, expire_on_commit=True)`.

### 2. `get_sql_uow` ya no entra al UoW

**Qué cambió.** La dependencia cede el UoW **sin** abrir la transacción.

**Por qué.** Los ejemplos de use case hacen su propio `async with self.uow:`, que con la
dependencia anterior anidaba contextos.

**Acción.** Si tu endpoint operaba sobre un UoW ya abierto, cambiá a `get_sql_uow_open`.

### 3. `TransactionMiddleware` fuera del default, y exige `uow_factory`

**Qué cambió.** `CQRSConfig.command_bus` ya no lo incluye, y `TransactionMiddleware()` sin
`uow_factory` lanza `ValueError`.

**Por qué.** El default armaba la sesión con el session factory *interno* de HexCore en vez del
engine de tu aplicación, y comiteaba tras el handler — así que un handler que ya comitea
comiteaba dos veces.

**Acción.** Si lo querés, declaralo a mano:

```python
cqrs.TransactionMiddleware(uow_factory=lambda: SqlAlchemyUnitOfWork(session=session_factory()))
```

Y recordá que es para handlers que **no** gestionan su propia transacción.

### 4. `enqueue_event` lanza en vez de callar

**Qué cambió.** `ProcrastinateEnqueuer.enqueue_event` y el de Celery lanzan
`NotImplementedError`.

**Por qué.** Eran un `pass`: el evento se perdía sin traza.

**Acción.** Usá `@background_handler` para ejecutar un suscriptor concreto en background, o
`RedisEventBus`/`PostgresEventBus` para fan-out real.

### 5. Los decoradores rechazan objetos no resolubles

**Qué cambió.** `@background_command`/`@background_handler`/`@background_task` lanzan
`ValueError` si el objeto está definido dentro de otra función (`<locals>` en su
`__qualname__`).

**Por qué.** El worker nunca podría importarlo: antes el mensaje se encolaba bien y fallaba en
el worker, donde ya no se puede recuperar.

**Acción.** Mové esas definiciones al nivel de módulo.

### 6. `CQRSFactory` exige el enqueuer si hay comandos de background

**Qué cambió.** Si el registry tiene `@background_command` y la factory no recibió `enqueuer`,
`create_command_bus()` falla al construir.

**Por qué.** Antes construía un bus que lanzaba `RuntimeError` en el primer dispatch — con la
petición del usuario ya en vuelo.

**Acción.** `cqrs.CQRSFactory(config, registry, enqueuer=enqueuer)`.

### 7. Cuándo corre un cron job

**Qué cambió.** `DynamicScheduler` decide por catch-up (¿hubo alguna ocurrencia entre la última
ejecución y ahora?) en vez de comparar con el minuto actual.

**Por qué.** Con `tick=60s` el drift acumulado se saltaba un minuto entero y el job no corría;
con `tick<60s` se duplicaba.

**Acción.** Ninguna: arrancar a las 03:00:30 sigue disparando el job de las 03:00. Si tu
repositorio no implementaba `update_last_run`, implementalo — es lo que deduplica.

### 8. El `detail` del 422 de las queries es un objeto

**Qué cambió.** `build_query_endpoint` devuelve `{"message": ..., "field": ..., "allowed": [...]}`
en vez de un string.

**Acción.** Ajustá el cliente si parseaba `detail` como texto.

### 9. `MiddlewareConfig` eliminado

Era código muerto: `_build_middlewares` instancia con `cls()` y nunca leía
`enabled`/`order`/`options`. Quitá el import; no perdés comportamiento porque no tenía.

---

## Contribuir

Gracias por tu interés. Para mantener la colaboración organizada:

1. **Código de conducta** — revisá el [Código de Conducta](CODE_OF_CONDUCT.md) antes de
   interactuar.
2. **Ramas** — forkeá y creá una rama (`feat/nombre`, `fix/nombre`, `docs/nombre`).
3. **Tests** — toda corrección entra con al menos un test que falle antes y pase después. El
   suite se corre con los extras completos:

   ```sh
   uv sync --extra all --group dev
   uv run python -m pytest -q
   ```

   El CI falla si algún test se **salta**: un skip significa que falta un extra y que
   estaríamos reportando verde sin ejecutar la mitad del suite.
4. **Typecheck** — `uv run pyright hexcore`.
5. **Estilo** — [PEP8](https://pep8.org/). Comentá el *por qué*, no el *qué*.
6. **Commits** — se usa [Commitizen](https://commitizen-tools.github.io/commitizen/):
   `feat:`, `fix:`, `docs:`, `refactor:`, y `!` para breaking changes. El bump de versión y el
   CHANGELOG son automáticos al mergear a `master`.
7. **PRs** — describí el problema, la reproducción, la solución y **por qué esa opción**.
   Relacioná los issues que aplique.

### Skills del proyecto

Hay un conjunto de skills para extender HexCore en VS Code y entornos compatibles:
[Repositorio de Skills de HexCore](https://github.com/Indroic/hexcore-skill).

---

## Referencias

- [DOCS.md](./DOCS.md) — guía de arranque y documentación de clases y funciones.
- [docs/ARCHITECTURE_DARWIN.md](./docs/ARCHITECTURE_DARWIN.md) — arquitectura del módulo de identidad.
- [docs/ARCHITECTURE_TYPING.md](./docs/ARCHITECTURE_TYPING.md) — sistema de tipos y stubs.
- [CHANGELOG.md](./CHANGELOG.md) — historial de cambios.
- [CONTRIBUTING.md](./CONTRIBUTING.md) — pautas de colaboración.
- [SECURITY.md](./SECURITY.md) — política de seguridad.
