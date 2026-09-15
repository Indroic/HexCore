# Quickstart

Two files. The first is the app; the second is the worker. Everything here has a default that
works, so anything you do not need you can simply delete.

---

## The complete app

```python
# main.py
from hexcore.fastapi import build_lifespan, create_app, SqlEngineStep

app = create_app(
    lifespan=build_lifespan(SqlEngineStep()),
    routers=[users_router, tickets_router],
)
```

```sh
uvicorn main:app --reload
```

That already gives you, without asking:

- `title` and `version` from your `ServerConfig`.
- CORS from `config.allow_origins`.
- The `X-Request-ID` middleware, which reuses the incoming header when there is one.
- The timing middleware (`X-Response-Time`).
- Domain exceptions mapped to HTTP status codes.
- `GET /health` (liveness) and `GET /health/ready` (readiness, with real probes against SQL,
  Redis and Mongo, concurrent and with a per-probe timeout).
- The SQL engine started on boot and disposed on shutdown, in that order.

To turn something off there is **one switch object**, not eight keyword arguments:

```python
from hexcore.fastapi import AppFeatures, create_app

app = create_app(features=AppFeatures(cors=False, timing=False))
```

→ Full detail in **[FastAPI utilities](./fastapi.md)**.

---

## The complete worker

```python
# worker.py
import asyncio

import hexcore.cqrs as cqrs
from hexcore.infrastructure.task_queues.procrastinate_adapter import (
    register_hexcore_procrastinate_tasks,
)


async def main() -> None:
    consumer = cqrs.CQRSConsumer(command_bus, event_bus)
    register_hexcore_procrastinate_tasks(procrastinate_app, consumer)

    await cqrs.run_procrastinate_worker(
        procrastinate_app,
        queues=["default", "reactive"],
        concurrency=4,
        scheduler=cqrs.DynamicScheduler(repo, enqueuer, lock_provider=lock),
        on_startup=[lambda: cqrs.seed_cron_jobs(CRON_JOBS)],
    )


if __name__ == "__main__":
    asyncio.run(main())
```

`command_bus` and `event_bus` are **the same ones** the web process uses. There is no second
wiring: the consumer marks the message as "came from the worker", so the bus executes it locally
instead of re-enqueuing it. That is Smart Routing, explained in
**[Queues and workers](./queues-and-workers.md)**.

If **any** of the loops dies — the worker's or the scheduler's — the runner cancels the rest and
the process exits with `WorkerDied`, so the orchestrator restarts the whole thing. Running with a
dead loop — enqueuing without consuming, or the reverse — is worse than crashing. `SIGTERM` and
`SIGINT` are translated into an orderly drain.

---

## An end-to-end flow

### 1. The entity

```python
from hexcore.domain.base import BaseEntity


class Ticket(BaseEntity):
    title: str
    closed: bool = False
```

### 2. The command and its handler

```python
import hexcore.cqrs as cqrs


class CreateTicket(cqrs.Command):
    title: str


class CreateTicketHandler:
    def __init__(self, uow) -> None:
        self.uow = uow

    async def handle(self, cmd: CreateTicket) -> str:
        async with self.uow:
            ticket = Ticket(title=cmd.title)
            await self.uow.tickets.save(ticket)
            await self.uow.commit()
        return str(ticket.id)
```

### 3. The registry and the bus

```python
import hexcore.cqrs as cqrs

registry = cqrs.HandlerRegistry()
registry.register_command_handler(
    CreateTicket,
    cqrs.HandlerRegistry.factory(lambda: CreateTicketHandler(build_uow())),
)
```

`HandlerRegistry.factory()` is an explicit marker. Without it, a handler that implements
`__call__` would be indistinguishable from a factory.

### 4. The endpoint

```python
from fastapi import APIRouter, Depends
from hexcore.fastapi import configure_cqrs, provide_command_bus

container = configure_cqrs(registry, enqueuer=enqueuer)   # once, at startup

router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.post("")
async def create(cmd: CreateTicket, bus=Depends(provide_command_bus)):
    return {"id": await bus.dispatch(cmd)}
```

`provide_command_bus` exists as a **function** for exactly one reason: so you can replace it with
`app.dependency_overrides` in tests.

### 5. The test

```python
from hexcore.testing import build_test_buses, override_cqrs

buses = build_test_buses()
buses.registry.register_command_handler(CreateTicket, CreateTicketHandler(FakeUoW()))

with override_cqrs(app, command_bus=buses.command_bus):
    response = client.post("/tickets", json={"title": "Something broke"})
```

→ **[Testing](./testing.md)**.

---

## Outside a request

`get_session` and `get_sql_uow` are FastAPI dependencies: they work in an endpoint and nowhere
else. For workers, tasks, cron, scripts and seeds there are **scopes**:

```python
import hexcore.sql as sql

async with sql.session_scope() as session:      # bare session
    ...

async with sql.uow_scope() as uow:              # UoW, NOT entered
    await CloseTicketUseCase(uow).execute(request)

async with sql.open_uow_scope() as uow:         # UoW already entered
    await uow.tickets.save(ticket)
    await uow.commit()
```

`session_scope` does not build the UoW **on purpose**: building it runs auto-discovery and
instantiates *every* domain repository, an absurd cost for reading one infrastructure table.

→ **[SQL layer](./sql.md)**.

---

## Project structure

The CLI generates the two layouts the framework supports well:

```sh
hexcore init my_project --template hexagonal
hexcore init my_project --template vertical-slice
```

Both create a root `config.py` with a sample `repository_discovery_paths` and leave Alembic
configured — including the `ensure_identity_schema_loaded` call in `env.py`, which is what stops
`--autogenerate` from dropping your identity tables.

→ **[CLI](./cli.md)**.

---

## Next

→ **[Configuration](./configuration.md)** — `ServerConfig`, discovery and CORS.
