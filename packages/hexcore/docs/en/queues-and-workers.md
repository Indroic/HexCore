# Queues and workers

You do not need separate buses for synchronous and asynchronous code. HexCore routes commands,
handlers and tasks to queues with **decorators**, and the same bus decides by context whether to
enqueue or to execute. That is **Smart Routing**.

```python
import hexcore.cqrs as cqrs
```

---

## The three decorators

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

| Decorator | Applies to | What gets enqueued |
| :-- | :-- | :-- |
| `@background_command` | A `Command` class | The whole command, for the bus to dispatch in the worker |
| `@background_handler` | An event subscriber | **That** subscriber, not the fan-out |
| `@background_task` | Any coroutine | The function, with its payload |

⚠️ **All three reject at decoration time** any object defined inside another function: its
`__qualname__` contains `<locals>` and the worker could never import it. Previously the message
enqueued fine and failed **in the worker**, where it can no longer be recovered. Failing at import
is strictly better.

The decorators leave two attributes behind, which is where the name and the queue come from:

```python
clean_old_records_task.__cqrs_task_name__   # 'my_package.tasks.clean_old_records_task'
clean_old_records_task.__cqrs_queue__       # 'maintenance'
```

---

## The enqueuer

The port is `ITaskEnqueuer`, with four methods. HexCore ships two adapters:

```python
from hexcore.infrastructure.task_queues.procrastinate_adapter import ProcrastinateEnqueuer

enqueuer = ProcrastinateEnqueuer(procrastinate_app)
```

```python
from hexcore.infrastructure.task_queues.celery_adapter import CeleryEnqueuer

enqueuer = CeleryEnqueuer(celery_app)
```

### ⚠️ `enqueue_event` is not a `pass`

A task queue cannot fan out to "every subscriber": it does not know them. Both official adapters
raise `NotImplementedError` rather than losing the event silently, which is what they did in 2.x.

To run one specific subscriber in the background use `@background_handler`. For real cross-replica
fan-out, use a distributed event bus: `RedisEventBus`, `PostgresEventBus` or `RabbitMQEventBus`.

### ⚠️ Celery and the event loop

**Do not use `asyncio.run()` per task** in a synchronous worker: it closes the event loop and
leaves the `AsyncEngine` pool bound to a dead one, with the symptom `Event loop is closed`
surfacing in a task that has nothing to do with it.

The Celery adapter uses a **persistent per-process loop**, exposed as:

```python
from hexcore.infrastructure.task_queues.celery_adapter import run_in_worker_loop, shutdown_worker_loop

result = run_in_worker_loop(my_coroutine())
shutdown_worker_loop(timeout=5.0)   # on worker shutdown
```

### Writing your own

Implement `ITaskEnqueuer`: `enqueue_command`, `enqueue_event`, `enqueue_handler`, `enqueue_task`.
All four receive `(name, payload: dict, queue: str)`. If your broker cannot fan out, raise
`NotImplementedError` in `enqueue_event` rather than swallowing it.

---

## The buses with Smart Routing

```python
factory = cqrs.CQRSFactory(cqrs.CQRSConfig(), registry, enqueuer=enqueuer)
command_bus = factory.create_command_bus()
event_bus = factory.create_event_bus()
```

For events distributed across replicas, replace the in-memory bus:

```python
from hexcore.infrastructure.cqrs.redis_bus import RedisEventBus

event_bus = RedisEventBus(
    redis_client=redis_client,
    serializer=cqrs.PydanticSerializer(),
    stream_name="hexcore:events",
    group_name="api_workers",
    enqueuer=enqueuer,      # so the bus can route to background
)
```

| Distributed bus | Requires | Note |
| :-- | :-- | :-- |
| `RedisEventBus(client, serializer, stream_name, group_name, consumer_name=None, enqueuer=None)` | `[redis]` | Redis Streams with consumer groups |
| `PostgresEventBus(pool, serializer, channel_name, enqueuer=None)` | `[sql]` + asyncpg | Native `LISTEN`/`NOTIFY`: fan-out without standing up Redis |
| `RabbitMQEventBus(connection, serializer, pipeline=None, exchange_name=..., queue_name=...)` | `[rabbitmq]` | AMQP fanout exchange |

All three expose `start_consuming()` and `stop()`, so they can be wrapped in a `worker_loop`.

---

## The worker

```python
import hexcore.cqrs as cqrs
from hexcore.infrastructure.task_queues.procrastinate_adapter import (
    register_hexcore_procrastinate_tasks,
)

# The SAME bus the web process uses.
consumer = cqrs.CQRSConsumer(command_bus, event_bus)
register_hexcore_procrastinate_tasks(procrastinate_app, consumer)   # idempotent

await cqrs.run_procrastinate_worker(
    procrastinate_app,
    queues=["default", "reactive"],
    concurrency=4,
)
```

A commands-only worker can omit the event bus: `cqrs.CQRSConsumer(command_bus)`.

`register_hexcore_procrastinate_tasks` registers four tasks — `hexcore.process_command`,
`hexcore.process_event`, `hexcore.process_handler`, `hexcore.process_task` — and returns `False`
if they were already there. It is idempotent so that calling it twice (say, from the lifespan and
from the worker) does not break. `force=True` re-registers.

The Celery equivalent is `register_hexcore_celery_tasks(app, consumer)`.

### The generic runner

```python
await cqrs.run_cqrs_worker(
    cqrs.worker_loop("my-broker", my_consumer.run, my_consumer.stop),
    scheduler=scheduler,
    on_startup=[init_everything],
    on_shutdown=[close_everything],
    drain_timeout=30.0,
)
```

| Parameter | Default | What it does |
| :-- | :-- | :-- |
| `*loops` | — | One or more `WorkerLoop` (`name`, `run()`, `stop()`) |
| `scheduler` | `None` | A `DynamicScheduler`, which runs as one more loop |
| `on_startup` / `on_shutdown` | `()` | Coroutines, in order |
| `handle_signals` | `True` | Translates `SIGTERM`/`SIGINT` into an orderly drain |
| `drain_timeout` | `30.0` | How long to wait before cancelling hard |

⚠️ **Mutual death.** If *any* loop dies, the runner cancels the rest and the process exits with
`WorkerDied`, so the orchestrator restarts the whole thing. Running with a dead loop — enqueuing
without consuming, or the reverse — is worse than crashing: the queue grows, nobody notices, and
the process keeps reporting itself alive.

`worker_loop(name, run, stop=None)` wraps any pair of callables into a `WorkerLoop`.

---

## "Right here, right now" vs "in the background"

There is no separate API, and that is on purpose: **the bus decides by context**.

| Where you are | What `bus.dispatch(cmd)` does with a `@background_command` |
| :-- | :-- |
| In the web process | **Enqueues** it |
| Inside the worker (message came from `CQRSConsumer`) | **Executes** it locally |
| Inside the worker, dispatching *another* `@background_command` | **Enqueues** it |

That is why you can — and should — share a single bus between web and worker. The worker context
is consumed on the first dispatch: if a handler deliberately dispatches another background
command, that one does get enqueued, which is almost always what you want.

```python
if cqrs.is_worker_execution():
    ...

with cqrs.worker_execution():     # force it, mostly in tests
    await bus.dispatch(cmd)
```

In 2.x the worker **re-enqueued** commands instead of executing them: a silent infinite loop, with
the queue growing without bound and the handler never running.

---

## Flow diagram

```
   Web process                          Broker                     Worker
   ───────────                          ──────                     ──────
   bus.dispatch(cmd)  ──enqueue──▶  hexcore.process_command  ──▶  CQRSConsumer
        │                                                             │
        │ (same bus,                                                  │ marks context
        │  same serializer)                                           ▼
        └───────────────────────────────────────────────────▶  bus.dispatch(cmd)
                                                                      │
                                                                      ▼
                                                                   handler
```

---

## Common errors

| Symptom | Cause |
| :-- | :-- |
| `RuntimeError` on the first dispatch of a `@background_command` | The bus was built without an `enqueuer`. Use `CQRSFactory`, which fails at construction |
| The message enqueues and the worker cannot find it | The message is defined inside a function, or the worker does not import the module that defines it |
| `Event loop is closed` in Celery | An `asyncio.run()` per task. Use `run_in_worker_loop` |
| The event goes nowhere | `enqueue_event` on a task-queue adapter. Use `@background_handler` or a distributed bus |
| The handler runs 12 times | `RetryMiddleware` **and** the queue's retry. Pick one |

---

## Next

→ **[Scheduled tasks](./cron.md)**.
