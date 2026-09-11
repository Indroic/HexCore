# Colas y workers

No necesitás buses separados para lo síncrono y lo asíncrono. HexCore enruta comandos, handlers
y tareas hacia las colas con **decoradores**, y el mismo bus decide por contexto si encola o
ejecuta. Eso es el **Smart Routing**.

```python
import hexcore.cqrs as cqrs
```

---

## Los tres decoradores

```python
import hexcore.cqrs as cqrs


@cqrs.background_command(queue="high_priority")
class SendEmailCommand(cqrs.Command):
    user_id: str
    template: str


@cqrs.background_handler(queue="analytics")
async def on_user_created(event: UserCreatedEvent) -> None:
    ...


@cqrs.background_task(queue="maintenance")
async def clean_old_records_task(days_retention: int) -> None:
    ...
```

| Decorador | Se aplica a | Qué encola |
| :-- | :-- | :-- |
| `@background_command` | Una clase `Command` | El comando entero, para que el bus lo despache en el worker |
| `@background_handler` | Un suscriptor de evento | **Ese** suscriptor, no el fan-out |
| `@background_task` | Una corrutina cualquiera | La función, con su payload |

⚠️ **Los tres rechazan al decorar** cualquier objeto definido dentro de otra función: su
`__qualname__` lleva `<locals>` y el worker nunca podría importarlo. Antes, el mensaje se
encolaba bien y fallaba **en el worker**, donde ya no se recupera. Fallar al importar es
estrictamente mejor.

Los decoradores dejan dos atributos, que es de donde salen el nombre y la cola:

```python
clean_old_records_task.__cqrs_task_name__   # 'mi_paquete.tasks.clean_old_records_task'
clean_old_records_task.__cqrs_queue__       # 'maintenance'
```

---

## El enqueuer

El puerto es `ITaskEnqueuer`, con cuatro métodos. HexCore trae dos adaptadores:

```python
from hexcore.infrastructure.task_queues.procrastinate_adapter import ProcrastinateEnqueuer

enqueuer = ProcrastinateEnqueuer(procrastinate_app)
```

```python
from hexcore.infrastructure.task_queues.celery_adapter import CeleryEnqueuer

enqueuer = CeleryEnqueuer(celery_app)
```

### ⚠️ `enqueue_event` no es un `pass`

Una cola de tareas no puede hacer fan-out a «todos los suscriptores»: no los conoce. Los dos
adaptadores oficiales lanzan `NotImplementedError` en vez de perder el evento en silencio, que
es lo que hacían en 2.x.

Para ejecutar un suscriptor concreto en background usá `@background_handler`. Para fan-out real
entre réplicas, un event bus distribuido: `RedisEventBus`, `PostgresEventBus` o
`RabbitMQEventBus`.

### ⚠️ Celery y el event loop

**No uses `asyncio.run()` por tarea** en un worker síncrono: cierra el event loop y deja el pool
del `AsyncEngine` atado a un loop muerto, con el síntoma `Event loop is closed` apareciendo en
una tarea que no tiene nada que ver.

El adaptador de Celery usa un **loop persistente por proceso**, expuesto como:

```python
from hexcore.infrastructure.task_queues.celery_adapter import run_in_worker_loop, shutdown_worker_loop

resultado = run_in_worker_loop(mi_corrutina())
shutdown_worker_loop(timeout=5.0)   # en el apagado del worker
```

### Escribir uno propio

Implementá `ITaskEnqueuer`: `enqueue_command`, `enqueue_event`, `enqueue_handler`,
`enqueue_task`. Las cuatro reciben `(nombre, payload: dict, queue: str)`. Si tu broker no puede
hacer fan-out, lanzá `NotImplementedError` en `enqueue_event` en vez de tragarlo.

---

## Los buses con Smart Routing

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
    enqueuer=enqueuer,      # para que el bus pueda enrutar a background
)
```

| Bus distribuido | Requiere | Nota |
| :-- | :-- | :-- |
| `RedisEventBus(client, serializer, stream_name, group_name, consumer_name=None, enqueuer=None)` | `[redis]` | Redis Streams con grupos de consumo |
| `PostgresEventBus(pool, serializer, channel_name, enqueuer=None)` | `[sql]` + asyncpg | `LISTEN`/`NOTIFY` nativo: fan-out sin levantar Redis |
| `RabbitMQEventBus(connection, serializer, pipeline=None, exchange_name=..., queue_name=...)` | `[rabbitmq]` | Exchange fanout de AMQP |

Los tres exponen `start_consuming()` y `stop()`, para envolverlos en un `worker_loop`.

---

## El worker

```python
import hexcore.cqrs as cqrs
from hexcore.infrastructure.task_queues.procrastinate_adapter import (
    register_hexcore_procrastinate_tasks,
)

# El MISMO bus que usa el proceso web.
consumer = cqrs.CQRSConsumer(command_bus, event_bus)
register_hexcore_procrastinate_tasks(procrastinate_app, consumer)   # idempotente

await cqrs.run_procrastinate_worker(
    procrastinate_app,
    queues=["default", "reactive"],
    concurrency=4,
)
```

Un worker sólo-comandos puede omitir el event bus: `cqrs.CQRSConsumer(command_bus)`.

`register_hexcore_procrastinate_tasks` registra cuatro tareas —`hexcore.process_command`,
`hexcore.process_event`, `hexcore.process_handler`, `hexcore.process_task`— y devuelve `False`
si ya estaban. Es idempotente para que llamarlo dos veces (por ejemplo, desde el lifespan y
desde el worker) no rompa. `force=True` re-registra.

El equivalente para Celery es `register_hexcore_celery_tasks(app, consumer)`.

### El runner genérico

```python
await cqrs.run_cqrs_worker(
    cqrs.worker_loop("mi-broker", mi_consumidor.run, mi_consumidor.stop),
    scheduler=scheduler,
    on_startup=[init_todo],
    on_shutdown=[cerrar_todo],
    drain_timeout=30.0,
)
```

| Parámetro | Default | Qué hace |
| :-- | :-- | :-- |
| `*loops` | — | Uno o más `WorkerLoop` (`name`, `run()`, `stop()`) |
| `scheduler` | `None` | Un `DynamicScheduler`, que corre como un bucle más |
| `on_startup` / `on_shutdown` | `()` | Corrutinas, en orden |
| `handle_signals` | `True` | Traduce `SIGTERM`/`SIGINT` a drenaje ordenado |
| `drain_timeout` | `30.0` | Cuánto espera antes de cancelar a lo bruto |

⚠️ **Muerte mutua.** Si *cualquiera* de los bucles muere, el runner cancela el resto y el proceso
sale con `WorkerDied`, para que el orquestador lo reinicie completo. Correr con un bucle caído
—encolar sin consumir, o al revés— es peor que caerse: la cola crece, nadie se entera, y el
proceso sigue reportando «vivo».

`worker_loop(name, run, stop=None)` envuelve cualquier par de callables en un `WorkerLoop`.

---

## «Aquí y ahora» vs «en background»

No hay una API separada, y es a propósito: **el bus decide por contexto**.

| Dónde estás | Qué hace `bus.dispatch(cmd)` con un `@background_command` |
| :-- | :-- |
| En el proceso web | Lo **encola** |
| Dentro del worker (el mensaje viene del `CQRSConsumer`) | Lo **ejecuta** localmente |
| Dentro del worker, pero despachando *otro* `@background_command` | Lo **encola** |

Por eso podés —y debés— compartir un único bus entre la web y el worker. El contexto de worker
se consume en el primer dispatch: si un handler despacha a propósito otro comando de background,
ése sí se encola, que es lo que casi siempre se quiere.

```python
if cqrs.is_worker_execution():
    ...

with cqrs.worker_execution():     # forzarlo, sobre todo en tests
    await bus.dispatch(cmd)
```

En 2.x el worker **reencolaba** los comandos en vez de ejecutarlos: un bucle infinito
silencioso, con la cola creciendo sin límite y el handler sin correr jamás.

---

## Diagrama del flujo

```
   Proceso web                          Broker                     Worker
   ───────────                          ──────                     ──────
   bus.dispatch(cmd)  ──encola──▶  hexcore.process_command  ──▶  CQRSConsumer
        │                                                             │
        │ (mismo bus,                                                 │ marca contexto
        │  mismo serializer)                                          ▼
        └───────────────────────────────────────────────────▶  bus.dispatch(cmd)
                                                                      │
                                                                      ▼
                                                                   handler
```

---

## Errores comunes

| Síntoma | Causa |
| :-- | :-- |
| `RuntimeError` en el primer dispatch de un `@background_command` | El bus se construyó sin `enqueuer`. Usá `CQRSFactory`, que falla al construir |
| El mensaje se encola y el worker no lo encuentra | El mensaje está definido dentro de una función, o el worker no importa el módulo que lo define |
| `Event loop is closed` en Celery | Un `asyncio.run()` por tarea. Usá `run_in_worker_loop` |
| El evento no llega a ningún lado | `enqueue_event` sobre un adaptador de cola. Usá `@background_handler` o un bus distribuido |
| El handler corre 12 veces | `RetryMiddleware` **y** el retry de la cola. Elegí uno |

---

## Siguiente

→ **[Tareas periódicas](./cron.md)**.
