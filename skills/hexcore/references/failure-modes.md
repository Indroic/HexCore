# Failure modes, indexed by symptom

Almost everything here shares one property: **it does not raise where it is caused.** Either
nothing raises at all, or the exception surfaces far from the mistake. That is why this file
is indexed by what you can *see*, not by what is wrong.

Run `python scripts/hexcore_audit.py src/` to find most of these statically.

---

## Nothing raises at all

### `alembic --autogenerate` emits `op.drop_table` on a table with data {#op-drop-table}

**The framework's worst failure mode.** The migration is generated cleanly; the damage appears
when it is applied.

**Cause.** A table that exists in the database and is missing from `Base.metadata`. Alembic
compares metadata against the database and concludes the table was deleted.

**Fix.** `env.py` needs all three:

```python
ensure_framework_models_loaded()                    # hexcore_cron_jobs, the event store
ensure_identity_schema_loaded(plugins=DARWIN_PLUGINS)   # Darwin and its plugins
import_all_models(models)                           # yours, recursively
```

With Darwin the table that gets dropped is the **entire credential store**. Put
`hexcore identity check-schema` in CI — it exits 1 when an identity table is missing.

**Always read a generated migration before applying it.**

### The Unit of Work publishes no events, and logs nothing

**Cause, before 9.0.** `commit()` committed first and *then* called `collect_domain_events()`,
which walks `session.new | dirty | deleted` — all empty after a commit. So it collected
nothing and published nothing.

**Fix.** Upgrade to 9.0, where collection happens before the commit and publication after it.
Expect handlers that were subscribed and never ran to **start running**. That is the fix, and
it can look like a regression if nobody knows the history.

### A handler subscribed to a base class never runs

**Cause, before 9.0.** Dispatch was `self._handlers.get(type(event))` — exact class. The
handler stayed registered and was silently never invoked.

**Fix.** 9.0 dispatches by hierarchy. After upgrading, review every subscription: handlers
that were dead may come alive, and one registered at two levels now runs once.

### `RabbitMQEventBus` messages vanish after upgrading to 9.0

**Cause.** `DomainEvent.event_name` changed from `replace` to `removesuffix`, which changes
the routing keys. The publisher routes with one key while the consumer's binding uses the
other, and **AMQP discards what does not match with no error at all**.

**Fix.** A deployment with messages in flight needs **both keys bound** for one version.

### Every event is delivered twice {#double-publish}

**Cause.** An `EventStoreRelay` is running *and* the Unit of Work is still publishing after
commit. Both push the same event to the bus.

**Fix.** `publish_after_commit=False` on the UoW.
`EventStoreContainer.publish_after_commit_recomendado` tells you which it should be, so the
rule does not depend on anyone remembering it. With non-idempotent handlers this does real
damage before anyone notices.

### CORS lets any origin read authenticated responses {#cors}

**Cause.** `allow_origins=["*"]` with `allow_credentials=True`. The browser cannot receive `*`
with credentials, so Starlette **reflects the attacker's `Origin`** and adds
`Access-Control-Allow-Credentials: true`. Any origin then reads authenticated responses with
the victim's session cookie — no XSS required.

Before 7.0 this was the *default*: the derivation lived in the class body where `debug` is
always `True`, so the value was always `["*"]` even with `ServerConfig(debug=False)`.

**Fix.** Declare real origins. In 9.x, declaring both explicitly stops the app from starting;
declaring only `"*"` lowers `allow_credentials` to `False` with a warning — at which point
cookie auth silently stops working, which is the other half of this symptom.

### The cron does not fire, or fires twice

**Cause, before 5.0.** `DynamicScheduler` compared against the current minute: with `tick=60s`
accumulated drift skipped a whole minute, with `tick<60s` it duplicated.

**Fix.** 9.x decides by catch-up — any occurrence between `last_run_at` and now. If your
repository does not implement `update_last_run`, implement it: **that is what deduplicates**.

Across replicas, duplicates mean no `lock_provider`. The scheduler emits a `RuntimeWarning` on
a sub-minute tick without one.

### The entire cron switches off during a Redis blip

**Cause, before 5.0.** Lock providers returned `False` on *any* error, with a log line
indistinguishable from "another replica holds the lock".

**Fix.** 9.x distinguishes them: "I could not decide" logs `critical`, "another replica holds
it" logs `debug`. Choose the policy explicitly — `on_error="skip"` (default, the cron stalls)
or `on_error="raise"` (the supervisor sees it).

### `PostgresLockProvider` grows the database forever

**Cause, before 5.0.** It never purged: ~10,000 rows a day, permanently.

**Fix.** 9.x purges on `setup()` and every 100 acquisitions. Call `await
lock_provider.setup()`.

---

## The queue and the worker

### The queue grows without bound and the handler never runs

**Cause, in 2.x.** The worker **re-enqueued** `@background_command`s instead of executing
them. A silent infinite loop.

**Fix.** 9.x Smart Routing: the `CQRSConsumer` marks the message as "came from the worker", so
the bus executes it locally. **Share one bus between web and worker** — that is the whole
mechanism. Building separate buses reintroduces the bug.

### The message enqueues, and the worker cannot find it {#worker-cannot-find-the-message}

**Cause.** The message class or task is defined **inside a function**, so its `__qualname__`
carries `<locals>` and the FQN the payload travels with cannot be imported. Or the worker
simply never imports the module that defines it.

**Fix.** Move the definition to module level. Since 5.0 all three decorators reject this **at
decoration time**, which is strictly better: previously the message enqueued fine and failed
in the worker, where it can no longer be recovered.

### `RuntimeError` on the first dispatch of a `@background_command` {#runtimeerror-on-first-dispatch}

**Cause.** The bus was built without an `enqueuer`.

**Fix.** Build with `cqrs.CQRSFactory(config, registry, enqueuer=enqueuer)`, which **fails at
construction** instead of on the first dispatch with a user's request already in flight.

### The handler runs 12 times {#handler-runs-12-times}

**Cause.** `RetryMiddleware` *and* the queue's retry. 3 × 3 is up to 12 executions, not 6.
With a non-idempotent handler, that is 12 charges.

**Fix.** Pick one. The queue's for `@background_command` — it persists the attempt and
survives a worker restart. The middleware for synchronous commands, where there is no queue to
retry. The middleware warns when it detects both.

### The event goes nowhere

**Cause.** `enqueue_event` on a task-queue adapter. A task queue cannot fan out to "every
subscriber" — it does not know them. In 2.x both adapters were a `pass` and the event was lost
without a trace.

**Fix.** 9.x raises `NotImplementedError`. Use `@background_handler` to run one specific
subscriber in the background, or `RedisEventBus` / `PostgresEventBus` / `RabbitMQEventBus` for
real fan-out.

### A Redis event is lost when a handler fails

**Cause, before 9.0.** `RedisEventBus` acknowledged inside the `try`, so the `xack` ran even
when the handler raised and the message left the PEL.

**Fix.** 9.0 acknowledges after the handlers; a failed message stays pending and can be
claimed with `XAUTOCLAIM`.

### `Event loop is closed`, in a task unrelated to the one that broke {#event-loop-is-closed}

**Cause.** `asyncio.run()` per task in a synchronous Celery worker. It closes the loop and
leaves the `AsyncEngine` pool bound to a dead one.

**Fix.** `run_in_worker_loop(coro)` from the Celery adapter, which keeps a persistent
per-process loop. `shutdown_worker_loop(timeout=5.0)` on worker shutdown.

### The whole process exits with `WorkerDied`

**Not a bug.** If any loop dies, the runner cancels the rest so the orchestrator restarts
everything. Running with a dead loop — enqueuing without consuming, or the reverse — is worse
than crashing: the queue grows, nobody notices, and the process keeps reporting itself alive.

Run the worker as a **separate process** from the API, or this restarts your API too.

---

## SQLAlchemy and Beanie

### `MissingGreenlet` / `DetachedInstanceError` after `commit()` {#missinggreenlet}

**Cause.** `expire_on_commit=True` (SQLAlchemy's default). Attributes expire on commit and the
next access triggers a lazy load on a closed session. The number one bug of async SQLAlchemy.

**Fix.** Use `sql.init_engine()` / `sql.get_session_factory()`, which pass
`expire_on_commit=False`. If you build the sessionmaker yourself, pass it yourself.

### `CollectionWasNotInitialized` on a document's first query {#collectionwasnotinitialized}

**Cause.** `init_beanie` **does not accumulate**: a second call against the same database
replaces the first call's registry. A `Document` that the surviving call never saw fails here.

**Fix.** One call, with every document — yours, identity's, the plugins'.

### `AttributeError` during `commit()`, after the commit succeeded

**Cause.** A framework table inheriting `BaseModel[T]`. `collect_domain_entities()` walks the
session, filters on `isinstance(model, BaseModel)` and calls `get_domain_entity()` — on a row
with no domain entity behind it, that is an `AttributeError`.

**Fix.** A framework table inherits `Base`, not `BaseModel[T]`.

### `Base.metadata` is suddenly a column {#column-named-metadata}

**Cause.** A column literally named `metadata` shadows it.

**Fix.** Rename it. Darwin uses `audit_metadata`.

### Alembic cannot generate a `DROP CONSTRAINT`

**Cause.** An anonymous constraint: the database named it something the `MetaData` does not
know, so the downgrade is left to hand-editing.

**Fix.** Use HexCore's `Base`, whose metadata carries an explicit naming convention. Decide
this **before the first migration** — changing it later renames existing constraints.

### A Beanie document behaves oddly, sharing a collection

**Cause.** Subclassing `BaseDocument`, which sets `is_root = True` (single-collection
inheritance) and `use_cache = True`.

**Fix.** Subclass `Document` directly with your own `Settings`.

---

## Startup, config and tests

### The Unit of Work will not build

**Cause.** `repository_discovery_paths` is empty. Discovery is explicit since v2 and the UoW
fails with a diagnostic error rather than guessing by folder convention — which tied the
framework to one layout and failed silently on any other.

**Fix.** List your repository modules in `ServerConfig`.

### `ServerConfig(...)` ignores a keyword

**Cause.** Pydantic silently discards unknown keywords. Anyone migrating with
`event_dispatcher=` would keep the default bus and see it surface much later as "my events
never arrive".

**Fix.** 9.x has a validator for the removed names that **raises with remediation**. If you
hit it, do what it says. For genuinely unknown keywords, check the spelling with
`--find`.

### Two tests see each other's event subscriptions

**Cause, before 9.0.** `ServerConfig.event_bus`'s default was evaluated at class definition,
so every `ServerConfig()` in the process shared one bus and one handler dictionary.

**Fix.** 9.0 uses `default_factory`. Also call `LazyConfig.clear_cache()` between cases that
need different configuration, and `hx.reset_cqrs()` to clear the CQRS container.

### The first endpoint touching the database fails, pointing at `init_engine`

**Cause.** `TestClient(app)` without the context manager. The lifespan never ran, so the
engine was never initialised.

**Fix.** `with TestClient(app) as client:`. The `with` is not optional.

### A test's dependency override leaks into an unrelated test

**Cause.** `app.dependency_overrides` is an instance dict, and an override that is not cleaned
up survives every test reusing the app.

**Fix.** `override_cqrs(app, ...)` as a context manager — it saves each previous value and
restores even if the block raises.

### Every connection to `:memory:` sees a different database

**Cause.** SQLite opens a fresh in-memory database per connection, so the test that creates
the table is not the one that queries it.

**Fix.** `StaticPool`, which the `sqlite_engine` fixture already uses.

### `install_request_id_logging()` does nothing

**Cause.** It instruments the handlers that **already exist**. Called before anyone configured
logging, there are none.

**Fix.** `logging.basicConfig(...)` first. It warns with a `RuntimeWarning` rather than staying
silent.

### The rate limit is bypassed by changing a header

**Cause.** Using a forwarded-IP key without `trusted_proxies`. `X-Forwarded-For` is written by
the client.

**Fix.** `forwarded_ip_key(trusted_proxies={...}, trust_hops=1)`. The list is mandatory for
this reason.

---

## Event sourcing

### A projection never sees an event that is definitely in the store

**Cause.** `global_position` is monotonic but **not contiguous**. The sequence is taken at
insert time, not commit time, so the transaction that reserved position 10 can commit after
the one that reserved 11 — and a projector with `WHERE global_position > checkpoint` that
already passed 11 will never see 10.

**Fix.** `safety_window=N` on `Projector` / `EventStoreRelay` re-reads N positions backwards
on each start. Or `ordering="serialized"` on `SqlAlchemyEventStore`, which takes a
`pg_advisory_xact_lock` and costs serialising every write (PostgreSQL only).

### An event is processed twice after a crash

**Not a bug.** Delivery is **at-least-once**. The relay saves the checkpoint *after*
publishing, so a crash in between republishes the batch. The reverse order would lose events
forever.

**Consequence: handlers and projections must be idempotent.** `event_id` is the key.

### A Mongo stream has gaps

**Cause.** The `append` of N events is **not atomic** without a replica set. A failure halfway
leaves the stream truncated; the unique index prevents duplicates, not gaps. And the append
cannot share a transaction with the business change, so event-log mode loses its guarantee.

**Fix.** On Mongo, use pure event sourcing — let the append *be* the change. With a replica
set, pass the Mongo transaction's session to the store.

### A Redis stream disappears

**Cause.** `maxmemory-policy allkeys-lru` can evict whole streams. The adapter never passes
`MAXLEN` — trimming an event log is destroying it — but it cannot protect itself from a server
policy. And without AOF, minutes of events are lost on restart.

**Fix.** Do not use Redis as the primary store.

### `rebuild()` wiped a read model you did not mean to touch

**Not a bug — it is destructive by design**, and it calls `reset()` on **every** registered
projection. That is also why projections are registered explicitly with no discovery: one
discovered by accident takes part in a rebuild.
