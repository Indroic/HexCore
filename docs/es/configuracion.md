# Configuración

Toda la configuración vive en un objeto pydantic, `ServerConfig`, que HexCore resuelve
perezosamente desde un módulo de tu proyecto. **No hay I/O ni resolución de configuración en
import time**, así que podés cambiar de dónde se lee antes de que nada la lea.

---

## El archivo

Definí un `config.py` en la raíz del proyecto:

```python
from hexcore.config import ServerConfig

config = ServerConfig(
    app_title="Red API",
    app_version="1.4.0",
    async_sql_database_url="postgresql+asyncpg://user:pass@localhost/red",
    repository_discovery_paths={
        "myapp.features.users.infrastructure.repositories",
        "myapp.features.billing.infrastructure.repositories",
    },
)
```

Podés exportar una **instancia** llamada `config`, una **clase** llamada `config` que derive de
`ServerConfig`, o una clase llamada `ServerConfig` que derive de la base. Las tres se aceptan.

---

## Cómo se resuelve (`LazyConfig`)

`LazyConfig` busca el módulo de configuración en este orden y se queda con el primero que
resuelva:

| # | Origen | Ejemplo |
| :-- | :-- | :-- |
| 1 | `HEXCORE_CONFIG_MODULE` (módulo único) | `HEXCORE_CONFIG_MODULE=myapp.settings.prod` |
| 2 | `HEXCORE_CONFIG_MODULES` (lista separada por comas) | `HEXCORE_CONFIG_MODULES=myapp.base,myapp.prod` |
| 3 | `LazyConfig.set_config_modules([...])` | En código, antes de arrancar |
| 4 | El módulo `config` en la raíz | El default |

Si no encuentra nada válido, usa `ServerConfig()` con todos los defaults.

```python
from hexcore.config import LazyConfig

LazyConfig.set_config_modules(["myapp.settings.testing"])
LazyConfig.clear_cache()          # fuerza una resolución nueva

config = LazyConfig.get_config()  # cacheado a partir de acá
```

`clear_cache()` es lo que usan los tests para cambiar de configuración entre casos: sin él, la
primera resolución queda cacheada en la clase para todo el proceso.

---

## Los campos

### Servidor y proyecto

| Campo | Default | Qué hace |
| :-- | :-- | :-- |
| `base_dir` | `Path(".")` | Raíz del proyecto |
| `host` | `"localhost"` | — |
| `port` | `8000` | También alimenta el default de CORS fuera de `debug` |
| `debug` | `True` | Cambia el default de `allow_origins` |
| `app_title` | `"HexCore API"` | Título de FastAPI que usa `create_app()` |
| `app_version` | `"0.1.0"` | Versión de FastAPI que usa `create_app()` |

### Bases de datos

| Campo | Default |
| :-- | :-- |
| `sql_database_url` | `"sqlite:///./db.sqlite3"` |
| `async_sql_database_url` | `"sqlite+aiosqlite:///./db.sqlite3"` |
| `mongo_uri` | `"mongodb://localhost:27017/euphoria_db"` |
| `mongo_db_name` | `"euphoria_db"` |
| `redis_uri` | `"redis://localhost:6379/0"` |
| `redis_cache_duration` | `300` (segundos) |

`sql_database_url` es el DSN **síncrono**, y lo usa el `env.py` de Alembic;
`async_sql_database_url` es el que consume `init_engine()`. Que existan los dos no es
redundancia: Alembic corre migraciones sincrónicas y la app corre asíncrona.

### Seguridad (CORS)

| Campo | Default |
| :-- | :-- |
| `allow_origins` | derivado — ver abajo |
| `allow_credentials` | `True` |
| `allow_methods` | `["*"]` |
| `allow_headers` | `["*"]` |

⚠️ **Este es el campo que más conviene entender.** `allow_origins` se deriva en un validador
`mode="after"`, no en el cuerpo de la clase:

- Si **no** lo pasás: en `debug` queda `["*"]`; fuera de `debug`, `["http://localhost:<port>"]`.
- Si lo pasás explícito —incluso `[]`— se respeta tal cual.

Y hay un invariante que vale **siempre**, no sólo en producción: **`"*"` junto con
`allow_credentials=True` nunca es válido.** El navegador no puede recibir `*` con credenciales,
así que Starlette **refleja el `Origin` del atacante** y agrega
`Access-Control-Allow-Credentials: true`. Cualquier origen puede entonces leer respuestas
autenticadas con la cookie de sesión de la víctima, sin necesidad de XSS.

Según cómo lo hayas pedido:

- Si **no** declaraste `allow_credentials`, se baja a `False` y se emite un warning. Sin ese
  header el navegador no expone la respuesta, así que el reflejo queda inofensivo.
- Si declaraste **las dos cosas**, la app **no arranca**: pediste explícitamente algo que la
  especificación de CORS no permite, y adivinar cuál de las dos querías sería peor que fallar.

```python
config = ServerConfig(
    allow_origins=["https://mi-front.com"],   # la única combinación que sirve con cookies
    allow_credentials=True,
)
```

### Infraestructura inyectable

| Campo | Default | Puerto |
| :-- | :-- | :-- |
| `cache_backend` | `MemoryCache()` | `ICache` |
| `event_bus` | `InMemoryEventBus()` | `EventBus` |

Son instancias, no dotted paths: se cambian por el objeto real, construido con tus parámetros.

### Discovery de repositorios

```python
config = ServerConfig(
    repository_discovery_paths={
        "myapp.features.users.infrastructure.repositories",
    }
)
```

El discovery es **explícito y folder-agnostic**. Si el set está vacío no se carga ningún módulo,
y el Unit of Work falla al construirse con un error diagnóstico en vez de adivinar rutas. Es un
cambio deliberado respecto de v1: adivinar por convención de carpetas ataba el framework a una
estructura de proyecto, y fallaba en silencio cuando la estructura era otra.

### Módulos opcionales

| Campo | Tipo | Default |
| :-- | :-- | :-- |
| `cqrs` | `CQRSConfig \| None` | `None` (deshabilitado) |
| `darwin` | `IdentityConfig \| None` | `None` (deshabilitado) |

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

Los dos se tipan `t.Any` en el modelo, y no es descuido: anotarlos de verdad obligaría a
importar esos módulos desde `hexcore.config`, que carga medio framework —incluida la CLI—.

⚠️ **La clave de firma de Darwin no va en `ServerConfig`.** Todo campo de `ServerConfig` tiene
default, y un secreto de firma con default es lo peor que puede shippear una librería de auth:
la mitad de los despliegues quedaría firmando con el mismo valor de ejemplo. Vive en
`IdentityConfig.secret_key` como `SecretStr` **sin default**, leída de
`HEXCORE_DARWIN_SECRET_KEY`.

---

## Nombres removidos

Pasarle a `ServerConfig` un nombre que se eliminó **falla con remediación** en vez de ignorarse:

```python
ServerConfig(event_dispatcher=bus)
# ValueError: ServerConfig ya no acepta 'event_dispatcher': se eliminó en 7.0 y estaba
# deprecado desde 5.0. Usá 'event_bus'.
```

Sin ese validador, pydantic **descarta en silencio** los kwargs que no conoce: quien migre
pasando `event_dispatcher=` se quedaría con el bus por defecto sin enterarse, y el síntoma
aparecería mucho más tarde como «mis eventos no llegan». No se usa `extra="forbid"` para lograr
lo mismo porque eso rechazaría *cualquier* clave desconocida, y hay consumidores que pasan
kwargs propios a propósito.

---

## Variables de entorno

| Variable | Para qué |
| :-- | :-- |
| `HEXCORE_CONFIG_MODULE` | El módulo de configuración (prioridad máxima) |
| `HEXCORE_CONFIG_MODULES` | Varios módulos candidatos, separados por comas |
| `HEXCORE_DARWIN_SECRET_KEY` | La clave de firma de Darwin |
| `HEXCORE_TEST_MONGO_URI` | Sólo para el suite del repositorio: el Mongo real de los tests `mongo` |

---

## Siguiente

→ **[Capa SQL](./sql.md)** — engine, sesiones y unit of work.
