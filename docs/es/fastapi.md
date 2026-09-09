# Utilidades FastAPI

Todo lo de este documento vive en `hexcore.fastapi` y requiere el extra `[api]`.

```python
import hexcore.fastapi as hx
```

---

## `create_app()`

```python
from hexcore.fastapi import create_app

app = create_app()   # ya es una app usable
```

Sin argumentos cablea:

- `title` y `version` desde `ServerConfig` (`app_title`, `app_version`).
- CORS desde `config.allow_origins`.
- `RequestIDMiddleware` (`X-Request-ID`).
- `TimingMiddleware` (`X-Response-Time`).
- El mapeo de excepciones de dominio a HTTP.
- `GET /health` y `GET /health/ready`.

### La firma

```python
create_app(
    lifespan=None,
    *,
    features: AppFeatures | None = None,
    routers: Sequence[MountableRouter] | None = None,
    health_probes: Sequence[Probe] | None = None,
    exception_mapping: dict[type[Exception], int] | None = None,
    exception_headers: HeadersFactory | None = None,
    **fastapi_kwargs,          # se reenvían tal cual a FastAPI(...)
) -> FastAPI
```

### `AppFeatures`

Los interruptores van en **un solo objeto**, no en ocho keywords:

```python
from hexcore.fastapi import AppFeatures, create_app

app = create_app(
    features=AppFeatures(cors=False, timing=False),
    routers=[(usuarios_router, {"prefix": "/api/v1"})],
    title="Red API",
)
```

| Campo | Default | Qué controla |
| :-- | :-- | :-- |
| `cors` | `True` | El `CORSMiddleware` con la config de `ServerConfig` |
| `request_id` | `True` | `RequestIDMiddleware` |
| `timing` | `True` | `TimingMiddleware` |
| `exception_handlers` | `True` | El mapeo de excepciones a HTTP |
| `health` | `True` | Las rutas de salud. Acepta también un `HealthRoutes` |
| `auth_context` | `False` | `AuthContextMiddleware` de Darwin |
| `csrf` | `False` | `CsrfMiddleware` de Darwin |

Los dos últimos vienen apagados porque sólo tienen sentido con identidad cableada: prenderlos
sin `configure_identity()` sería un middleware que mira un contenedor que no existe.

---

## `build_lifespan()`

Orquesta el arranque y el apagado como una lista de **steps**:

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

Las garantías, que son la razón de existir del helper:

1. **Teardown en orden inverso**, y sólo de los steps que **sí** arrancaron. Un step que falló
   no tiene nada que cerrar, y llamarle `stop()` esconde el error original detrás de un
   `AttributeError`.
2. **`on_error` por step.** Un warmup de caché no debe tumbar el arranque, y eso se declara en
   el step sin relajar la política de todo el arranque.
3. **Un log por step con su duración.** Un arranque lento sin esto es un arranque lento sin
   pistas.
4. **Un teardown que falla no impide los siguientes** ni tapa la excepción que provocó el
   apagado.

### Los steps que trae

| Step | Qué hace al arrancar | Al apagar |
| :-- | :-- | :-- |
| `SqlEngineStep(url=None, *, pool=None, **engine_kwargs)` | `init_engine(...)` | `dispose_engine()` |
| `BeanieStep(documents=None)` | `init_beanie` con esos documentos | — |
| `EventBusStep(bus)` | Instala el bus y lo arranca | Lo detiene |
| `CacheStep(backend)` | Instala el backend de caché | Lo cierra |
| `ProcrastinateStep(app)` | Abre la conexión de Procrastinate | La cierra |
| `CronSeedStep(jobs, *, create_tables=False)` | `seed_cron_jobs(jobs)` | — |
| `CallableStep(name, start, stop=None)` | Tu corrutina | La de `stop`, si la diste |

`on_error` acepta `"raise"` (default) o `"warn"`, y se puede pasar por step o para todo el
lifespan: `build_lifespan(*steps, on_error="warn")`.

Escribir uno propio es implementar el protocolo `StartupStep`: un atributo `name`, un
`async def start()`, y opcionalmente un `async def stop()`.

---

## Health checks

```python
from hexcore.fastapi import Probe, register_health_routes

register_health_routes(app)                    # /health y /health/ready
register_health_routes(app, path="/_status")   # o donde quieras
```

| Ruta | Qué es | Qué hace |
| :-- | :-- | :-- |
| `GET /health` | **Liveness** | 200 sin tocar nada |
| `GET /health/ready` | **Readiness** | Sondea las dependencias; 503 con detalle si algo falla |

Que la liveness **no** sondee dependencias es la decisión importante: si lo hiciera, un Redis
caído provocaría que Kubernetes reiniciara una app perfectamente sana, y reiniciarla no arregla
Redis.

La readiness hace `SELECT 1` al engine, `ping` a Redis y a Mongo, **todas las sondas
concurrentes y con timeout propio**, y devuelve la latencia por dependencia.

### Sondas propias

```python
register_health_routes(app, probes=[
    Probe("sql", check_database),
    Probe("cache", check_redis, timeout=1.0, critical=False),
])
```

Una dependencia con `critical=False` reporta `degraded` en vez de `down`: sin Redis la app sirve
más lento, no deja de servir, y tirar la instancia por eso empeora el incidente.

### Si tu app ya publica su propio `/health`

Las dos rutas se registran por separado, así que una app ya en producción —con su forma de
respuesta y un cliente tipado generado desde su OpenAPI— puede quedarse con la readiness, que es
la parte que no se escribe a mano, sin tocar el contrato que ya publicó:

```python
register_health_routes(app, liveness=False)
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

### Fuera de una ruta

```python
from hexcore.fastapi import check_health

report = await check_health(deep=True)
report.status            # "up" | "degraded" | "down"
report.dependencies      # list[DependencyReport], con latency_ms y detail
report.http_status()     # 200 o 503
```

---

## Rate limiting

```python
from fastapi import Depends
from hexcore.fastapi import rate_limit

@router.get("/reports", dependencies=[Depends(rate_limit(10, 60))])
async def reports(): ...

por_usuario = rate_limit(100, 3600, key=lambda r: r.state.user_id)
```

Se apoya en el puerto `ICache`, no en Redis directamente, así que funciona con `MemoryCache` en
tests. Devuelve **429 con `Retry-After`**.

### La política ante un backend caído es explícita

```python
rate_limit(10, 60, on_backend_error="allow")   # default: un Redis caído no tumba la API
rate_limit(10, 60, on_backend_error="deny")
```

No hay default universalmente correcto, así que la decisión es tuya. `"allow"` es el default
porque en la mayoría de las rutas un límite es una protección de capacidad, no de seguridad, y
convertir una caída de caché en una caída total es peor. En las rutas de autenticación la
respuesta se invierte: el `sign_in_rate_limit` de Darwin usa `"deny"`, porque un Redis caído no
debería convertirse en credential stuffing ilimitado.

### La clave

`client_ip_key` es el default. Detrás de un proxy, el IP que ve la app es el del proxy, así que:

```python
from hexcore.infrastructure.api.rate_limit import forwarded_ip_key

rate_limit(10, 60, key=forwarded_ip_key(trusted_proxies={"10.0.0.1"}, trust_hops=1))
```

`trusted_proxies` es obligatorio: sin la lista, `X-Forwarded-For` lo escribe el cliente y el
límite se esquiva cambiando un header.

---

## Request-id correlacionado

```python
import logging

from hexcore.fastapi import get_request_id, install_request_id_logging

logging.basicConfig(level=logging.INFO)          # primero: configurá el logging
install_request_id_logging(fmt="%(asctime)s [%(request_id)s] %(message)s")
```

`RequestIDMiddleware` **reusa el header entrante si viene** —romper la cadena del gateway es
perder la traza— y lo publica en un `ContextVar` y en `request.state`.
`install_request_id_logging()` lo inyecta en **cada línea de log**, que es la mitad del valor:
sin eso, tener el header no correlaciona nada.

⚠️ **El orden importa.** `install_request_id_logging()` instrumenta los handlers que **ya
existen**. En un proceso donde nadie configuró el logging todavía no hay ninguno, así que la
llamada no tiene nada que hacer, y avisa con un `RuntimeWarning` en vez de quedarse callada.

Desde cualquier punto del request:

```python
rid = get_request_id()
```

---

## Excepciones de dominio a HTTP

```python
from hexcore.fastapi import register_exception_handlers

register_exception_handlers(app, mapping={TicketNoEncontrado: 404})
```

`DEFAULT_EXCEPTION_STATUS_MAP` trae el mapeo base del framework. `create_app` lo mergea con el
de identidad y, encima, con el tuyo (`exception_mapping=`), así que tu mapa **gana**.

`include_detail` acepta `False` para no filtrar el mensaje de la excepción al cliente, o un
callable que decide qué exponer por excepción. `headers_for` agrega headers por excepción: es
lo que usa Darwin para el `WWW-Authenticate` de los 401.

---

## Streaming: SSE, WebSocket y límite de conexiones

```python
from hexcore.fastapi import sse_stream

@router.get("/events")
async def events():
    return sse_stream(mi_generador(), heartbeat_seconds=30)
```

El heartbeat es un comentario SSE que los clientes ignoran y los proxies cuentan como tráfico:
sin él, un balanceador con idle timeout corta la conexión. Se añade también
`X-Accel-Buffering: no`, sin el cual nginx acumula los eventos y el stream llega a bloques.

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

`connection_slot` libera el slot **aunque el bloque lance o se cancele**. Filtrar un slot al
desconectarse mal deja al usuario sin poder reconectar hasta que expire el TTL, y es el bug
clásico de estos límites.

---

## Composición de routers

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

`children` acepta un **dict** `{prefijo: router}` o una **secuencia**, y no es azúcar: un dict no
puede tener dos claves `""`, así que un raíz con varios hijos que **ya traen su propio prefijo**
—lo normal cuando cada feature declara sus rutas completas— no se puede expresar con un mapa.

```python
# usuarios_router ya es APIRouter(prefix="/usuarios"), tickets_router idem.
api_v1 = build_root_router("/api/v1", [usuarios_router, tickets_router])

# Se pueden mezclar: un router pelado equivale a ("", router).
api_v1 = build_root_router("/api/v1", [usuarios_router, ("/reports", reports_router)])
```

---

## Endpoints de listado y búsqueda

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

Genera un `GET` con `limit`, `offset`, `search`, `search_fields`, `filters`
(`campo:operador:valor`) y `sort` (`campo:asc|desc`) como query params, documentados en el
OpenAPI.

Un campo inválido devuelve un **422 estructurado**:

```json
{"detail": {"message": "…", "field": "nombre_inexistente", "allowed": ["titulo", "estado"]}}
```

`build_query_endpoint` devuelve la función sin registrarla, por si querés montarla vos.

---

## Dependencias de sesión y UoW

| Dependencia | Cede |
| :-- | :-- |
| `hx.get_session` | Una `AsyncSession` |
| `hx.get_sql_uow` | El UoW **sin abrir** — el use case hace su `async with` |
| `hx.get_sql_uow_open` | El UoW ya abierto |
| `hx.get_nosql_uow` | El UoW de Beanie |

---

## Providers CQRS

```python
from fastapi import Depends
from hexcore.fastapi import configure_cqrs, provide_command_bus

container = configure_cqrs(registry, enqueuer=enqueuer)   # una vez, al arrancar

@router.post("/tickets")
async def crear(cmd: CrearTicket, bus=Depends(provide_command_bus)):
    return await bus.dispatch(cmd)
```

| Provider | Cede |
| :-- | :-- |
| `provide_command_bus` | El command bus |
| `provide_query_bus` | El query bus |
| `provide_event_bus` | El event bus |
| `provide_registry` | El `HandlerRegistry` |
| `get_cqrs_container` | El contenedor entero |

Existen como **funciones** por una única razón: poder sobreescribirlas con
`app.dependency_overrides` en los tests. `reset_cqrs()` limpia el contenedor entre casos.

Y `container.build_consumer()` construye el consumer del worker sobre **los mismos** buses y el
mismo serializer, así que no hay dos fuentes de verdad entre la web y el worker.

---

## Siguiente

→ **[Arquitectura CQRS](./cqrs.md)**.
