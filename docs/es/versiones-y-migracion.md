# Versiones y migración

---

## Política de soporte

| Serie | Estado | Qué significa |
| :-- | :-- | :-- |
| **8.x** | ✅ **Activa** | La única soportada. Recibe features y correcciones. Trae Darwin. |
| **7.x** | ⛔ Deprecada | Elimina la superficie anterior a 5.0 y corrige los defectos de CORS y rate limiting. **No trae Darwin**: se publicó antes de que el módulo llegara a `master`. |
| **6.x** | ⛔ Deprecada | Funciona, pero no recibe correcciones. Incluye los defectos de CORS y rate limiting corregidos en 7.0, y los alias anteriores a 5.0 todavía presentes. |
| **5.x** | ⛔ Deprecada | Misma superficie de API que 6.x. |
| **4.x** | ⛔ Deprecada | Aplicación **parcial**: le faltan el fix del event loop de Celery, las fachadas y la documentación alineada. |
| **3.x** | ⛔ Deprecada | Aplicación **parcial**: tiene las correcciones P0/P1 pero ninguna de las factories de FastAPI. |
| **2.x** | ⛔ Deprecada | Contiene los bugs silenciosos corregidos en 5.x (abajo). |
| **1.x** | ⛔ Deprecada | Sin soporte de ningún tipo. |

**Todo lo anterior a 8.0 está deprecado.**

3.0.0 y 4.0.0 existen sólo porque el trabajo se mergeó por fases y cada merge disparó un bump
automático: **no son releases pensadas para usarse**, son cortes intermedios de la misma
migración. 5.0.0 es la primera versión completa. 6.0.0 es el mismo caso —la disparó un PR de
documentación con un commit `feat!:`— y no hay **ninguna** ruptura de API entre 5.x y 6.x.

La tabla no puede desincronizarse de la realidad: `tests/test_documentation_examples.py` verifica
que la serie marcada como activa sea la de `pyproject.toml`. Ese test es el que detectó que 6.0.0
salió con 5.x todavía marcada como activa.

---

## Por qué 2.x y anteriores no deberían estar en producción

No es una cuestión de estilo. 2.x tiene defectos que **no lanzan excepción y no aparecen en logs
de error**, así que un proyecto puede estar afectado sin saberlo.

| Defecto en ≤ 2.x | Síntoma |
| :-- | :-- |
| El worker **reencolaba** los `@background_command` en vez de ejecutarlos | Bucle infinito silencioso: la cola crece sin límite y el handler no corre jamás |
| FQN partido con `rsplit(".", 1)` | Un `Command` en una clase contenedora, o una task como `@staticmethod`, se encola bien y **falla en el worker**, donde el mensaje ya no se recupera |
| `PostgresLockProvider` nunca purgaba | ~10.000 filas/día **para siempre** en la base principal |
| `expire_on_commit` sin pasar | `MissingGreenlet` / `DetachedInstanceError` al leer una entidad tras `commit()` |
| `enqueue_event` era un `pass` | El evento se pierde sin traza |
| `DynamicScheduler` comparaba con el minuto actual | Con `tick=60s` se salta minutos; con `tick<60s` se duplica |
| Los lock providers devolvían `False` ante cualquier error | Una caída de Redis **apaga el cron entero**, con un log indistinguible del caso normal |
| `asyncio.run()` por tarea en Celery | `Event loop is closed` con un `AsyncEngine` compartido |
| `HandlerRegistry` decía ser thread-safe sin ningún lock | Doble instanciación del handler bajo concurrencia |

---

## API removida en 7.0, y su reemplazo

Los alias de v1/v2 estuvieron deprecados y emitiendo `DeprecationWarning` desde 5.0 —dos majors
completos de aviso— y **se eliminaron en 7.0**. El reemplazo es mecánico: son renombres, no
cambios de comportamiento.

| Removido (era v1/v2) | Usá en su lugar |
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
| `MiddlewareConfig` | **Eliminado en 3.0.** Era código muerto: nunca se leía |

Los alias avisaban que se eliminaban «en 6.0». 6.0.0 salió sin eliminarlos: se prefirió mover la
fecha antes que romper retroactivamente a quien ya había actualizado confiando en que seguían.

`ServerConfig(event_dispatcher=...)` **falla con un error que dice qué usar**, en vez de
ignorarse en silencio. Ver [Configuración](./configuracion.md#nombres-removidos).

### Ver qué te falta migrar

```sh
python -m pytest -W "default::DeprecationWarning"
```

---

## Cambios de comportamiento en 5.x

La API de 2.x seguía funcionando en 5.x. Lo que cambió de **comportamiento** —y por tanto puede
requerir acción— es esto.

### 1. `expire_on_commit=False` en el session factory

**Qué cambió.** `get_session_factory()` pasa a `expire_on_commit=False`.

**Por qué.** Con el default de SQLAlchemy (`True`) los atributos expiran al comitear y el
siguiente acceso dispara un lazy-load sobre una sesión cerrada. La documentación ya enseñaba
`False`, así que doc e implementación no coincidían.

**Acción.** Ninguna en el caso normal. Si dependías del refresco tras el commit, construí tu
propio `async_sessionmaker(engine, expire_on_commit=True)`.

### 2. `get_sql_uow` ya no entra al UoW

**Qué cambió.** La dependencia cede el UoW **sin** abrir la transacción.

**Por qué.** Los use cases hacen su propio `async with self.uow:`, que con la dependencia
anterior anidaba contextos.

**Acción.** Si tu endpoint operaba sobre un UoW ya abierto, cambiá a `get_sql_uow_open`.

### 3. `TransactionMiddleware` fuera del default, y exige `uow_factory`

**Qué cambió.** `CQRSConfig.command_bus` ya no lo incluye, y `TransactionMiddleware()` sin
`uow_factory` lanza `ValueError`.

**Por qué.** El default armaba la sesión con el session factory *interno* de HexCore en vez del
engine de tu aplicación, y comiteaba tras el handler — así que un handler que ya comitea
comiteaba dos veces.

**Acción.** Si lo querés, declaralo a mano con tu factory. Y recordá que es para handlers que
**no** gestionan su propia transacción.

### 4. `enqueue_event` lanza en vez de callar

**Qué cambió.** El de Procrastinate y el de Celery lanzan `NotImplementedError`.

**Por qué.** Eran un `pass`: el evento se perdía sin traza.

**Acción.** `@background_handler` para ejecutar un suscriptor concreto en background, o
`RedisEventBus`/`PostgresEventBus` para fan-out real.

### 5. Los decoradores rechazan objetos no resolubles

**Qué cambió.** Los tres decoradores lanzan `ValueError` si el objeto está definido dentro de
otra función (`<locals>` en su `__qualname__`).

**Por qué.** El worker nunca podría importarlo: antes el mensaje se encolaba bien y fallaba en
el worker, donde ya no se recupera.

**Acción.** Mové esas definiciones al nivel de módulo.

### 6. `CQRSFactory` exige el enqueuer si hay comandos de background

**Qué cambió.** `create_command_bus()` falla al construir si el registry tiene
`@background_command` y no hubo `enqueuer`.

**Por qué.** Antes construía un bus que lanzaba `RuntimeError` en el primer dispatch, con la
petición del usuario ya en vuelo.

**Acción.** `cqrs.CQRSFactory(config, registry, enqueuer=enqueuer)`.

### 7. Cuándo corre un cron job

**Qué cambió.** `DynamicScheduler` decide por catch-up —¿hubo alguna ocurrencia entre la última
ejecución y ahora?— en vez de comparar con el minuto actual.

**Por qué.** Con `tick=60s` el drift acumulado se saltaba un minuto entero y el job no corría;
con `tick<60s` se duplicaba.

**Acción.** Ninguna. Si tu repositorio no implementaba `update_last_run`, implementalo: es lo que
deduplica.

### 8. El `detail` del 422 de las queries es un objeto

**Qué cambió.** Ahora devuelve `{"message": ..., "field": ..., "allowed": [...]}` en vez de un
string.

**Acción.** Ajustá el cliente si parseaba `detail` como texto.

---

## Cambios de comportamiento en 7.0

### CORS ya no queda abierto de fábrica

**Qué cambió.** `allow_origins` se deriva en un validador que ve el `debug` real de la
instancia, y `"*"` con `allow_credentials=True` deja de ser una configuración válida.

**Por qué.** La derivación estaba en el cuerpo de la clase, donde `debug` es siempre `True`: el
condicional era código muerto y el valor era **siempre** `["*"]`, incluso con
`ServerConfig(debug=False)`. Combinado con `allow_credentials=True`, Starlette refleja el
`Origin` del atacante y agrega `Access-Control-Allow-Credentials: true`.

**Acción.** Si usás cookies de sesión, declará tus orígenes explícitamente. Ver
[Configuración](./configuracion.md#seguridad-cors).

### El rate limit de autenticación falla cerrando

**Qué cambió.** El límite del `sign-in` de Darwin usa `on_backend_error="deny"`, al revés que el
default del framework.

**Por qué.** Un Redis caído no debería convertirse en credential stuffing ilimitado.

---

## 8.0: Darwin

8.0 no rompe la API de 7.x. Lo que agrega es el módulo de identidad completo, con sus nueve
extras. Si venís de 7.x y no usás identidad, la actualización es cambiar la versión.

Si vas a usar Darwin, lo único que **no** es opcional leer es la sección de Alembic:
[Darwin · Almacenamiento](./darwin/almacenamiento.md).

---

## Versionado

El proyecto usa [Commitizen](https://commitizen-tools.github.io/commitizen/) con
`cz_conventional_commits`. El bump de versión y el `CHANGELOG` son **automáticos** al mergear a
`master`:

| Prefijo del commit | Efecto |
| :-- | :-- |
| `fix:` | patch |
| `feat:` | minor |
| `feat!:` / `BREAKING CHANGE:` | major |
| `docs:`, `refactor:`, `test:`, `chore:` | ninguno |

⚠️ Un `feat!:` en un PR de documentación dispara un major. Es literalmente lo que produjo 6.0.0.

---

## Siguiente

← Volver al **[índice](./)**.
