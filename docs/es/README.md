# Documentación de HexCore

HexCore es un núcleo reutilizable para aplicaciones Python con **arquitectura hexagonal**,
**DDD**, **CQRS** y **tareas en background**. Trae las abstracciones —entidades, repositorios,
unit of work, buses— *y* la infraestructura que normalmente reescribe cada proyecto: la capa de
sesión SQL, las factories de FastAPI, el runner del worker, el cron dinámico, la identidad y las
utilidades de test.

El objetivo de diseño es que **el camino feliz sea cero configuración**: `create_app()` sin
argumentos da una app usable, `init_engine()` sin argumentos da un engine correcto para
producción.

> 🇬🇧 [English version](../en/) — la versión de referencia. Se escribe primero ahí, y esta es su
> traducción completa. Si las dos se contradicen, la de inglés es la que manda.

---

## Empezar

| # | Documento | Qué cubre |
| :-- | :-- | :-- |
| 1 | **[Instalación](./instalacion.md)** | El paquete, los 17 extras y qué habilita cada uno |
| 2 | **[Inicio rápido](./inicio-rapido.md)** | Una app y un worker completos, en dos pantallas |
| 3 | **[Configuración](./configuracion.md)** | `ServerConfig`, `LazyConfig`, discovery y CORS |

## El framework

| # | Documento | Qué cubre |
| :-- | :-- | :-- |
| 4 | **[Capa SQL](./sql.md)** | Engine, sesiones, scopes, unit of work, Alembic |
| 5 | **[Repositorios y entidades](./repositorios.md)** | `BaseEntity`, eventos, repositorios genéricos, queries y cursor |
| 6 | **[Utilidades FastAPI](./fastapi.md)** | `create_app`, lifespan, health, rate limit, streaming, routers |
| 7 | **[Arquitectura CQRS](./cqrs.md)** | Comandos, queries, eventos, buses, middlewares, factory |
| 8 | **[Colas y workers](./colas-y-workers.md)** | Smart Routing, enqueuers, el consumer y el runner |
| 9 | **[Tareas periódicas](./cron.md)** | Cron dinámico, locks distribuidos, catch-up |
| 10 | **[Testing](./testing.md)** | Buses de prueba, fakes, fixtures, overrides |
| 11 | **[CLI](./cli.md)** | `hexcore init`, migraciones, `hexcore identity` |

## Identidad

| # | Documento | Qué cubre |
| :-- | :-- | :-- |
| 12 | **[Darwin: introducción](./darwin/)** | Qué es, quickstart, y las decisiones que cambian cómo lo integrás |
| 13 | **[Almacenamiento](./darwin/almacenamiento.md)** | Backends, esquema, Alembic, `init_beanie`, modelo de usuario propio |
| 14 | **[Plugins incluidos](./darwin/plugins-incluidos.md)** | Los seis, con rutas y advertencias |
| 15 | **[Escribir un plugin](./darwin/plugins-propios.md)** | Puntos de extensión, hooks y las trampas |

## Referencia

| # | Documento | Qué cubre |
| :-- | :-- | :-- |
| 16 | **[Referencia de API](./referencia.md)** | Todos los símbolos públicos, por fachada |
| 17 | **[Versiones y migración](./versiones-y-migracion.md)** | Política de soporte, API removida, guías de migración |
| 18 | **[Tipado](./tipado.md)** | La regla de la casa, los stubs generados y los gates |

---

## Los tres imports

Hay un módulo fachada por tarea. Reexportan lo público **sin mover nada de sitio**: las rutas
largas siguen funcionando y devuelven el mismo objeto.

```python
import hexcore.fastapi as hx    # create_app, build_lifespan, providers, middlewares, health
import hexcore.cqrs as cqrs     # Command, Query, handlers, decoradores, buses, worker, cron
import hexcore.sql as sql       # init_engine, session_scope, uow_scope, Base, DTOs de query
```

Y uno más para identidad:

```python
import hexcore.darwin as darwin  # IdentityConfig, configure_identity, build_identity_router
```

Las cuatro fachadas resuelven sus nombres **perezosamente** (PEP 562): `import hexcore.cqrs`
funciona sin ningún extra instalado, y `cqrs.SqlAlchemyCronJobRepository` sólo exige `[sql]` en
el momento exacto en que lo pedís. Por eso cada fachada trae un `.pyi` generado: sin él, los
type checkers verían `Any` en los 64 símbolos de `hexcore.cqrs`.

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
| Health checks que sondean de verdad | `hx.register_health_routes()`, `hx.check_health()` | `api` |
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
| Identidad y autenticación | `darwin.configure_identity()`, `darwin.build_identity_router()` | `darwin` + almacenamiento |
| Testear todo lo anterior | `hexcore.testing` | — |

---

## Convenciones de esta documentación

- **Los nombres canónicos son los `Abstract*`.** Los alias `I*` de v1/v2 se eliminaron en 7.0;
  la tabla de reemplazos está en [Versiones y migración](./versiones-y-migracion.md).
- **Los ejemplos son código que corre.** Ver la [regla](../README.md#the-rule-of-this-documentation).
- **Las advertencias marcadas con ⚠️ son modos de falla reales**, no estilo. Casi todas
  describen algo que no lanza excepción y aparece lejos de su causa.
