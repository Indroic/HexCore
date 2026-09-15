# Versions and migration

---

## Support policy

| Series | Status | What it means |
| :-- | :-- | :-- |
| **9.x** | ✅ **Active** | The only supported one. Receives features and fixes. Adds the event store and Event Sourcing, and leaves a single event bus port. |
| **8.x** | ⛔ Deprecated | Ships Darwin. Migrating to 9.x is mechanical: the two deprecated names still resolve and warn until 10.0. |
| **7.x** | ⛔ Deprecated | Removes the pre-5.0 surface and fixes the CORS and rate-limiting defects. **No Darwin**: it shipped before the module landed on `master`. |
| **6.x** | ⛔ Deprecated | Works, but receives no fixes. Contains the CORS and rate-limiting defects fixed in 7.0, and the pre-5.0 aliases still present. |
| **5.x** | ⛔ Deprecated | Same API surface as 6.x. |
| **4.x** | ⛔ Deprecated | **Partial** application: missing the Celery event-loop fix, the facades, and the aligned documentation. |
| **3.x** | ⛔ Deprecated | **Partial** application: has the P0/P1 fixes but none of the FastAPI factories. |
| **2.x** | ⛔ Deprecated | Contains the silent bugs fixed in 5.x (below). |
| **1.x** | ⛔ Deprecated | No support of any kind. |

**Everything before 9.0 is deprecated.**

3.0.0 and 4.0.0 exist only because the work was merged in phases and each merge triggered an
automatic bump: **they are not releases meant to be used**, they are intermediate cuts of the same
migration. 5.0.0 is the first complete version. 6.0.0 is the same story — it was triggered by a
documentation PR with a `feat!:` commit — and there is **no** API break between 5.x and 6.x.

The table cannot drift from reality: `tests/test_documentation_examples.py` verifies that the
series marked active is the one in `pyproject.toml`. That test is what caught 6.0.0 shipping with
5.x still marked active.

---

## Why 2.x and earlier should not be in production

This is not a matter of taste. 2.x has defects that **raise no exception and appear in no error
log**, so a project can be affected without knowing.

| Defect in ≤ 2.x | Symptom |
| :-- | :-- |
| The worker **re-enqueued** `@background_command`s instead of executing them | Silent infinite loop: the queue grows without bound and the handler never runs |
| FQN split with `rsplit(".", 1)` | A `Command` inside a containing class, or a task as a `@staticmethod`, enqueues fine and **fails in the worker**, where the message is unrecoverable |
| `PostgresLockProvider` never purged | ~10,000 rows/day **forever** in the main database |
| `expire_on_commit` not passed | `MissingGreenlet` / `DetachedInstanceError` reading an entity after `commit()` |
| `enqueue_event` was a `pass` | The event is lost without a trace |
| `DynamicScheduler` compared against the current minute | With `tick=60s` it skips minutes; with `tick<60s` it duplicates |
| Lock providers returned `False` on any error | A Redis outage **switches off the entire cron**, with a log line indistinguishable from the normal case |
| `asyncio.run()` per task in Celery | `Event loop is closed` with a shared `AsyncEngine` |
| `HandlerRegistry` claimed thread safety with no lock | Double handler instantiation under concurrency |

---

## API removed in 7.0, and its replacement

The v1/v2 aliases were deprecated and emitting `DeprecationWarning` since 5.0 — two full majors of
notice — and **were removed in 7.0**. The replacement is mechanical: they are renames, not
behavior changes.

| Removed (was v1/v2) | Use instead |
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
| `MiddlewareConfig` | **Removed in 3.0.** It was dead code: never read |

The aliases announced they would be removed "in 6.0". 6.0.0 shipped without removing them: moving
the date was preferred over retroactively breaking people who had already upgraded trusting they
were still there.

`ServerConfig(event_dispatcher=...)` **fails with an error that says what to use** instead of
being silently ignored. See [Configuration](./configuration.md#removed-names).

### Seeing what you still have to migrate

```sh
python -m pytest -W "default::DeprecationWarning"
```

---

## Behavior changes in 5.x

The 2.x API still worked in 5.x. What changed in **behavior** — and therefore may require action —
is this.

### 1. `expire_on_commit=False` in the session factory

**What changed.** `get_session_factory()` moved to `expire_on_commit=False`.

**Why.** With SQLAlchemy's default (`True`) attributes expire on commit and the next access
triggers a lazy load on a closed session. The documentation already taught `False`, so doc and
implementation disagreed.

**Action.** None in the normal case. If you depended on refresh-after-commit, build your own
`async_sessionmaker(engine, expire_on_commit=True)`.

### 2. `get_sql_uow` no longer enters the UoW

**What changed.** The dependency yields the UoW **without** opening the transaction.

**Why.** Use cases do their own `async with self.uow:`, which nested contexts under the previous
dependency.

**Action.** If your endpoint worked on an already-open UoW, switch to `get_sql_uow_open`.

### 3. `TransactionMiddleware` out of the default, and requires `uow_factory`

**What changed.** `CQRSConfig.command_bus` no longer includes it, and `TransactionMiddleware()`
without a `uow_factory` raises `ValueError`.

**Why.** The default built the session with HexCore's *internal* session factory instead of your
application's engine, and committed after the handler — so a handler that already commits
committed twice.

**Action.** If you want it, declare it by hand with your factory. And remember it is for handlers
that do **not** manage their own transaction.

### 4. `enqueue_event` raises instead of staying silent

**What changed.** Both the Procrastinate and Celery adapters raise `NotImplementedError`.

**Why.** They were a `pass`: the event was lost without a trace.

**Action.** `@background_handler` to run one specific subscriber in the background, or
`RedisEventBus`/`PostgresEventBus` for real fan-out.

### 5. The decorators reject unresolvable objects

**What changed.** All three decorators raise `ValueError` if the object is defined inside another
function (`<locals>` in its `__qualname__`).

**Why.** The worker could never import it: previously the message enqueued fine and failed in the
worker, where it cannot be recovered.

**Action.** Move those definitions to module level.

### 6. `CQRSFactory` requires the enqueuer when there are background commands

**What changed.** `create_command_bus()` fails at construction if the registry holds
`@background_command`s and no `enqueuer` was given.

**Why.** It used to build a bus that raised `RuntimeError` on the first dispatch, with the user's
request already in flight.

**Action.** `cqrs.CQRSFactory(config, registry, enqueuer=enqueuer)`.

### 7. When a cron job runs

**What changed.** `DynamicScheduler` decides by catch-up — was there any occurrence between the
last run and now? — instead of comparing against the current minute.

**Why.** With `tick=60s` the accumulated drift skipped a whole minute and the job did not run;
with `tick<60s` it duplicated.

**Action.** None. If your repository did not implement `update_last_run`, implement it: that is
what deduplicates.

### 8. The query 422's `detail` is an object

**What changed.** It now returns `{"message": ..., "field": ..., "allowed": [...]}` instead of a
string.

**Action.** Adjust the client if it parsed `detail` as text.

---

## Behavior changes in 7.0

### CORS is no longer open out of the box

**What changed.** `allow_origins` is derived in a validator that sees the instance's real `debug`,
and `"*"` with `allow_credentials=True` stops being a valid configuration.

**Why.** The derivation lived in the class body, where `debug` is always `True`: the conditional
was dead code and the value was **always** `["*"]`, even with `ServerConfig(debug=False)`.
Combined with `allow_credentials=True`, Starlette reflects the attacker's `Origin` and adds
`Access-Control-Allow-Credentials: true`.

**Action.** If you use session cookies, declare your origins explicitly. See
[Configuration](./configuration.md#security-cors).

### The authentication rate limit fails closed

**What changed.** Darwin's `sign-in` limit uses `on_backend_error="deny"`, the opposite of the
framework default.

**Why.** A Redis outage should not become unlimited credential stuffing.

---

## 8.0: Darwin

8.0 does not break the 7.x API. What it adds is the complete identity module, with its nine
extras. If you are on 7.x and do not use identity, upgrading is a version bump.

If you are going to use Darwin, the one section that is not optional reading is the Alembic one:
[Darwin · Storage](./darwin/storage.md).

---

## 9.0: Event Sourcing, and one event bus

9.0 adds the event store and Event Sourcing — see
**[Event Sourcing](./event-sourcing.md)** — and unifies something that had been duplicated
since the CQRS layer landed.

### Deprecated, removed in 10.0

A full major of notice. Both warn with a `DeprecationWarning` **when you ask for the name**, not
when the module is imported: if they warned on import, there would be no way to tell *who* uses
the old name.

| Deprecated name | Replacement | Why |
| :-- | :-- | :-- |
| `hexcore.domain.events.EventBus` | `hexcore.domain.cqrs.buses.AbstractEventBus` | There were two event bus ports, mutually incompatible and with no common ancestor: a bus written against one did not work for the other, and the two handler graphs coexisted without seeing each other. The CQRS one stays — it also has the middleware pipeline and Smart Routing. |
| `hexcore.infrastructure.events.events_backends.memory.InMemoryEventBus` | `hexcore.cqrs.InMemoryEventBus` | Two classes with the same name hanging off those two ports. The CQRS one wins, being a strict superset. The whole `hexcore.infrastructure.events` package is removed in 10.0. |

The `EventBus` alias **resolves to the replacement**, not to the old object: the two ABCs were
structurally identical, so returning the new one does not break anyone on the next line. The
only change is that a bus which subclassed the old one now does pass `issubclass` against
`AbstractEventBus` — which is the fix, not the damage.

To find the usages before upgrading:

```sh
uv run pytest -W "error::DeprecationWarning"
```

### Behavior changes that cannot warn

These are semantics, not names, so no `DeprecationWarning` is possible.

**The event buses dispatch by hierarchy.** They used to do `self._handlers.get(type(event))` —
exact class. Subscribing to a base class received nothing **and did not fail either**: the
handler stayed registered and was silently never invoked. A handler subscribed to a base class
now receives its subclasses, and one registered at two levels runs once.

That limitation was shaping the domain: `hexcore/darwin/domain/events.py` declares its fourteen
events with no common base and says so explicitly, because with exact dispatch an `AuthEvent`
base would have been useless.

**`DomainEvent.event_name` uses `removesuffix` instead of `replace`.** `replace` removed *every*
occurrence, so `EventLogCreatedEvent` came out as `"LOGCREATED"`. Names that carry "Event" only
as a suffix — the project's convention, and every Darwin event — do not change value.

> ⚠️ **This changes `RabbitMQEventBus`'s routing keys.** `rabbitmq.py` reimplemented the same
> heuristic on the `subscribe` side, and both changed together: if only one had, the publisher
> would route with one key while the consumer's binding used the other, and AMQP discards what
> does not match **with no error at all**. A deployment with messages in flight needs both keys
> bound for one version.

**`SqlAlchemyUnitOfWork.commit()` collects the events before committing.** The previous order
committed first and only then called `collect_domain_events()`, which walks
`session.new | dirty | deleted` — and after a commit those three collections are empty. So the
UoW collected no events and published nothing, with no error and no log. Fixing it means a
handler that was subscribed and never ran starts running.

**`collect_domain_entities()` returns a list, not a set.** `BaseEntity` is a mutable pydantic
model, so it does not define `__hash__` and `set.add()` raised `TypeError`. It never surfaced
because the defect above kept the method from ever running with anything in it — one was hiding
the other. Deduplication is by identity, not equality: two different entities with the same
fields are equal to pydantic, and draining the events of only one would lose the other's.

**`ServerConfig.event_bus` uses `default_factory`.** The previous default was evaluated when the
class was defined, so every `ServerConfig()` in the process shared the same bus and the same
handler dictionary. In tests, a subscription made by one test was seen by the next.

**`RedisEventBus` acknowledges after the handlers, not before.** The `xack` was inside the `try`
and ran even when a handler failed, so the message left the PEL and was lost. It now stays
pending so it can be claimed with `XAUTOCLAIM`.

Redis and Postgres also resolve an event's type by FQN when it is not in the subscription
registry, instead of discarding it silently. That registry is only populated in `subscribe()`, so
with hierarchical dispatch the normal case becomes that the concrete type is not there —
whoever subscribes to a base class never registers it. `resolve_unknown_events=False` restores
the old semantics.

## Versioning

The project uses [Commitizen](https://commitizen-tools.github.io/commitizen/) with
`cz_conventional_commits`. The version bump and the `CHANGELOG` are **automatic** on merge to
`master`:

| Commit prefix | Effect |
| :-- | :-- |
| `fix:` | patch |
| `feat:` | minor |
| `feat!:` / `BREAKING CHANGE:` | major |
| `docs:`, `refactor:`, `test:`, `chore:` | none |

⚠️ A `feat!:` in a documentation PR triggers a major. That is literally what produced 6.0.0.

---

## Next

← Back to the **[index](./)**.
