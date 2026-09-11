# CQRS architecture

HexCore integrates CQRS natively, separating write operations (**Command**) from read operations
(**Query**). No extra is required: `import hexcore.cqrs` works on a bare install.

```python
import hexcore.cqrs as cqrs
```

---

## The three buses

| Bus | Dispatches | To how many handlers |
| :-- | :-- | :-- |
| `AbstractCommandBus` | `Command` — mutation intents | Exactly one |
| `AbstractQueryBus` | `Query` — read intents, no state change | Exactly one |
| `AbstractEventBus` | `DomainEvent` — facts that already happened | Many subscribers |

A command's transaction is managed by **the handler**. If you would rather the bus managed it,
add `TransactionMiddleware` with its `uow_factory` — with the caveats below.

---

## The minimum

```python
import hexcore.cqrs as cqrs


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

`Command` and `Query` are **frozen** pydantic models: a message in flight is never mutated. That
is what lets a hook or a middleware replace it with another one without anybody observing an
intermediate state.

### Typed handlers

```python
from hexcore.domain.cqrs import AbstractCommandHandler


class CreateTicketHandler(AbstractCommandHandler[CreateTicket, str]):
    def __init__(self, uow) -> None:
        self.uow = uow

    async def handle(self, command: CreateTicket) -> str:
        ...
```

Inheriting from the abstract class is not mandatory — having `handle` is enough — but it is what
gives the type checker the type of the dispatch result.

---

## Factories: resolving dependencies at dispatch time

A handler that needs a fresh UoW per message cannot be registered as an instance:

```python
registry.register_command_handler(
    CreateTicket,
    cqrs.HandlerRegistry.factory(lambda: CreateTicketHandler(build_uow())),
)
```

`HandlerRegistry.factory()` is an **explicit marker**. Without it, a handler implementing
`__call__` would be indistinguishable from a factory, and the registry would have to guess.

`HandlerRegistry(allow_override=True)` allows replacing an already-registered handler; the default
is `False` and raises `DuplicateHandlerError`, because in production a double registration is
almost always a module imported twice.

The registry is **genuinely thread-safe** (it holds a lock). The 2.x version claimed to be
without any lock, and under concurrency it instantiated the handler twice.

---

## The bus factory

The recommended way to build all three. It propagates the enqueuer and serializer, and **fails at
construction** if something is missing:

```python
factory = cqrs.CQRSFactory(cqrs.CQRSConfig(), registry, enqueuer=enqueuer)

command_bus = factory.create_command_bus()
query_bus = factory.create_query_bus()
event_bus = factory.create_event_bus()
```

⚠️ If the registry holds commands marked `@background_command` and the factory did **not** receive
an `enqueuer`, `create_command_bus()` fails right there. It used to build a bus that raised
`RuntimeError` on the first dispatch — with the user's request already in flight.

In a FastAPI app, `configure_cqrs()` does the same and leaves the container reachable from the
dependencies:

```python
from hexcore.fastapi import configure_cqrs

container = configure_cqrs(registry, enqueuer=enqueuer)
consumer = container.build_consumer()      # the same wiring, for the worker
```

---

## Declarative configuration

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

| `BusConfig` field | What it does |
| :-- | :-- |
| `backend` | Dotted path to the bus class |
| `middlewares` | Dotted paths, instantiated with `cls()` |
| `options` | Kwargs forwarded **to the bus**, not to the middlewares |

`middlewares` only accepts middlewares constructible **with no arguments**. The ones needing
configuration are instantiated by hand and the `MiddlewarePipeline` is passed to the bus: a
dotted path cannot express "with *this* engine".

---

## Middleware

```python
class Auditing(cqrs.AbstractMiddleware):
    async def handle(self, message, next_handler: cqrs.NextHandler):
        record(message)
        result = await next_handler(message)
        return result
```

The ones the framework ships:

| Middleware | What it does |
| :-- | :-- |
| `LoggingMiddleware` | Logs each message, its duration and its result |
| `ValidationMiddleware` | Re-validates the message before the handler |
| `RetryMiddleware` | Retries the handler on exception |
| `TransactionMiddleware` | Opens a UoW and commits after the handler |

### ⚠️ `TransactionMiddleware` is not the default

It commits **after** the handler, so with a handler that already manages its own transaction you
would commit twice. And it needs a `uow_factory` built with *your* engine, which cannot be
expressed as a dotted path:

```python
cqrs.TransactionMiddleware(
    uow_factory=lambda: SqlAlchemyUnitOfWork(session=session_factory())
)
```

Without `uow_factory` it raises `ValueError` at construction. In 4.x and earlier it was in the
default and built the session with HexCore's *internal* session factory instead of the app's
engine.

### ⚠️ `RetryMiddleware` and the queue's retry multiply

If the queue retries 3 times and the middleware retries 3 times inside each attempt, the handler
runs up to **12** times, not 6. With a non-idempotent handler that is 12 charges.

Pick one:

- **The queue's** for `@background_command`: it persists the attempt and survives a worker
  restart.
- **The middleware** for synchronous commands, where there is no queue to retry.

The middleware warns if it detects both at once.

---

## Exceptions

| Exception | When |
| :-- | :-- |
| `CQRSError` | The base of all of them |
| `HandlerNotFoundError` | No handler registered for that type |
| `DuplicateHandlerError` | Two registrations for the same type without `allow_override` |
| `DeserializationError` | The queue payload does not rebuild the message |

---

## Serialization

```python
serializer = cqrs.PydanticSerializer()
```

This is what turns a message into the `dict` that travels through the queue and rebuilds it on
the other side. It implements `AbstractSerializer`, so it can be replaced; what you should **not**
do is mix two serializers between the web process and the worker, because the message is
serialized with one and deserialized with the other.

Type resolution: the payload carries the message's fully qualified name and the deserializer
imports it. That is why the decorators reject classes defined inside a function — see
**[Queues and workers](./queues-and-workers.md)**.

---

## The envelope: metadata that travels with the message

When a command crosses into the queue there is context from the web process that the worker needs
and that is not part of the payload: who asked for it, with which request ID, in which tenant.

```python
import hexcore.cqrs as cqrs

cqrs.register_envelope_metadata_provider("tenant", lambda: current_tenant())
cqrs.register_envelope_restorer("tenant", MyRestorer())
```

| Function | Purpose |
| :-- | :-- |
| `register_envelope_metadata_provider(key, provider)` | What to attach when enqueuing |
| `register_envelope_restorer(key, restorer)` | How to reinstall it in the worker |
| `collect_envelope_metadata()` | The current envelope |
| `restored_envelope_scope(...)` | The context manager that installs it |
| `message_correlation_id()` | The `cid` of the message in flight |
| `registered_envelope_keys()` | Diagnostics |
| `unregister_envelope_key(key)` / `clear_envelope_registry()` | Cleanup, mostly in tests |

Darwin uses this so the authenticated actor crosses the queue in a **signed envelope bound to the
message** (`cid`, `mt`): without that binding, a grant captured from a "delete account" could be
re-attached to a "transfer funds". See **[Darwin](./darwin/)**.

---

## Migrating from `UseCase`

If your application is already written with `UseCase`, migration is incremental:

```python
import hexcore.cqrs as cqrs

registry.register_command_handler(
    CreateUserCommand,
    cqrs.UseCaseCommandHandler(CreateUserUseCase(uow)),
)
```

When you are ready, the use case becomes a plain handler:

```python
from hexcore.domain.cqrs import AbstractCommandHandler


class CreateUserHandler(AbstractCommandHandler[CreateUserCommand, UserDTO]):
    def __init__(self, uow) -> None:
        self.uow = uow

    async def handle(self, command: CreateUserCommand) -> UserDTO:
        ...
```

`UseCase` is not deprecated: it is still the right abstraction for orchestrating without going
through a bus. What the adapter buys you is not having to rewrite everything at once.

---

## Next

→ **[Queues and workers](./queues-and-workers.md)** — how this runs in the background.
