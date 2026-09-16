# Workflows

Ordered recipes. Each ends with the same gate:

```bash
python scripts/hexcore_surface.py --check <the files you wrote>
```

Project layout is **not** prescribed. HexCore supports layers-first (`hexagonal`) and
feature-first (`vertical-slice`) and is otherwise folder-agnostic — discovery is driven by
`repository_discovery_paths`, not by convention. Follow whatever the workspace already does;
these recipes name files by role, not by path.

---

## Start a project

```bash
hexcore init my_project --template hexagonal      # or vertical-slice
```

Both create the packages, write `config.py` **only if absent**, run `alembic init`, and wire
`env.py` with the three protective calls. Then:

1. Fill in `repository_discovery_paths` in `config.py`. The UoW will not build without it.
2. Set `app_title`, `app_version`, and the two database DSNs — sync for Alembic, async for the
   engine.
3. If you use cookies, declare `allow_origins` explicitly.

---

## Add a domain module

```bash
hexcore create-domain-module sales
```

Generates `entities.py`, `repositories.py`, `services.py`, `value_objects.py`, `events.py`,
`enums.py`, `exceptions.py` plus the matching test module. It does not overwrite an existing
module.

Then, in order:

1. **Entity** — subclass `BaseEntity`. Do not redeclare `id`, `created_at`, `updated_at`,
   `is_active`.
2. **Repository interface** — derive from `IBaseRepository`; declare only the specialised
   queries, since the base CRUD is inherited.
3. **Events** — subclass `EntityCreatedEvent[T]` / `EntityUpdatedEvent[T]` /
   `EntityDeletedEvent`, or `DomainEvent` directly. Emit them from the entity with
   `register_event()`.
4. **Exceptions** — one for "not found"; the repository will raise it.
5. **Service** — the business rules, if the module has any worth isolating.

→ `core.md`

---

## Add a SQL repository

1. **The model.** Subclass `BaseModel["Entity"]` with `__tablename__`. It already carries
   `id`, `is_active`, `created_at`, `updated_at`.
2. **The repository.** Subclass `SqlAlchemyRepository[Entity, Model]` and implement
   **three** properties: `entity_cls`, `model_cls`, `not_found_exception`. Add
   `fields_serializers` / `fields_resolvers` only for fields the automatic conversion cannot
   handle — a value object serialized to JSON, a list in a separate table. Apply
   `@cycle_protection_resolver` to a resolver walking a circular relation.
3. **Register it for discovery.** Add the module to `repository_discovery_paths`. Nothing
   finds it otherwise.
4. **Migrate.** `hexcore make-migrations "add the X table"`, **read the migration**, then
   `hexcore migrate`.

Do not implement `get_by_id`, `list_all`, `save` or `delete`. Template:
`assets/templates/repository.py`. → `core.md`

**Beanie variant:** subclass `BeanieRepository[Entity, Document]` with `document_cls`; your
document subclasses `Document` (not `BaseDocument`); every document goes in **one**
`init_beanie` call; and NoSQL tracking is explicit — `uow.collect_entity(entity)` or
`@register_entity_on_uow` on the save.

---

## Add a list/search endpoint

1. Build the use case over the repository — `QueryEntitiesUseCase` is the one place a
   repository is injected directly.
2. Register the endpoint:

```python
hx.register_query_endpoint(
    router,
    path="/tickets",
    use_case_factory=lambda: QueryEntitiesUseCase(repo),
)
```

That gives `limit`, `offset`, `search`, `search_fields`, `filters` (`field:operator:value`)
and `sort` (`field:asc|desc`), documented in OpenAPI, with a structured 422 on an unknown
field.

3. For large lists, prefer `query_cursor` with `CursorRequestDTO` over `offset`: `OFFSET
   100000` makes the database scan 100,000 rows to discard them.

→ `fastapi.md`

---

## Wire up the app

```python
app = hx.create_app(
    lifespan=hx.build_lifespan(hx.SqlEngineStep()),
    routers=[users_router, tickets_router],
)
```

Add steps as you add infrastructure — `BeanieStep`, `EventBusStep`, `CacheStep`,
`ProcrastinateStep`, `CronSeedStep`. Turn features off through `AppFeatures`, not by
hand-assembling middleware. Template: `assets/templates/config.py`. → `fastapi.md`

---

## Add a command and its handler

1. `class CreateTicket(cqrs.Command)` — **module level**, frozen, carrying only data.
2. The handler: `AbstractCommandHandler[CreateTicket, Result]` with `async def handle`.
   Taking the UoW directly is idiomatic.
3. Register: `registry.register_command_handler(CreateTicket, handler)`. If it needs a fresh
   UoW per message, register `cqrs.HandlerRegistry.factory(lambda: Handler(build_uow()))`.
4. Build the buses once with `hx.configure_cqrs(registry, enqueuer=enqueuer)`.
5. Dispatch from the endpoint through `Depends(hx.provide_command_bus)` — a function
   specifically so tests can override it.

Template: `assets/templates/handler.py`. → `cqrs-workers-cron.md`

---

## Move work to the background

1. Decorate: `@cqrs.background_command(queue="…")` on the Command class,
   `@cqrs.background_handler(...)` on one event subscriber, `@cqrs.background_task(...)` on any
   coroutine. **Module level, always.**
2. Build an enqueuer (`ProcrastinateEnqueuer` or `CeleryEnqueuer`) and pass it to the factory
   — without it, construction fails, which is the point.
3. Write the worker: `cqrs.CQRSConsumer(command_bus, event_bus)` on the **same** buses,
   `register_hexcore_procrastinate_tasks(app, consumer)`, then
   `cqrs.run_procrastinate_worker(...)`.
4. Deploy it as a **separate process**, same image, different command.

Do not add a second "dispatch async" API. The bus decides by context. Template:
`assets/templates/worker.py`. → `cqrs-workers-cron.md`

---

## Add a scheduled job

1. Write the task with `@cqrs.background_task(queue="…")` and a **docstring** — its first line
   becomes the `description` an operator reads before disabling the job.
2. `cqrs.cron_job(the_function, "*/5 * * * *", payload={...})` — pass the function, never the
   name.
3. Create the table via Alembic (or `create_cron_tables()` in dev) and seed with
   `seed_cron_jobs(CRON_JOBS)`, which does not overwrite database edits.
4. Hand a `DynamicScheduler` to the runner. **With more than one replica, pass a
   `lock_provider`.**
5. Confirm `env.py` calls `ensure_framework_models_loaded()` — `hexcore_cron_jobs` is a
   framework table.

→ `cqrs-workers-cron.md`

---

## Distribute events across replicas

Replace the in-memory bus with `RedisEventBus`, `PostgresEventBus` (LISTEN/NOTIFY, no Redis
needed) or `RabbitMQEventBus`. All three expose `start_consuming()` / `stop()`, so wrap them
in `cqrs.worker_loop(...)` and hand them to the runner.

Subscribing to a base class works since 9.0. If you are upgrading, re-check every
subscription: some were silently dead. → `cqrs-workers-cron.md`

---

## Event-source an aggregate

1. Subclass `AggregateRoot` (**not** `BaseEntity` — they are siblings, and the reasons are in
   `event-sourcing.md`).
2. `@when(SomeEvent)` mutators that **only mutate**: no validation, no raising, no emitting.
3. Business methods hold the rules and call `raise_event(...)`.
4. `EventSourcedRepository(Order, store=store)`; `get()` / `save()`.
5. Handle `ConcurrencyError` by reloading and reapplying the business decision.
6. Choose the store: `SqlAlchemyEventStore` for a primary store, `InMemoryEventStore` for
   tests. Not Redis as the source of truth.
7. Add the event store tables to `ensure_framework_models_loaded()`.

→ `event-sourcing.md`

---

## Add a projection

1. Subclass `AbstractProjection` with `name`, `handles`, an **idempotent** `apply`, and a
   `reset` if it writes anything.
2. Register it explicitly on the `Projector` — there is no discovery, deliberately.
3. `catch_up()` to advance; `rebuild()` to reprocess from event 1 — **destructive**, it calls
   `reset()` on every projection.
4. Set `safety_window` because `global_position` is monotonic but not contiguous.
5. If a relay is running, make sure the UoW has `publish_after_commit=False`.

→ `event-sourcing.md`

---

## Add identity

1. `pip install 'hexcore[darwin-sqlalchemy]'` (or `-beanie`), plus a plugin extra per feature.
2. `export HEXCORE_DARWIN_SECRET_KEY="$(hexcore identity generate-secret)"`. It has **no
   default**, on purpose.
3. `configure_identity(IdentityConfig())` at startup.
4. `create_app(features=AppFeatures(auth_context=True, csrf=True),
   lifespan=build_lifespan(SqlEngineStep(), *identity_startup_steps()),
   routers=[build_identity_router()])`.
5. **`env.py`: add `ensure_identity_schema_loaded(plugins=DARWIN_PLUGINS)`.** Get the exact
   plugin list from `hexcore identity plugins <module>`. Skipping this drops the credential
   store.
6. Put `hexcore identity check-schema` in CI.
7. Do not expose `POST /sign-up` publicly as-is: it answers 409 on a known email, which is an
   enumeration oracle.

→ `darwin.md`

---

## Write the tests

1. `pytest_plugins = ["hexcore.testing.fixtures"]` in `conftest.py`.
2. `build_test_buses()` — it wires the enqueuer and serializer, whose absence is the classic
   CQRS test failure.
3. `with TestClient(app) as client:` — the `with` runs the lifespan.
4. `override_cqrs(app, command_bus=...)` for dependency overrides that restore themselves.
5. Cover both halves of Smart Routing: dispatch enqueues, `consumer.process_command(...)`
   executes.
6. `FakeUnitOfWork` counts commits — which is how a double commit becomes visible.

Template: `assets/templates/conftest.py`. → `testing.md`

---

## Upgrade from 2.x–8.x

The ordered procedure is at the end of `references/removed-api.md`. Start with:

```bash
python scripts/hexcore_surface.py --version
python scripts/hexcore_audit.py src/ --fail-on none
```
