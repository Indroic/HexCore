# HexCore [![PyPI Downloads](https://static.pepy.tech/personalized-badge/hexcore?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=GREEN&left_text=downloads)](https://pepy.tech/projects/hexcore)

Núcleo reutilizable para aplicaciones Python con **arquitectura hexagonal**, **DDD**, **CQRS**
y **tareas en background**. HexCore trae las abstracciones (entidades, repositorios, UoW,
buses) *y* la infraestructura que normalmente reescribe cada proyecto: la capa de sesión SQL,
las factories de FastAPI, el runner del worker, el cron dinámico, la identidad y las utilidades
de test.

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

## 📚 La documentación

**→ [`docs/`](./docs/) — 🇪🇸 [español](./docs/es/) · 🇬🇧 [English](./docs/en/)**

| | Español | English |
| :-- | :-- | :-- |
| Instalación y extras | [instalacion](./docs/es/instalacion.md) | [installation](./docs/en/installation.md) |
| Inicio rápido | [inicio-rapido](./docs/es/inicio-rapido.md) | [quickstart](./docs/en/quickstart.md) |
| Configuración | [configuracion](./docs/es/configuracion.md) | [configuration](./docs/en/configuration.md) |
| Capa SQL | [sql](./docs/es/sql.md) | [sql](./docs/en/sql.md) |
| Repositorios y entidades | [repositorios](./docs/es/repositorios.md) | [repositories](./docs/en/repositories.md) |
| Utilidades FastAPI | [fastapi](./docs/es/fastapi.md) | [fastapi](./docs/en/fastapi.md) |
| Arquitectura CQRS | [cqrs](./docs/es/cqrs.md) | [cqrs](./docs/en/cqrs.md) |
| Colas y workers | [colas-y-workers](./docs/es/colas-y-workers.md) | [queues-and-workers](./docs/en/queues-and-workers.md) |
| Tareas periódicas | [cron](./docs/es/cron.md) | [cron](./docs/en/cron.md) |
| Testing | [testing](./docs/es/testing.md) | [testing](./docs/en/testing.md) |
| CLI | [cli](./docs/es/cli.md) | [cli](./docs/en/cli.md) |
| **Darwin** (identidad) | [darwin/](./docs/es/darwin/) | [darwin/](./docs/en/darwin/) |
| Referencia de API | [referencia](./docs/es/referencia.md) | [reference](./docs/en/reference.md) |
| Versiones y migración | [versiones-y-migracion](./docs/es/versiones-y-migracion.md) | [versions-and-migration](./docs/en/versions-and-migration.md) |
| Tipado | [tipado](./docs/es/tipado.md) | [typing](./docs/en/typing.md) |

---

## Instalación

```sh
pip install hexcore
```

Requiere Python ≥ 3.12. HexCore no arrastra dependencias pesadas: todo lo que no es el núcleo va
en **extras**, y los módulos que las necesitan sólo las importan cuando los usás.

```sh
pip install "hexcore[api,sql,procrastinate]"
pip install "hexcore[darwin-sqlalchemy]"
pip install "hexcore[all]"
```

| Grupo | Extras |
| :-- | :-- |
| Núcleo | `api`, `sql`, `mongo`, `redis`, `rabbitmq`, `procrastinate`, `celery` |
| Identidad | `darwin`, `darwin-sqlalchemy`, `darwin-beanie`, `darwin-magic-link`, `darwin-two-factor`, `darwin-oauth`, `darwin-impersonate`, `darwin-passkey`, `darwin-organization` |
| Todo | `all` |

La tabla completa, con qué habilita cada uno, está en
[instalación](./docs/es/instalacion.md) · [installation](./docs/en/installation.md).

> `import hexcore.cqrs` funciona sin ningún extra: la resolución de nombres es perezosa, así
> que `hexcore.cqrs.SqlAlchemyCronJobRepository` sólo exige `[sql]` en el momento en que lo
> pedís.

---

## Los cuatro imports

Hay un módulo fachada por tarea. Reexportan lo público **sin mover nada de sitio**: las rutas
largas siguen resolviendo al mismo objeto.

```python
import hexcore.fastapi as hx     # create_app, build_lifespan, providers, middlewares, health
import hexcore.cqrs as cqrs      # Command, Query, handlers, decoradores, buses, worker, cron
import hexcore.sql as sql        # init_engine, session_scope, uow_scope, Base, DTOs de query
import hexcore.darwin as darwin  # IdentityConfig, configure_identity, build_identity_router
```

Las fachadas exponen **sólo los nombres canónicos**. Los alias históricos `I*` se eliminaron en
7.0 — ver [API removida](#api-removida-y-su-reemplazo).

---

## Qué trae, de un vistazo

| Necesitás | API | Extra |
| :-- | :-- | :-- |
| Una app FastAPI cableada | `hx.create_app()`, `hx.AppFeatures` | `api` |
| Orquestar el arranque y el apagado | `hx.build_lifespan()` + steps | `api` |
| Engine y sesiones SQL | `sql.init_engine()`, `sql.PoolSettings` | `sql` |
| Sesión o UoW fuera de un request | `sql.session_scope()`, `sql.uow_scope()` | `sql` |
| Health checks que sondean de verdad | `hx.register_health_routes()` | `api` |
| Rate limiting | `hx.rate_limit()` | `api` |
| SSE / WebSocket / límite de conexiones | `hx.sse_stream()`, `hx.connection_slot()` | `api` |
| Commands, Queries y eventos | `cqrs.Command`, `cqrs.Query`, `cqrs.HandlerRegistry` | — |
| Ejecutar en background | `cqrs.background_command`, `cqrs.background_task` | — |
| Entrypoint del worker | `cqrs.run_cqrs_worker()`, `cqrs.run_procrastinate_worker()` | — |
| Cron editable en caliente | `cqrs.DynamicScheduler`, `cqrs.SqlAlchemyCronJobRepository` | `sql` |
| Locks distribuidos | `cqrs.RedisLockProvider`, `cqrs.PostgresLockProvider` | `redis` / `sql` |
| Identidad y autenticación | `darwin.configure_identity()`, `darwin.build_identity_router()` | `darwin` + almacenamiento |
| Testear todo lo anterior | `hexcore.testing` | — |

---

## Darwin: el módulo de identidad

Registro, verificación de mail, login, sesiones con refresh rotativo, revocación, impersonación
auditada, y un sistema de plugins que agrega segundo factor, OAuth, magic links, passkeys y
organizaciones sin que el núcleo los conozca.

```python
from hexcore.darwin import (
    IdentityConfig,
    build_identity_router,
    configure_identity,
    identity_startup_steps,
)
from hexcore.fastapi import AppFeatures, SqlEngineStep, build_lifespan, create_app

configure_identity(IdentityConfig())

app = create_app(
    features=AppFeatures(auth_context=True, csrf=True),
    lifespan=build_lifespan(SqlEngineStep(), *identity_startup_steps()),
    routers=[build_identity_router()],
)
```

⚠️ Si usás SQL, lo más importante que podés leer antes de desplegar es la sección de Alembic:
[almacenamiento](./docs/es/darwin/almacenamiento.md) · [storage](./docs/en/darwin/storage.md).
Un plugin que falte en el `env.py` hace que `alembic revision --autogenerate` emita
`op.drop_table` sobre sus tablas.

---

## Templates de proyecto (CLI)

```sh
hexcore init mi_proyecto --template hexagonal
hexcore init mi_proyecto --template vertical-slice
```

- `hexagonal` → `src/domain`, `src/application`, `src/infrastructure`.
- `vertical-slice` → `src/features`, `src/shared/{domain,application,infrastructure}`.

Ambos generan `config.py` en la raíz y dejan Alembic configurado. Ver
[CLI](./docs/es/cli.md) · [CLI](./docs/en/cli.md).

---

## Versiones y soporte

| Serie | Estado | Qué significa |
| :-- | :-- | :-- |
| **8.x** | ✅ **Activa** | La única soportada. Recibe features y correcciones. Trae Darwin. |
| **7.x** | ⛔ **Deprecada** | Elimina la superficie anterior a 5.0 y corrige los defectos de CORS y rate limiting. No trae Darwin: se publicó antes de que el módulo llegara a `master`. |
| **6.x** | ⛔ **Deprecada** | Ya no recibe correcciones. Incluye los defectos de seguridad de CORS y rate limiting corregidos en 7.0, y los alias anteriores a 5.0 todavía presentes. |
| **5.x** | ⛔ **Deprecada** | Misma superficie de API que 6.x. |
| **4.x** | ⛔ **Deprecada** | Aplicación **parcial**: le faltan el fix del event loop de Celery, las fachadas y la documentación alineada. |
| **3.x** | ⛔ **Deprecada** | Aplicación **parcial**: tiene las correcciones P0/P1 pero ninguna de las factories de FastAPI. |
| **2.x** | ⛔ **Deprecada** | Contiene bugs silenciosos corregidos en 5.x: el worker reencolaba en vez de ejecutar, el cron se salteaba o duplicaba ejecuciones, y una caída de Redis apagaba el cron entero. |
| **1.x** | ⛔ **Deprecada** | Sin soporte de ningún tipo. |

**Todo lo anterior a 8.0 está deprecado. Migrá a 8.x.** El detalle de cada serie, los bugs
silenciosos de 2.x y las guías paso a paso están en
[versiones y migración](./docs/es/versiones-y-migracion.md) ·
[versions and migration](./docs/en/versions-and-migration.md).

### API removida y su reemplazo

Los alias de v1/v2 estuvieron deprecados desde 5.0 —dos majors completos de aviso— y se
eliminaron en 7.0. El reemplazo es mecánico: son renombres, no cambios de comportamiento.

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
defecto sin enterarte se manifestaría mucho después como «mis eventos no llegan».

Si todavía estás en 6.x, corré tus tests con los warnings visibles para ver qué te falta migrar:

```sh
python -m pytest -W "default::DeprecationWarning"
```

---

## Contribuir

1. **Código de conducta** — revisá el [Código de Conducta](CODE_OF_CONDUCT.md) antes de
   interactuar.
2. **Ramas** — forkeá y creá una rama (`feat/nombre`, `fix/nombre`, `docs/nombre`).
3. **Tests** — toda corrección entra con al menos un test que falle antes y pase después:

   ```sh
   uv sync --extra all --group dev
   uv run python -m pytest -q
   ```

   El CI falla si algún test se **salta**: un skip significa que falta un extra y que
   estaríamos reportando verde sin ejecutar la mitad del suite.
4. **Typecheck** — `uv run pyright hexcore`. El veredicto lo da el ratchet, no el exit code:
   ver [tipado](./docs/es/tipado.md) · [typing](./docs/en/typing.md).
5. **Estilo** — [PEP8](https://pep8.org/). Comentá el *por qué*, no el *qué*.
6. **Commits** — [Commitizen](https://commitizen-tools.github.io/commitizen/): `feat:`, `fix:`,
   `docs:`, `refactor:`, y `!` para breaking changes. El bump de versión y el CHANGELOG son
   automáticos al mergear a `master`.
7. **PRs** — describí el problema, la reproducción, la solución y **por qué esa opción**.

Detalle completo en [CONTRIBUTING.md](CONTRIBUTING.md).

### Skills del proyecto

Hay un conjunto de skills para extender HexCore en VS Code y entornos compatibles:
[Repositorio de Skills de HexCore](https://github.com/Indroic/hexcore-skill).

---

## Referencias

- [docs/](./docs/) — la documentación completa, en español e inglés.
- [docs/ARCHITECTURE_TYPING.md](./docs/ARCHITECTURE_TYPING.md) — sistema de tipos y stubs.
- [CHANGELOG.md](./CHANGELOG.md) — historial de cambios.
- [CONTRIBUTING.md](./CONTRIBUTING.md) — pautas de colaboración.
- [SECURITY.md](./SECURITY.md) — política de seguridad.
