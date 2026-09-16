# CQRS, queues, workers and cron

```python
import hexcore.cqrs as cqrs
```

No extra required: `import hexcore.cqrs` works on a bare install. Individual backends
(`SqlAlchemyCronJobRepository`, `RedisLockProvider`) demand theirs at the moment you ask.

---

## The three buses

| Bus | Dispatches | To how many handlers |
| :-- | :-- | :-- |
| `AbstractCommandBus` | `Command` — mutation intents | Exactly one |
| `AbstractQueryBus` | `Query` — reads, no state change | Exactly one |
| `AbstractEventBus` | `DomainEvent` — facts that already happened | Many subscribers |

A command's transaction belongs to **the handler**. `Command` and `Query` are **frozen**
pydantic models: a message in flight is never mutated, which is what lets a middleware replace
it without anybody observing an intermediate state.

```python
class CreateTicket(cqrs.Command):
    title: str


class CreateTicketHandler:
    async def handle(self, cmd: CreateTicket) -> str:
        ...


registry = cqrs.HandlerRegistry()
registry.register_command_handler(CreateTicket, CreateTicketHandler())

bus = cqrs.InMemoryCommandBus(registry=registry)
await bus.dispatch(CreateTicket(title="Something broke"))
```

The method is `register_command_handler`, not `register_command`.

### Typed handlers

```python
from hexcore.domain.cqrs import AbstractCommandHandler


class CreateTicketHandler(AbstractCommandHandler[CreateTicket, str]):
    def __init__(self, uow) -> None:
        self.uow = uow

    async def handle(self, command: CreateTicket) -> str:
        ...
```

Inheriting is not mandatory — having `handle` is enough — but it is what gives the type
checker the type of the dispatch result. Injecting the UoW straight into the handler is
idiomatic here; the domain-service indirection belongs to the `UseCase` path.

### Factories

A handler needing a fresh UoW per message cannot be registered as an instance:

```python
registry.register_command_handler(
    CreateTicket,
    cqrs.HandlerRegistry.factory(lambda: CreateTicketHandler(build_uow())),
)
```

`HandlerRegistry.factory()` is an **explicit marker**: without it, a handler implementing
`__call__` would be indistinguishable from a factory. `HandlerRegistry(allow_override=True)`
allows replacing a registration; the default raises `DuplicateHandlerError`, because in
production a double registration is almost always a module imported twice. The registry holds
a real lock — the 2.x version claimed thread safety without one and instantiated handlers
twice under concurrency.

---

## Building the buses

```python
factory = cqrs.CQRSFactory(cqrs.CQRSConfig(), registry, enqueuer=enqueuer)

command_bus = factory.create_command_bus()
query_bus = factory.create_query_bus()
event_bus = factory.create_event_bus()
```

⚠️ If the registry holds `@background_command`s and the factory got **no** `enqueuer`,
`create_command_bus()` fails right there. It used to build a bus that raised `RuntimeError` on
the first dispatch — with a user's request already in flight.

In a FastAPI app, `configure_cqrs()` does the same and leaves the container reachable from the
dependencies:

```python
from hexcore.fastapi import configure_cqrs

container = configure_cqrs(registry, enqueuer=enqueuer)
consumer = container.build_consumer()      # the same wiring, for the worker
```

### Declarative configuration

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

`BusConfig`: `backend` (dotted path to the bus class), `middlewares` (dotted paths,
instantiated with `cls()`), `options` (kwargs forwarded **to the bus**, not the middlewares).
`middlewares` only accepts middlewares constructible with no arguments — the ones needing
configuration are built by hand, because a dotted path cannot express "with *this* engine".

---

## Middleware {#middleware}

```python
class Auditing(cqrs.AbstractMiddleware):
    async def handle(self, message, next_handler: cqrs.NextHandler):
        record(message)
        return await next_handler(message)
```

`LoggingMiddleware`, `ValidationMiddleware`, `RetryMiddleware`, `TransactionMiddleware`.

### ⚠️ `TransactionMiddleware` is not the default

It commits **after** the handler, so on a handler that already manages its own transaction you
commit twice. And it needs a `uow_factory` built with *your* engine:

```python
cqrs.TransactionMiddleware(uow_factory=lambda: SqlAlchemyUnitOfWork(session=session_factory()))
```

Without `uow_factory` it raises `ValueError` at construction. In 4.x and earlier it was in the
default and built the session with HexCore's *internal* session factory instead of the app's.

### ⚠️ `RetryMiddleware` and the queue's retry multiply

Queue retries 3 times, middleware retries 3 times inside each attempt: the handler runs up to
**12** times, not 6. With a non-idempotent handler that is 12 charges. Pick one — the queue's
for `@background_command` (it persists the attempt and survives a worker restart), the
middleware for synchronous commands, where there is no queue to retry. The middleware warns if
it detects both.

---

## Serialization and errors

```python
serializer = cqrs.PydanticSerializer()
```

This turns a message into the dict that travels the queue and rebuilds it on the other side.
Do **not** mix two serializers between the web process and the worker. Type resolution is by
fully qualified name, which the deserializer imports — the reason the decorators reject
classes defined inside a function.

`CQRSError` is the base. `HandlerNotFoundError` (no handler for that type),
`DuplicateHandlerError` (two registrations without `allow_override`), `DeserializationError`
(the queue payload does not rebuild the message).

---

## The envelope

Context the worker needs that is not part of the payload: who asked, with which request ID, in
which tenant.

```python
cqrs.register_envelope_metadata_provider("tenant", lambda: current_tenant())
cqrs.register_envelope_restorer("tenant", MyRestorer())
```

Plus `collect_envelope_metadata()`, `restored_envelope_scope(...)`,
`message_correlation_id()`, `registered_envelope_keys()`, `unregister_envelope_key(key)`,
`clear_envelope_registry()`. Darwin uses this so the authenticated actor crosses the queue in
a **signed envelope bound to the message** (`cid`, `mt`): without that binding, a grant
captured from a "delete account" could be re-attached to a "transfer funds".

---

## Smart Routing

```python
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

⚠️ **All three reject, at decoration time,** any object defined inside another function: its
`__qualname__` contains `<locals>` and the worker could never import it. Previously the
message enqueued fine and failed *in the worker*, where it cannot be recovered.

They leave `__cqrs_task_name__` and `__cqrs_queue__` behind — which is where cron gets the
task name, and why you never write it by hand.

**There is no separate "run it now" API: the bus decides by context.**

| Where you are | What `bus.dispatch(cmd)` does with a `@background_command` |
| :-- | :-- |
| In the web process | **Enqueues** it |
| Inside the worker (message came from `CQRSConsumer`) | **Executes** it locally |
| Inside the worker, dispatching *another* background command | **Enqueues** it |

That is why you share one bus between web and worker. `cqrs.is_worker_execution()` asks;
`with cqrs.worker_execution():` forces it, mostly in tests. In 2.x the worker **re-enqueued**
instead of executing: a silent infinite loop.

---

## Enqueuers

The port is `ITaskEnqueuer`, four methods, all taking `(name, payload: dict, queue: str)`.

```python
from hexcore.infrastructure.task_queues.procrastinate_adapter import ProcrastinateEnqueuer
from hexcore.infrastructure.task_queues.celery_adapter import CeleryEnqueuer
```

⚠️ **`enqueue_event` is not a `pass`.** A task queue cannot fan out to "every subscriber" — it
does not know them. Both official adapters raise `NotImplementedError` rather than losing the
event silently, which is what they did in 2.x. Use `@background_handler` for one specific
subscriber, or a distributed bus for real fan-out.

⚠️ **Celery and the event loop.** Do not use `asyncio.run()` per task: it closes the loop and
leaves the `AsyncEngine` pool bound to a dead one, surfacing as `Event loop is closed` in an
unrelated task. The adapter keeps a persistent per-process loop:

```python
from hexcore.infrastructure.task_queues.celery_adapter import run_in_worker_loop

result = run_in_worker_loop(my_coroutine())
```

### Distributed event buses

| Bus | Requires | Note |
| :-- | :-- | :-- |
| `RedisEventBus` | `[redis]` | Redis Streams with consumer groups |
| `PostgresEventBus` | `[sql]` + asyncpg | Native `LISTEN`/`NOTIFY`, no Redis needed |
| `RabbitMQEventBus` | `[rabbitmq]` | AMQP fanout exchange |

All three expose `start_consuming()` and `stop()`, so they wrap in a `worker_loop`. Imported
from `hexcore.infrastructure.cqrs.redis_bus`, `.postgres_bus` and `.rabbitmq`.

Since 9.0 `RedisEventBus` acknowledges **after** the handlers: the `xack` used to run even
when a handler failed, so the message left the PEL and was lost.

---

## The worker

```python
import hexcore.cqrs as cqrs
from hexcore.infrastructure.task_queues.procrastinate_adapter import (
    register_hexcore_procrastinate_tasks,
)

consumer = cqrs.CQRSConsumer(command_bus, event_bus)     # the SAME buses the web process uses
register_hexcore_procrastinate_tasks(procrastinate_app, consumer)

await cqrs.run_procrastinate_worker(
    procrastinate_app,
    queues=["default", "reactive"],
    concurrency=4,
    scheduler=cqrs.DynamicScheduler(repo, enqueuer, lock_provider=lock),
    on_startup=[lambda: cqrs.seed_cron_jobs(CRON_JOBS)],
)
```

A commands-only worker can omit the event bus. `register_hexcore_procrastinate_tasks`
registers `hexcore.process_command`, `hexcore.process_event`, `hexcore.process_handler` and
`hexcore.process_task`, and is idempotent — it returns `False` if they were already there.
The Celery equivalent is `register_hexcore_celery_tasks(app, consumer)`.

The generic runner takes any loops:

```python
await cqrs.run_cqrs_worker(
    cqrs.worker_loop("my-broker", my_consumer.run, my_consumer.stop),
    scheduler=scheduler,
    on_startup=[init_everything],
    drain_timeout=30.0,
)
```

⚠️ **Mutual death.** If *any* loop dies, the runner cancels the rest and the process exits with
`WorkerDied`, so the orchestrator restarts everything. Running with a dead loop — enqueuing
without consuming, or the reverse — is worse than crashing: the queue grows, nobody notices,
and the process keeps reporting itself alive. `SIGTERM`/`SIGINT` become an orderly drain.

Run the worker as a **separate process** from the API, same image, different command.

---

## Cron {#cron}

`DynamicScheduler` reads its configuration from a **repository**, not a file: enable, disable
or reschedule a job without restarting anything.

```python
CRON_JOBS = [
    cqrs.cron_job(clean_old_records_task, "*/5 * * * *", payload={"days_retention": 30}),
    cqrs.cron_job(close_books, "0 3 * * *"),
]

await cqrs.create_cron_tables()        # or an Alembic migration
await cqrs.seed_cron_jobs(CRON_JOBS)   # idempotent, does NOT overwrite database edits

scheduler = cqrs.DynamicScheduler(
    repository=cqrs.SqlAlchemyCronJobRepository(),
    enqueuer=enqueuer,
    lock_provider=lock_provider,
    tick_interval_seconds=30,
    catch_up_window_seconds=3600,
)
```

`cron_job(task, expression, *, job_id=None, payload=None, queue=None, is_active=True,
description=None)`. **Pass the function, not its name** — the name comes from
`__cqrs_task_name__`, and writing it by hand is how you end up with a cron enqueuing a task
that was since renamed, failing in the worker far from the mistake. `description` defaults to
the first line of the docstring and exists for the admin panel: with only a task name and a
cron expression, an operator cannot tell turning off something harmless from stopping
invoicing.

`CronJobModel` (table `hexcore_cron_jobs`) holds `job_id`, `task_name`, `cron_expression`,
`queue`, `payload`, `is_active`, `last_run_at`, `description`. `CronJobModelMixin` composes
with your own `Base` if you need another schema; repository and seed both accept `model=`.

⚠️ That table is framework-declared, so `env.py` must call
`ensure_framework_models_loaded()`.

### How it decides to run

It does **not** compare against the current minute. It looks for any occurrence between
`last_run_at` and now, so a minute skipped by tick drift does not lose the run,
`update_last_run` genuinely deduplicates, and `catch_up_window_seconds` bounds the catch-up so
a scheduler down for a week does not fire every missed occurrence at once.

⚠️ Across **replicas** you need a lock. The scheduler emits a `RuntimeWarning` on a sub-minute
tick with no `lock_provider`: without one, two replicas enqueue the same job.

```python
lock_provider = cqrs.RedisLockProvider(redis_client)
lock_provider = cqrs.PostgresLockProvider(asyncpg_pool)
await lock_provider.setup()      # creates the table and index, purges what expired
```

The Postgres provider purges itself — on `setup()` and every 100 acquisitions. In 2.x it never
purged: ~10,000 rows a day, forever, in the main database.

When the lock does not answer, both responses are bad in different ways, so the choice is
explicit: `on_error="skip"` (default — the cron stalls) or `on_error="raise"` (the supervisor
sees it). The two cases are distinguishable in the logs: "I could not decide" is `critical`,
"another replica holds it" is `debug`.

Operating without a restart:

```python
repo = cqrs.SqlAlchemyCronJobRepository()
await repo.get_all_jobs()                    # including disabled ones
await repo.set_active("issue_invoices", False)
```

`seed_cron_jobs` inserts what is missing and leaves the rest alone. A seed that overwrote
would revert, on every deploy, the job an operator disabled at three in the morning.

---

## Migrating from `UseCase`

```python
registry.register_command_handler(
    CreateUserCommand,
    cqrs.UseCaseCommandHandler(CreateUserUseCase(uow)),
)
```

`UseCase` is **not deprecated** — it is still the right abstraction for orchestrating without
a bus. The adapter buys you not rewriting everything at once.
