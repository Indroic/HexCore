# Tareas periódicas (cron dinámico)

`DynamicScheduler` lee su configuración de un **repositorio**, no de un archivo. Podés activar,
desactivar o cambiar el horario de un job **sin reiniciar nada**.

```python
import hexcore.cqrs as cqrs
```

---

## El cableado completo

```python
import hexcore.cqrs as cqrs

CRON_JOBS = [
    cqrs.cron_job(clean_old_records_task, "*/5 * * * *", payload={"days_retention": 30}),
    cqrs.cron_job(cerrar_caja, "0 3 * * *"),
]

await cqrs.create_cron_tables()        # o una migración de Alembic
await cqrs.seed_cron_jobs(CRON_JOBS)   # idempotente, y NO pisa lo editado en la base

scheduler = cqrs.DynamicScheduler(
    repository=cqrs.SqlAlchemyCronJobRepository(),
    enqueuer=enqueuer,
    lock_provider=lock_provider,
)
```

Y se le pasa al runner, que lo corre como un bucle más:

```python
await cqrs.run_procrastinate_worker(procrastinate_app, scheduler=scheduler)
```

Si tu cron vive en SQL no tenés que escribir nada: HexCore trae la tabla, el repositorio y el
seed. Para otro origen —Mongo, Redis, un YAML— implementá `cqrs.ICronJobRepository`:
`get_active_jobs()` y `update_last_run(job_id, run_time)`.

---

## `cron_job()`

```python
cqrs.cron_job(
    task,                      # la corrutina decorada con @background_task
    "0 6 1 * *",               # expresión cron de cinco campos
    *,
    job_id=None,               # default: derivado del task_name
    payload=None,              # kwargs de la tarea
    queue=None,                # default: la del decorador
    is_active=True,
    description=None,          # default: la primera línea del docstring
)
```

El `task_name` sale de `__cqrs_task_name__`, **no se escribe a mano**. Escribirlo a mano es cómo
se acaba con un cron que encola una tarea ya renombrada, y el fallo aparece en el worker, lejos
de donde se cometió.

### La descripción

Cada job lleva una `description` que viaja a la tabla. El scheduler **no la usa**: es lo que un
panel le muestra al operador para que sepa **qué hace** un cron antes de desactivarlo. Con sólo
`task_name` y una expresión cron no se distingue apagar algo inofensivo de dejar de facturar.

Por defecto sale de la primera línea del docstring, que suele ser exactamente eso:

```python
@cqrs.background_task(queue="billing")
async def emitir_facturas() -> None:
    """Emite las facturas del mes y las envía por email."""
    ...

cqrs.cron_job(emitir_facturas, "0 6 1 * *")
# description == "Emite las facturas del mes y las envía por email."

cqrs.cron_job(cerrar_caja, "0 3 * * *", description="Cierra la caja del día.")
```

> Si tu tabla es anterior a esa columna, la migración es un `add_column` nullable —
> `create_cron_tables()` la trae escrita en su docstring—. Un modelo propio que no la tenga no
> rompe: el seed inserta lo que la tabla admita.

---

## La tabla

`CronJobModel` (tabla `hexcore_cron_jobs`) tiene:

| Columna | Tipo | Qué es |
| :-- | :-- | :-- |
| `job_id` | `str` (PK) | Identificador estable |
| `task_name` | `str` | La tarea a encolar |
| `cron_expression` | `str` | Cinco campos |
| `queue` | `str` | Cola destino |
| `payload` | `dict` | Kwargs |
| `is_active` | `bool` | El interruptor que edita el operador |
| `last_run_at` | `datetime \| None` | Lo escribe el scheduler |
| `description` | `str \| None` | Para el panel |

Si necesitás la tabla en otro esquema o con columnas propias, `CronJobModelMixin` se compone con
tu `Base`, y tanto el repositorio como el seed aceptan `model=`.

⚠️ Esa tabla la declara el framework, así que el `env.py` de Alembic tiene que llamar a
`ensure_framework_models_loaded()`. Sin esa línea queda fuera de `Base.metadata` y el próximo
`--autogenerate` le emite un `op.drop_table`.

---

## Cómo decide si toca ejecutar

No compara contra el minuto actual. Busca si hubo **alguna ocurrencia entre `last_run_at` y
ahora**, y eso cambia tres cosas:

1. **Un minuto saltado por drift del tick no pierde la ejecución.** Arrancar a las 03:00:30
   sigue disparando el job de las 03:00.
2. **`update_last_run` deduplica de verdad**, así que un `tick_interval_seconds < 60` no
   duplica dentro del mismo proceso.
3. **`catch_up_window_seconds` (1 hora por default) acota el catch-up**: un scheduler caído una
   semana no dispara de golpe todas las ocurrencias atrasadas.

En 2.x comparaba con el minuto actual: con `tick=60s` el drift acumulado se saltaba un minuto
entero y el job no corría; con `tick<60s` se duplicaba.

```python
cqrs.DynamicScheduler(
    repository=repo,
    enqueuer=enqueuer,
    lock_provider=lock,
    tick_interval_seconds=30,
    catch_up_window_seconds=3600,
)
```

⚠️ Entre **réplicas** hace falta un lock. El scheduler emite un `RuntimeWarning` si detecta un
tick sub-minuto sin `lock_provider`: sin él, dos réplicas encolan el mismo job.

---

## Locks distribuidos

```python
import hexcore.cqrs as cqrs

lock_provider = cqrs.RedisLockProvider(redis_client)
```

O sobre Postgres, si no querés levantar Redis:

```python
lock_provider = cqrs.PostgresLockProvider(asyncpg_pool)
await lock_provider.setup()      # crea tabla e índice, y purga lo expirado
```

El provider de Postgres **purga solo** las filas expiradas: en `setup()` y cada 100
adquisiciones (`purge_every`), con una gracia de una hora (`purge_grace_seconds`). En 2.x nunca
purgaba, y eso eran ~10.000 filas por día, para siempre, en la base principal.

### Si el lock no responde

Hay dos respuestas posibles y las dos son malas de formas distintas, así que la decisión es
tuya y explícita:

```python
cqrs.RedisLockProvider(client, on_error="skip")    # default: no correr; el cron se detiene
cqrs.RedisLockProvider(client, on_error="raise")   # propagar, para que el supervisor lo vea
```

En 2.x los providers devolvían `False` ante *cualquier* error, así que una caída de Redis
apagaba el cron entero con un log indistinguible del caso normal. Hoy los dos casos se
distinguen en los logs:

| Situación | Nivel |
| :-- | :-- |
| «No pude decidir» (el backend falló) | `critical` |
| «El lock lo tiene otra réplica» (el caso normal) | `debug` |

Para escribir un provider propio, implementá `ILockProvider`: `acquire_lock(key, ttl_seconds)` y
`release_lock(key)`.

---

## Operar el cron en caliente

El repositorio SQL expone lo que un panel de administración necesita:

```python
repo = cqrs.SqlAlchemyCronJobRepository()

await repo.get_all_jobs()                    # incluidos los desactivados
await repo.set_active("emitir_facturas", False)
```

`seed_cron_jobs` es idempotente y **no pisa lo editado en la base**: inserta lo que falta y deja
en paz lo que ya está. Un seed que sobreescribiera revertiría, en cada despliegue, el job que un
operador desactivó a las tres de la mañana.

---

## Siguiente

→ **[Testing](./testing.md)**.
