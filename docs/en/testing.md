# Testing

`hexcore.testing` ships the doubles and helpers you need to test a hexagonal app without standing
up Redis, Postgres or a broker. It requires no extras.

```python
from hexcore.testing import (
    FakeLockProvider,
    FakeRepository,
    FakeUnitOfWork,
    InMemoryTaskEnqueuer,
    RecordedEnqueue,
    build_test_buses,
    override_cqrs,
)
```

---

## Test buses

```python
from hexcore.testing import build_test_buses

buses = build_test_buses()
buses.registry.register_command_handler(SendEmailCommand, SendEmailHandler())

await buses.command_bus.dispatch(SendEmailCommand(user_id="1", template="welcome"))

assert buses.enqueuer.command_names == ["SendEmailCommand"]
assert buses.enqueuer.commands[0].queue == "high_priority"
```

`build_test_buses()` builds all three buses **with** an enqueuer and a serializer. That is the
most common mistake when testing CQRS: building them without those, and having the first
`@background_command` blow up with a `RuntimeError` that says nothing about the test that caused
it.

What it returns (`TestBuses`) is a `NamedTuple` with `registry`, `command_bus`, `query_bus`,
`event_bus`, `enqueuer` and `serializer`. You can pass your own registry or enqueuer:
`build_test_buses(my_registry, enqueuer=my_enqueuer)`.

### Testing the other half: that the worker executes it

The same bus enqueues outside the worker and executes inside it. To test both halves:

```python
import hexcore.cqrs as cqrs

consumer = cqrs.CQRSConsumer(buses.command_bus, buses.event_bus)

await buses.command_bus.dispatch(SendEmailCommand(user_id="1", template="welcome"))
assert handled == []                         # it has not run yet: it was enqueued

await consumer.process_command(buses.enqueuer.commands[0].payload)
assert handled == ["1"]                      # the consumer executed it
```

This is the Smart Routing contract, and it is worth covering in your own suite: it is the one that
breaks if somebody builds separate buses for the web process and the worker.

---

## `InMemoryTaskEnqueuer`

Implements `ITaskEnqueuer`, storing everything in lists:

| Attribute | What it holds |
| :-- | :-- |
| `commands` | `list[RecordedEnqueue]` — the enqueued commands |
| `events` | The events |
| `handlers` | The background handlers |
| `tasks` | The tasks |
| `command_names` / `task_names` / `handler_names` | Shortcuts: names only |
| `clear()` | Empties everything, to reuse the double between cases |

Each `RecordedEnqueue` carries `kind`, `name`, `payload` and `queue`, so you can assert on the
queue as well as on the name:

```python
assert enqueuer.tasks[0].name.endswith("clean_old_records_task")
assert enqueuer.tasks[0].queue == "maintenance"
```

To test the error path — what your code does when the broker rejects the message — the double can
fail on demand:

```python
enqueuer = InMemoryTaskEnqueuer(fail_on={"SendEmailCommand"})
```

---

## `override_cqrs`

```python
from hexcore.testing import override_cqrs

with override_cqrs(app, command_bus=buses.command_bus):
    response = client.post("/tickets", json={"title": "x"})
```

It overrides the providers (`provide_command_bus`, `provide_query_bus`, `provide_event_bus`,
`provide_registry`) in `app.dependency_overrides`.

It saves each override's previous value, so **it nests and it restores even if the block
raises**. `app.dependency_overrides` is an instance dict: an override that is not cleaned up leaks
into every test that reuses the app, and the one that fails is an unrelated one.

---

## `FakeLockProvider`

```python
from hexcore.testing import FakeLockProvider

FakeLockProvider()                  # always grants
FakeLockProvider(grant=False)       # always denies
FakeLockProvider(shared=True)       # a real in-memory lock, shared
```

`shared=True` is the interesting mode: it behaves like a real lock across distinct provider
instances, so it lets you test **two concurrent schedulers** — the case the lock exists for —
without standing up Redis.

`raise_on_acquire=RuntimeError("redis down")` covers the third branch, which is the one the real
providers' `on_error` decides: what happens when the lock does not answer.

---

## Fake repositories and UoW

```python
from hexcore.testing import FakeRepository, FakeUnitOfWork

repo = FakeRepository(entities=[ticket_a, ticket_b])
uow = FakeUnitOfWork({"tickets": repo})

async with uow:
    ticket = await uow.tickets.get_by_id(ticket_a.id)
    await uow.commit()

assert uow.commits == 1
assert uow.rollbacks == 0
```

`FakeUnitOfWork` **counts** `commit()` and `rollback()` calls — it does not just flag them with a
boolean — and accumulates domain events, so you can assert on what was published without wiring a
bus. Counting is what makes a double commit visible, which is the bug
[`TransactionMiddleware` causes](./cqrs.md) on a handler that already manages its own
transaction.

`add_repository("tickets", repo)` is the fluent alternative to the dict, and returns the UoW
itself.

`FakeRepository` also records how many times each method was called (`count_calls("save")`) and
knows how to `snapshot()` / `restore()`, which is how you test a rollback without a database:

```python
before = repo.snapshot()
...
repo.restore(before)
```

---

## Pytest fixtures

Enable them from your `conftest.py`:

```python
pytest_plugins = ["hexcore.testing.fixtures"]
```

| Fixture | Gives you |
| :-- | :-- |
| `anyio_backend` | `"asyncio"`, for tests marked `@pytest.mark.anyio` |
| `task_enqueuer` | An `InMemoryTaskEnqueuer` |
| `lock_provider` | A `FakeLockProvider` |
| `cqrs_buses` | The `TestBuses`, already wired to the enqueuer above |
| `sqlite_engine` | An in-memory SQLite engine, with `StaticPool` |
| `sqlite_session` | An `AsyncSession` on that engine |
| `uow` | A UoW on that session |

`sqlite_engine` uses `StaticPool` on purpose: without it, each connection to `:memory:` opens a
**different** database, and the test that creates the table is not the one that queries it.

---

## Testing the HTTP layer

```python
from fastapi.testclient import TestClient

with TestClient(app) as client:          # the `with` is what runs the lifespan
    assert client.get("/health").status_code == 200
```

⚠️ **The `with` is not optional.** `TestClient(app)` without the context manager does not run the
lifespan, so the engine is never initialized and the first endpoint touching the database fails
with an error pointing at `init_engine` rather than at the test.

To isolate the database between tests, a `SqlEngineStep` with in-memory SQLite:

```python
from hexcore.fastapi import build_lifespan, create_app, SqlEngineStep

app = create_app(lifespan=build_lifespan(SqlEngineStep("sqlite+aiosqlite:///:memory:")))
```

---

## Testing identity

Darwin ships its own doubles in `hexcore.darwin.testing`:

```python
from hexcore.darwin.testing import (
    FakeUserRepository,
    FakeSessionRepository,
    PlainTextHasher,
    authenticated_context,
    configure_test_identity,
    create_test_user,
)
```

| Helper | What it does |
| :-- | :-- |
| `configure_test_identity(config=None, *, seed_users=(), clock=None, now=None, plugins=None, **overrides)` | Wires the whole container with fakes and a test key |
| `create_test_user(container, email, password, *, verified=True, scopes=())` | Registers a user **in** that container |
| `make_user(email, *, verified=True, scopes=())` | Just the entity, unpersisted |
| `authenticated_context(user, *, scopes=(), roles=(), transport="bearer")` | An authenticated `AuthContext`, to use as an override |
| `impersonated_context(actor, subject, *, reason="test")` | A valid impersonated context |
| `system_context(name="test:process")` | The system context, for jobs and migrations |
| `PlainTextHasher` | Replaces Argon2: real hashing makes an auth suite take minutes |
| `RecordingAuditSink` | Keeps the `AuditRecord`s so you can assert on them |
| `FixedClock` | A frozen clock, to test expirations without sleeping |

`FixedClock` is what lets you test a token's TTL without a `sleep`: you move the clock forward and
verify the token stopped being valid. Pass it with `configure_test_identity(now=...)`.

There are dedicated fixtures too, with the same mechanism:

```python
pytest_plugins = ["hexcore.darwin.testing.fixtures"]
```

They provide `identity_clock`, `identity_audit`, `identity_users` and `identity_container` — the
last one already wired to the other three and cleaned up after each test, which is what stops one
case's identity container from leaking into the next.

---

## Rules of HexCore's own suite

If you contribute to the repository:

```sh
uv sync --extra all --group dev
uv run python -m pytest -q
```

- **CI fails if any test is skipped.** A skip means an extra was missing from the environment, and
  reporting green without having run half the suite is worse than failing.
- Three markers stay out of the normal run because they are slow or need services:
  `pytest -m packaging` (actually builds the wheel), `pytest -m typing` (invokes pyright),
  `pytest -m mongo` (needs a MongoDB at `HEXCORE_TEST_MONGO_URI`).
- `mongo` is **deselected**, not skipped: a skip would break the zero-skips gate.

---

## Next

→ **[CLI](./cli.md)**.
