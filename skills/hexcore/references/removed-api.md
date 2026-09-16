# Removed, deprecated, and what changed silently

Three tables. The first two are renames a script can find. **The third is the one that
matters**: behaviour that changed without any name changing, so nothing can warn you.

Find what a project still has:

```bash
python scripts/hexcore_audit.py src/          # names, with file and line
python scripts/hexcore_surface.py --deprecated   # the live list, from the installed package
uv run pytest -W "error::DeprecationWarning"     # from inside the project's own suite
```

---

## Support policy

| Series | Status |
| :-- | :-- |
| **9.x** | ✅ **Active.** The only supported one. Adds the event store and event sourcing, and leaves a single event bus port. |
| 8.x | ⛔ Deprecated. Ships Darwin. Migrating to 9.x is mechanical. |
| 7.x | ⛔ Removes the pre-5.0 surface, fixes CORS and rate limiting. **No Darwin.** |
| 6.x | ⛔ Same API as 5.x. Contains the CORS and rate-limiting defects. |
| 5.x | ⛔ The first complete version of the 3.x–5.x migration. |
| 4.x, 3.x | ⛔ **Not releases meant to be used** — intermediate cuts, each merge triggered an automatic bump. |
| 2.x, 1.x | ⛔ Contains the silent bugs below. |

**Everything before 9.0 is deprecated.**

### Why 2.x and earlier should not be in production

Not a matter of taste. These raise no exception and appear in no error log, so a project can
be affected without knowing:

| Defect in ≤ 2.x | Symptom |
| :-- | :-- |
| The worker **re-enqueued** `@background_command`s instead of executing them | Silent infinite loop: the queue grows without bound, the handler never runs |
| FQN split with `rsplit(".", 1)` | A `Command` inside a containing class, or a task as a `@staticmethod`, enqueues fine and **fails in the worker**, where the message is unrecoverable |
| `PostgresLockProvider` never purged | ~10,000 rows/day **forever** in the main database |
| `expire_on_commit` not passed | `MissingGreenlet` / `DetachedInstanceError` reading an entity after `commit()` |
| `enqueue_event` was a `pass` | The event is lost without a trace |
| `DynamicScheduler` compared against the current minute | With `tick=60s` it skips minutes; with `tick<60s` it duplicates |
| Lock providers returned `False` on any error | A Redis outage **switches off the entire cron**, logged indistinguishably from the normal case |
| `asyncio.run()` per task in Celery | `Event loop is closed` with a shared `AsyncEngine` |
| `HandlerRegistry` claimed thread safety with no lock | Double handler instantiation under concurrency |

---

## 1. Removed in 7.0 — these do not exist

Deprecated since 5.0, warning for two full majors, gone in 7.0. The replacement is a rename,
not a behaviour change.

| Removed | Use instead |
| :-- | :-- |
| `ICommandBus`, `IQueryBus`, `IEventBus` | `AbstractCommandBus`, `AbstractQueryBus`, `AbstractEventBus` |
| `ICommandHandler`, `IQueryHandler` | `AbstractCommandHandler`, `AbstractQueryHandler` |
| `IMiddleware` | `AbstractMiddleware` |
| `ISerializer` | `AbstractSerializer` |
| `IEventDispatcher` | `EventBus` → now `AbstractEventBus` |
| `EventBus.register()` / `.dispatch()` | `EventBus.subscribe()` / `.publish()` |
| `ServerConfig.event_dispatcher` | `ServerConfig.event_bus` |
| `SQLAlchemyCommonImplementationsRepo` | `SqlAlchemyRepository` |
| `BeanieODMCommonImplementationsRepo` | `BeanieRepository` |
| `NoSqlUnitOfWork` | `BeanieUnitOfWork` |
| `reset_sqlalchemy_engine()` | `dispose_engine()` |
| `MiddlewareConfig` | **Removed in 3.0.** It was dead code: never read |

`ServerConfig(event_dispatcher=...)` **fails with an error that says what to use** rather than
being silently ignored. Without that validator pydantic would discard the unknown keyword and
the symptom would surface much later as "my events never arrive".

The aliases announced removal "in 6.0"; 6.0.0 shipped without removing them, because moving
the date was preferred over retroactively breaking people who had upgraded trusting they were
still there.

---

## 2. Deprecated in 9.0 — removed in 10.0

A full major of notice. Both warn **when you ask for the name**, not when the module is
imported: warning on import would give no way to tell *who* uses the old name.

| Deprecated | Replacement | Why |
| :-- | :-- | :-- |
| `hexcore.domain.events.EventBus` | `hexcore.domain.cqrs.buses.AbstractEventBus` | There were two event bus ports, mutually incompatible, with no common ancestor: a bus written against one did not work for the other, and the two handler graphs coexisted without seeing each other. The CQRS one stays — it also has the middleware pipeline and Smart Routing. |
| `hexcore.infrastructure.events.events_backends.memory.InMemoryEventBus` | `hexcore.cqrs.InMemoryEventBus` | Two classes with the same name on those two ports. The CQRS one wins, being a strict superset. **The whole `hexcore.infrastructure.events` package is removed in 10.0.** |
| `hexcore.domain.auth.PermissionsRegistry` | `hexcore.darwin.RoleRegistry` | Darwin replaces the pre-identity permission model |
| `hexcore.domain.auth.TokenClaims` | `hexcore.darwin.AccessTokenClaims` | Same |

`from hexcore import TokenClaims` warns too — the root package re-exports both under the same
`__getattr__`.

The `EventBus` alias **resolves to the replacement**, not to the old object: the two ABCs were
structurally identical, so returning the new one does not break anyone on the next line. The
only change is that a bus which subclassed the old one now passes `issubclass` against
`AbstractEventBus` — which is the fix, not the damage.

---

## 3. Changed without changing name

No `DeprecationWarning` is possible for any of these: they are semantics.

### In 9.0

**The event buses dispatch by hierarchy.** They used to do `self._handlers.get(type(event))` —
exact class. Subscribing to a base class received nothing **and did not fail either**: the
handler stayed registered and was silently never invoked. A handler subscribed to a base class
now receives its subclasses, and one registered at two levels runs once.

That limitation was shaping the domain: `hexcore/darwin/domain/events.py` declares its
fourteen events with no common base and says so, because with exact dispatch an `AuthEvent`
base would have been useless.

**`DomainEvent.event_name` uses `removesuffix` instead of `replace`.** `replace` removed
*every* occurrence, so `EventLogCreatedEvent` came out as `"LOGCREATED"`. Names carrying
"Event" only as a suffix do not change value.

> ⚠️ **This changes `RabbitMQEventBus`'s routing keys.** `rabbitmq.py` reimplemented the same
> heuristic on the `subscribe` side, and both changed together: if only one had, the publisher
> would route with one key while the consumer's binding used the other, and AMQP discards what
> does not match **with no error at all**. A deployment with messages in flight needs both
> keys bound for one version.

**`SqlAlchemyUnitOfWork.commit()` collects the events before committing.** The previous order
committed first, then called `collect_domain_events()`, which walks
`session.new | dirty | deleted` — empty after a commit. So the UoW collected no events and
published nothing, with no error and no log. Fixing it means **a handler that was subscribed
and never ran starts running**. If events suddenly fire after upgrading, this is why.

**`collect_domain_entities()` returns a list, not a set.** `BaseEntity` is a mutable pydantic
model, so it defines no `__hash__` and `set.add()` raised `TypeError`. It never surfaced
because the defect above kept the method from ever running with anything in it — one hid the
other. Deduplication is by identity, not equality: two different entities with the same fields
are equal to pydantic, and draining only one would lose the other's events.

**`ServerConfig.event_bus` uses `default_factory`.** The previous default was evaluated when
the class was defined, so every `ServerConfig()` in the process shared one bus and one handler
dictionary. In tests, a subscription made by one test was seen by the next.

**`RedisEventBus` acknowledges after the handlers, not before.** The `xack` was inside the
`try` and ran even when a handler failed, so the message left the PEL and was lost. It now
stays pending, claimable with `XAUTOCLAIM`.

Redis and Postgres also resolve an event's type by FQN when it is not in the subscription
registry, instead of discarding it silently. That registry is only populated in `subscribe()`,
so with hierarchical dispatch the normal case is that the concrete type is absent — whoever
subscribes to a base class never registers it. `resolve_unknown_events=False` restores the old
semantics.

### In 7.0

**CORS is no longer open out of the box.** `allow_origins` is derived in a validator that sees
the instance's real `debug`, and `"*"` with `allow_credentials=True` stops being a valid
configuration. The derivation used to live in the class body, where `debug` is always `True`:
the conditional was dead code and the value was **always** `["*"]`, even with
`ServerConfig(debug=False)`. Combined with `allow_credentials=True`, Starlette reflects the
attacker's `Origin` and adds `Access-Control-Allow-Credentials: true`.

**The authentication rate limit fails closed.** Darwin's `sign-in` limit uses
`on_backend_error="deny"`, the opposite of the framework default, because a Redis outage
should not become unlimited credential stuffing.

### In 5.0

1. **`expire_on_commit=False` in the session factory.** If you depended on refresh-after-commit,
   build your own `async_sessionmaker(engine, expire_on_commit=True)`.
2. **`get_sql_uow` no longer enters the UoW.** Use cases do their own `async with self.uow:`,
   which nested contexts under the previous dependency. If your endpoint worked on an
   already-open UoW, switch to `get_sql_uow_open`.
3. **`TransactionMiddleware` out of the default, and requires `uow_factory`.** The default
   built the session with HexCore's *internal* session factory instead of your engine, and
   committed after the handler — so a handler that already commits committed twice.
4. **`enqueue_event` raises instead of staying silent.** Use `@background_handler` for one
   subscriber, or `RedisEventBus`/`PostgresEventBus` for real fan-out.
5. **The decorators reject unresolvable objects** — anything with `<locals>` in its
   `__qualname__`. Move those definitions to module level.
6. **`CQRSFactory` requires the enqueuer when there are background commands.** It used to
   build a bus that raised `RuntimeError` on the first dispatch, with a user's request already
   in flight.
7. **When a cron job runs.** `DynamicScheduler` decides by catch-up instead of comparing
   against the current minute. If your repository did not implement `update_last_run`,
   implement it — that is what deduplicates.
8. **The query 422's `detail` is an object**: `{"message":…, "field":…, "allowed":[…]}`
   instead of a string. Adjust any client that parsed it as text.

---

## Upgrading a 2.x project: the order that works

1. **Inventory.** `python scripts/hexcore_audit.py src/ --fail-on none`. Fix the
   `removed-api` findings first — they are renames and the project will not import until they
   are done.
2. **Configuration.** `event_dispatcher` → `event_bus`. Add `repository_discovery_paths`: it
   is now required and the UoW will not build without it.
3. **Repositories.** `SQLAlchemyCommonImplementationsRepo` → `SqlAlchemyRepository`, and drop
   the two optional properties if they only returned `None`.
4. **Events.** `register()`/`dispatch()` → `subscribe()`/`publish()`. Then **review every
   subscription**: hierarchical dispatch may start delivering events to handlers that were
   silently dead, and the UoW fix may start publishing at all.
5. **`env.py`.** Add the three calls. Generate a migration and **read it** before applying —
   this is where a 2.x project discovers the tables that were never in `Base.metadata`.
6. **Workers.** Confirm the bus is shared between web and worker, and that `CQRSFactory` gets
   an `enqueuer`.
7. **Re-run the audit**, then the project's suite with
   `pytest -W "error::DeprecationWarning"`.
