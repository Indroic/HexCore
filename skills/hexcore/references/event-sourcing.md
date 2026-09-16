# Event Sourcing and the event store

> New in **9.0**.

```python
import hexcore.eventsourcing as es
```

A lazy facade: the SQL, Mongo and Redis adapters live side by side, each requiring its extra
only at the moment you ask for it.

Before 9.0 HexCore had domain events and a bus, and nothing else: an event nobody consumed was
gone. That left three things out of reach — you could not reconstruct the past, a new read
model was born empty, and delivery was not reliable (publishing after committing has a window
where the change exists and the fact reached nobody).

| Piece | What it is |
| :-- | :-- |
| `StoredEvent` | A persisted event, with its stream, version and global position |
| `AbstractEventStore` | `append`, `read_stream`, `read_all`, `stream_version` |
| `AggregateRoot` | An aggregate whose state is the fold of its stream |
| `AbstractSnapshotStore` | Summarised state, so long streams are not replayed in full |
| `AbstractProjection` | A read model fed event by event |
| `AbstractCheckpointStore` | How far each subscription has read |
| `EventSourcedRepository` | Loading and saving aggregates |
| `Projector` | Walks the global order and feeds projections |
| `EventStoreRelay` | Publishes to the bus what is already in the store |

---

## Two modes, and confusing them is where this breaks

### Event log — state stays in its tables

The Unit of Work writes the events of your classic entities into the store, **in the same
transaction as the change**.

```python
from hexcore.eventsourcing import SqlAlchemyEventStore
from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork

store = SqlAlchemyEventStore(session=session)
uow = SqlAlchemyUnitOfWork(session=session, event_store=store)

await uow.commit()   # the event and the change go in together, or neither does
```

Events are written with `EXPECTED_VERSION_ANY`: no aggregate read a version and wants to
defend it. **Reconstructing state from these streams does not work** — the events describe
what happened, but the state is not derived from them alone.

### Event sourcing — the stream *is* the state

```python
from hexcore.eventsourcing import AggregateRoot, EventSourcedRepository, when


class Order(AggregateRoot):
    customer: str
    total: int                      # no default: an order without a total does not exist

    @when(OrderCreated)
    def _create(self, event: OrderCreated) -> None:
        self.customer = event.customer
        self.total = 0

    @when(OrderPaid)
    def _pay(self, event: OrderPaid) -> None:
        self.total += event.amount

    def pay(self, amount: int) -> None:          # the rules live here
        if self.cancelled:
            raise ValueError("a cancelled order cannot be paid")
        self.raise_event(OrderPaid(amount=amount))


repo = EventSourcedRepository(Order, store=store)

order = await repo.get(order_id)
order.pay(4200)
await repo.save(order)
```

Both modes coexist in the same application and the same store.

---

## The aggregate

**The mutator only mutates.** It does not validate rules, it does not raise and it does not
emit events: what reaches it already happened, and during a replay you are reconstructing
history, not deciding. The rules go in the business method that calls `raise_event()`.

`apply()` has a double life — `raise_event()` uses it and the replay uses it — which is what
guarantees that rebuilding an aggregate yields exactly the same state as building it live: it
is literally the same code.

### It is `BaseEntity`'s sibling, not its child

Both inherit `EventRecorder`, so a handler calling `pull_domain_events()` works against
either. Neither inherits from the other, for three reasons:

1. **The Unit of Work trap.** `collect_domain_entities()` walks the session, recognises domain
   entities with an `isinstance` and drains their events. An aggregate that were also a
   `BaseEntity` attached to an ORM model would publish **twice**.
2. **`validate_assignment`.** `BaseEntity` revalidates the whole model on every assignment. A
   replay of five thousand events with three assignments each is fifteen thousand full
   validations to reach a result that was already valid.
3. **Its fields are either redundant or lying.** `is_active` in an aggregate is an event
   (`OrderCancelled`), not a flag; `updated_at` is overwritten by the ORM's `onupdate`.

`version` is the **committed** one, not the current one. It advances only in
`mark_events_as_committed()`, which the repository calls if and only if the `append`
succeeded — which is what makes a `ConcurrencyError` retryable: the aggregate keeps its
pending events and its version, so it can be reloaded and reapplied.

---

## Optimistic concurrency

```python
from hexcore.eventsourcing import ConcurrencyError

try:
    await repo.save(order)
except ConcurrencyError:
    order = await repo.get(order_id)   # another writer advanced the stream
    order.pay(4200)                    # the business decision is reapplied
    await repo.save(order)
```

**The real guarantee is not the version check, it is the uniqueness constraint.** Every
adapter checks the version before writing, and that check is a TOCTOU — another writer fits
between the read and the write.

| Backend | What actually prevents two events at the same version |
| :-- | :-- |
| SQL | `UNIQUE(stream_id, version)`; the `IntegrityError` becomes `ConcurrencyError` |
| Mongo | Unique index on `(stream_id, version)`; `DuplicateKeyError` → `ConcurrencyError` |
| Redis | `XADD` with an explicit `<version>-0` id: Redis rejects any id ≤ the stream's last |
| Memory | An `asyncio.Lock` around the append |

`EXPECTED_VERSION_NO_STREAM` (0) requires that the stream not exist, which is what makes
creation safe: two processes creating the same id at once, one wins.

---

## Projections {#projections}

```python
from hexcore.eventsourcing import AbstractProjection, Projector


class OrderSummary(AbstractProjection):
    name = "order_summary"
    handles = (OrderEvent,)      # reaches every subclass

    async def apply(self, event, stored) -> None:
        ...                      # must be idempotent

    async def reset(self) -> None:
        ...                      # required if it writes anything


projector = Projector(
    store=store,
    checkpoints=checkpoints,
    projections=[OrderSummary()],
    safety_window=50,
)
await projector.catch_up()
await projector.rebuild()        # destructive: calls reset() and reprocesses from event 1
```

`apply` receives the `StoredEvent` **in addition to** the event, because a domain event does
not know its own `stream_id` or position, and it is frozen so nothing can be attached to it.

`rebuild()` is what makes a read-model migration unnecessary: if the shape changes, rebuild.
And for exactly that reason **it is destructive** — it calls `reset()` on every projection.

Projections are registered **explicitly**, with no discovery. Same decision as
`repository_discovery_paths`, and here the consequence would be worse: a projection discovered
by accident takes part in `rebuild()`, which deletes.

The `handles` filter works on the `event_type` column, resolving the FQN to its class once per
type — deserializing an event no projection wants is pure waste, and over years of history
that decides whether a rebuild takes minutes or hours. It does **not** walk `__subclasses__()`,
which would make the set of events a projection receives depend on the process's import order.

---

## The outbox {#outbox}

- **Inbound**: the UoW writes the event in the same transaction as the change. If the commit
  fails, neither the change nor the fact remains.
- **Outbound**: `EventStoreRelay` reads the global order and publishes to the bus, saving the
  checkpoint **after** publishing.

**With SQL, the event store table *is* the outbox.** No second table is needed, and that is
half the reason `StoredEvent.payload` stores the complete `serialize_envelope()` envelope:
republishing is rebuilding the envelope and publishing it, with the original actor and
`request_id` intact.

```python
from hexcore.eventsourcing import EventStoreRelay

relay = EventStoreRelay(store=store, bus=bus, checkpoints=checkpoints)
await relay.run_forever()
```

> ⚠️ **With a relay running, the UoW must not publish.** Pass `publish_after_commit=False`, or
> every event goes out twice — silently — and with non-idempotent handlers that does real
> damage. `EventStoreContainer.publish_after_commit_recomendado` says which it should be, so
> the rule does not depend on anyone remembering it.

---

## The four backends

| Backend | Extra | What it is for |
| :-- | :-- | :-- |
| `InMemoryEventStore` | none | Tests, development, a single process that need not survive a restart. **Does not persist.** |
| `SqlAlchemyEventStore` | `[sql]` | **The recommended primary store.** Transactional, shares the session with the business change. |
| `BeanieEventStore` | `[mongo]` | Applications already on Mongo. See the limitations below. |
| `RedisEventStore` | `[redis]` | A fast short-lived log, or a hot buffer for projections. **Not** a primary store. |

The in-memory ones are real adapters, not test doubles: they honour the full contract,
optimistic concurrency included. The contract suite runs against all of them — an event store
with two implementations that behave differently is not a port, it is two stores sharing a
signature.

```python
from hexcore.config import ServerConfig
from hexcore.eventsourcing import EventStoreConfig, ProjectionsConfig, configure_event_store

config = ServerConfig(
    event_store=EventStoreConfig(
        backend="hexcore.eventsourcing.SqlAlchemyEventStore",
        projections=ProjectionsConfig(
            projections=["app.projections.OrderSummary"],
            safety_window=50,
        ),
        relay_enabled=True,
    ),
)

container = configure_event_store()          # reads ServerConfig.event_store
store = container.store()
```

`EventStoreConfig` is a field of `ServerConfig`, a sibling of `cqrs` and `darwin` — **not**
part of `CQRSConfig`. That one is frozen and models three buses; putting the event store there
would make `CQRSConfig(enabled=False)` switch off the store of record.

`configure_event_store()` takes the serializer from the CQRS container when there is one.
Sharing it is not an optimisation: if they diverge, an event written by the store and one
published by the bus do not share a format, and the day you replay history through the bus's
consumer the payload cannot be reconstructed.

### The SQL table

Three rules, each broken by a single line:

1. **The models do not inherit `BaseModel[T]`.** `collect_domain_entities()` would ask them
   for a domain entity they do not have, *during* `commit()`.
2. **The models module is listed in `ensure_framework_models_loaded()`.** Without it,
   `alembic revision --autogenerate` emits `op.drop_table` against the event store.
3. **No column is called `metadata`** — it would shadow `Base.metadata`.

```python
from hexcore.eventsourcing import create_eventstore_tables

await create_eventstore_tables()     # dev shortcut; use Alembic in production
```

---

## What this event store does **not** guarantee

Required reading before designing on top of it.

### Delivery is at-least-once, not exactly-once

Publishing to a broker and recording progress in the database are two systems; making them
atomic needs a distributed transaction neither supports here. The relay saves the checkpoint
**after** publishing, so a crash in between republishes the batch. The reverse order would
lose events forever, because on restart the checkpoint would claim they were delivered.

**Your handlers and projections must be idempotent.** `event_id` is the key to deduplicate on.

### `global_position` is monotonic but **not contiguous**

In SQL the position comes from a sequence, and a sequence is taken **at insert time, not at
commit time**: the transaction that reserved position 10 can commit after the one that
reserved 11. A reader already past 11 — a projector with `WHERE global_position > checkpoint`
— would never see 10. Mongo has the same shape of problem: the counter is incremented with
`$inc` before the insert, and a failed write burns the position it reserved.

Two tools, neither free:

- **`safety_window`** on `Projector` and `EventStoreRelay`: re-reads N positions backwards on
  each start, giving lagging writes a chance to appear. It costs reprocessing, which
  idempotency already covers.
- **`ordering="serialized"`** on `SqlAlchemyEventStore`: takes a `pg_advisory_xact_lock` before
  inserting, so writes are ordered the same as commits. It costs serialising **every** write.
  PostgreSQL only — on any other dialect it fails rather than accept the option and not honour
  it.

### Mongo: no atomicity without a replica set

The `append` of N events is **not atomic** — a failure halfway leaves the stream truncated,
with valid but incomplete versions; the unique index prevents duplicates, not gaps. And it
**cannot share a transaction with the business change**, so the event-log mode above loses its
main guarantee. **On Mongo, the sane path is pure event sourcing**: let the `append` *be* the
change. With a replica set, pass the Mongo transaction's session to the store.

### Redis: it is memory

Requires at minimum AOF with `appendfsync everysec`, and even then the last second can be
lost. With RDB alone, minutes are lost. `maxmemory-policy allkeys-lru` can evict whole
streams: the adapter never passes `MAXLEN` — trimming an event log is destroying it — but it
cannot protect itself from a server policy.

### SQLite: the driver's savepoints

The stdlib SQLite driver emits an implicit `COMMIT` before a `SAVEPOINT`, so the write is
committed and a later `rollback()` does not undo it. `SqlAlchemyEventStore` detects the
dialect and skips the savepoint there; the price is that an `IntegrityError` with a borrowed
session leaves the caller's transaction aborted. There is no real concurrent writing in SQLite
anyway.

### A snapshot is not a source of truth

It is a read optimisation. A store without snapshots yields the same results, slower — which
is why a failure to save one **is logged and not propagated**: losing a write because a cache
could not be filled would be absurd. A corrupt snapshot is recovered by deleting it, because
the store keeps history instead of overwriting.
