# Core: entities, repositories, unit of work, SQL, configuration

Everything in the classic (non-CQRS) path. For the buses see `cqrs-workers-cron.md`.

---

## Entities

```python
from hexcore.domain.base import BaseEntity


class Ticket(BaseEntity):
    title: str
    closed: bool = False
```

`BaseEntity` is a pydantic model and **already carries** `id` (UUID), `created_at`,
`updated_at` (both UTC) and `is_active` (`bool | None`, default `True`). Redeclaring any of
them shadows the base field — don't.

It is configured `from_attributes=True` (so it builds from an ORM model) and
`validate_assignment=True` (so an invalid assignment fails where it is assigned, not three
layers down).

`ticket.deactivate()` sets `is_active = False`. The generic repositories delete **softly**:
`delete()` deactivates the row rather than issuing a `DELETE`, and `get_active_by_id()` is
the variant that ignores deactivated rows.

---

## Domain events

```python
from hexcore.domain.events import EntityCreatedEvent


class TicketCreated(EntityCreatedEvent[Ticket]):
    pass


ticket.register_event(TicketCreated(entity_id=ticket.id, entity_data=ticket))
```

| Class | Own fields |
| :-- | :-- |
| `DomainEvent` | `event_id`, `occurred_on`, `event_name` (property) |
| `EntityCreatedEvent[T]` / `EntityUpdatedEvent[T]` | `entity_id`, `entity_data: T` |
| `EntityDeletedEvent` | `entity_id` |

Events are **frozen**. Mutating one in flight would make two subscribers see different things
depending on the order they ran in.

### How they reach the bus {#events}

The entity accumulates them; the Unit of Work publishes them **after the commit**.

```python
async with uow:
    ticket.register_event(TicketCreated(entity_id=ticket.id, entity_data=ticket))
    await uow.tickets.save(ticket)
    await uow.commit()        # published here, and not before
```

Publishing before the commit would announce something that may still not happen.
`pull_domain_events()` takes them off the entity and clears them, so a second commit does not
resend them.

**Application code never calls `dispatch_events()` or `collect_domain_events()`.** The UoW
owns that. In 9.0 it collects *before* committing — the old order collected after, when
`session.new | dirty | deleted` are already empty, so the UoW published **nothing at all**,
with no error and no log. If you upgrade and handlers suddenly start running, that is why.

Subscribing uses `subscribe()` / `publish()`. The old `register()` / `dispatch()` were
removed in 7.0. Since 9.0 dispatch is **by hierarchy**: a handler subscribed to a base class
receives its subclasses, and one registered at two levels runs once.

---

## Repositories {#repositories}

```python
from hexcore.infrastructure.repositories.implementations import SqlAlchemyRepository


class TicketRepository(SqlAlchemyRepository[Ticket, TicketModel]):
    @property
    def entity_cls(self):
        return Ticket

    @property
    def model_cls(self):
        return TicketModel

    @property
    def not_found_exception(self):
        return TicketNotFound
```

**Three properties and you have the whole CRUD.** `BeanieRepository` is the same with
`document_cls` instead of `model_cls`. Both import lazily: `[sql]` and `[mongo]` respectively,
demanded only when you ask for the name.

| Property | Required | Purpose |
| :-- | :-- | :-- |
| `entity_cls` | yes | The domain entity it returns |
| `model_cls` / `document_cls` | yes | The SQLAlchemy model or Beanie document |
| `not_found_exception` | yes | What `get_by_id` raises when there is no row |
| `fields_serializers` | no | Entity → model, for complex fields |
| `fields_resolvers` | no | Model → entity, for complex fields |

The last two are `{"field": callable}` maps and exist because the automatic conversion,
`to_entity_from_model_or_document`, covers scalars and simple relations — not a value object
serialized to JSON or a list stored in a separate table. Apply `@cycle_protection_resolver`
to a resolver that walks a circular relation.

### What you get, and must not rewrite

| Method | Returns |
| :-- | :-- |
| `get_by_id(entity_id)` | The entity, or `not_found_exception` |
| `get_active_by_id(entity_id)` | The same, ignoring deactivated rows |
| `list_all(limit=None, offset=0)` | `list[T]` |
| `query_all(query)` | `(list[T], total)` |
| `query_cursor(query)` | `CursorPageDTO[T]` |
| `save(entity)` | The saved entity, re-read |
| `delete(entity)` | `None` — a **soft** delete |

Add specialised queries. Overriding one of the seven is almost always a misunderstanding of
what the base class already does.

### Discovery

The UoW instantiates repositories for you from `config.repository_discovery_paths` and
exposes them as attributes:

```python
async with sql.uow_scope() as uow:
    ticket = await uow.tickets.get_by_id(ticket_id)
```

If the path set is empty the UoW **fails to build**, with a diagnostic error. It does not
guess by folder convention: that tied the framework to one project layout and failed silently
when the layout differed.

---

## Queries, filters and cursors

```python
import hexcore.sql as sql

items, total = await repo.query_all(
    sql.QueryRequestDTO(
        limit=50,
        offset=0,
        search="invoice",
        search_fields=["title", "description"],
        filters=[
            sql.FilterConditionDTO(
                field="status",
                operator=sql.FilterOperator.IN,
                value=["open", "in_progress"],
            ),
        ],
        sort=[sql.SortConditionDTO(field="created_at", direction=sql.SortDirection.DESC)],
    )
)
```

Operators: `EQ`, `NE`, `GT`, `GTE`, `LT`, `LTE`, `IN`, `NOT_IN`, `CONTAINS`, `STARTSWITH`,
`ENDSWITH`, `IS_NULL`.

A field the entity does not have raises `UnsupportedQueryFieldError`, which the endpoint turns
into a **structured 422** carrying `field` and `allowed`. Not a prose string: a client cannot
program against an error message.

### Cursor pagination

`OFFSET 100000` makes the database scan 100,000 rows to discard them. For large lists:

```python
page = await repo.query_cursor(
    sql.CursorRequestDTO(limit=50, sort_field="created_at", direction=sql.SortDirection.DESC)
)
page.items          # list[T]
page.next_cursor    # str or None; None means last page

following = await repo.query_cursor(sql.CursorRequestDTO(limit=50, cursor=page.next_cursor))
```

The cursor is **opaque on purpose** — base64url of the sort key plus the `id`. If it were
readable, clients would build it by hand and it would freeze as public API: changing the sort
criterion would become a breaking change. It carries the `id` because two rows sharing a
`created_at` would make pagination skip or repeat records at the page boundary.

---

## SQL layer

```python
import hexcore.sql as sql

sql.init_engine()            # at startup
await sql.dispose_engine()   # at shutdown
```

With `build_lifespan(SqlEngineStep())` you never call these by hand. `init_engine()` with no
arguments already produces a production-correct engine, reading
`config.async_sql_database_url`. Two things are not configurable because there is one right
answer:

- **`expire_on_commit=False`.** With SQLAlchemy's default, attributes expire on commit and the
  next access lazy-loads on a closed session — `MissingGreenlet` / `DetachedInstanceError`,
  the number one bug of async SQLAlchemy.
- **DSN normalisation.** A PaaS `DATABASE_URL` arrives as `postgresql://…`, which
  `create_async_engine` refuses; it becomes `postgresql+asyncpg://` automatically. A DSN that
  already names a driver is left alone.

What *is* configurable: `sql.init_engine(url=..., pool=sql.PoolSettings(size=20,
max_overflow=10, recycle=1800), echo=True)` — any `create_async_engine` kwarg is forwarded.
`pre_ping=True` is the deliberate default: a pool without it, against Postgres behind a load
balancer, hands out dead connections on the first failover.

### Scopes

```python
async with sql.session_scope() as session:      # bare session
    ...
async with sql.uow_scope() as uow:              # UoW, NOT entered
    await CloseTicketUseCase(uow).execute(request)
async with sql.open_uow_scope() as uow:         # UoW already entered
    await uow.tickets.save(ticket)
    await uow.commit()
async with sql.nosql_uow_scope() as uow:        # the Beanie equivalent
    ...
```

`session_scope` deliberately does not build the UoW: building it runs auto-discovery and
instantiates *every* domain repository, an absurd cost for reading one infrastructure table.

The convention: `uow_scope` and `hx.get_sql_uow` yield the UoW **without entering it**, so the
use case controls its own `async with self.uow:` without nesting contexts.

### Unit of Work

```python
from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork
```

It discovers and instantiates the repositories, accumulates the domain events of the entities
it touched and publishes them after the commit, and rolls back if the block raises.
`BeanieUnitOfWork` is the MongoDB equivalent behind the same `IUnitOfWork` interface — and
note that NoSQL tracking is explicit: `uow.collect_entity(entity)` or the
`@register_entity_on_uow` decorator on the repository's save.

### `Base`, `BaseModel` and the naming convention

```python
from hexcore.sql import Base, BaseModel

class TicketModel(BaseModel["Ticket"]):
    __tablename__ = "tickets"

    title: Mapped[str]
```

`BaseModel` ships `id` (UUID), `is_active`, `created_at`, `updated_at`, plus
`set_domain_entity()` / `get_domain_entity()` — what lets the repository return the domain
entity without a second query.

`Base.metadata` carries an explicit naming convention (`ix_`, `uq_`, `ck_`, `fk_`, `pk_`).
Without one, Alembic cannot generate `DROP CONSTRAINT` for an anonymous constraint: the
database named it something the `MetaData` does not know. Decide this before the first
migration — changing it afterwards renames existing constraints.

⚠️ **Never name a column `metadata`** — it shadows `Base.metadata`. Darwin uses
`audit_metadata`.

### Framework tables {#framework-tables}

A table the *framework* declares must not inherit `BaseModel[T]`.
`collect_domain_entities()` walks `session.new | dirty | deleted`, filters on
`isinstance(model, BaseModel)` and calls `get_domain_entity()` — on a row with no domain
entity behind it that is an `AttributeError`, raised *after* the commit.

---

## Alembic

```python
# alembic/env.py
from hexcore.config import LazyConfig
from hexcore.sql import Base, ensure_framework_models_loaded, import_all_models

import myapp.infrastructure.database.models as models

ensure_framework_models_loaded()      # the framework's tables
import_all_models(models)             # yours, recursively

target_metadata = Base.metadata
config.set_main_option("sqlalchemy.url", LazyConfig().get_config().sql_database_url)
```

With Darwin there is one more line, `ensure_identity_schema_loaded(plugins=[...])`, and it is
the module's most important warning — see `darwin.md`.

⚠️ All three share the same failure mode, the worst in the framework **because it does not
raise**: a table that exists in the database and is missing from `Base.metadata` gets an
`op.drop_table` in the next autogenerated migration. With data in it. The migration is
generated cleanly; the damage appears when it is applied.

```bash
hexcore make-migrations "add the tickets table"
hexcore migrate
```

`sql_database_url` is the **synchronous** DSN, used by Alembic; `async_sql_database_url` is
what `init_engine()` consumes. Both exist because Alembic runs migrations synchronously and
the app runs asynchronously.

---

## Beanie documents {#beanie}

```python
from hexcore.infrastructure.repositories.orms.beanie.utils import init_beanie_documents

await init_beanie_documents()
```

Or declaratively, `hx.BeanieStep(documents=[...])` in the lifespan.

⚠️ **`init_beanie` does not accumulate**: a second call against the same database replaces the
first call's registry. Every document — yours, identity's, the plugins' — goes into the
**same** call. A `Document` `init_beanie` never saw fails on its first query with
`CollectionWasNotInitialized`.

Do **not** subclass `BaseDocument` for your own documents: it sets `is_root = True`
(single-collection inheritance) and `use_cache = True`. Subclass `Document` directly with your
own `Settings`.

---

## Configuration {#configuration}

```python
from hexcore.config import ServerConfig

config = ServerConfig(
    app_title="Red API",
    app_version="1.4.0",
    async_sql_database_url="postgresql+asyncpg://user:pass@localhost/red",
    repository_discovery_paths={
        "myapp.features.users.infrastructure.repositories",
        "myapp.features.billing.infrastructure.repositories",
    },
)
```

Export an **instance** named `config`, a **class** named `config` deriving from
`ServerConfig`, or a class named `ServerConfig` deriving from the base. All three are
accepted. There is no I/O and no configuration resolution at import time.

`LazyConfig` resolves the module in this order, keeping the first that works:
`HEXCORE_CONFIG_MODULE` → `HEXCORE_CONFIG_MODULES` (comma-separated) →
`LazyConfig.set_config_modules([...])` → the root `config` module. Nothing valid found falls
back to `ServerConfig()` with every default. `LazyConfig.clear_cache()` forces a fresh
resolution — what tests use to switch configuration between cases.

### The fields worth knowing

`base_dir`, `host`, `port`, `debug`, `app_title`, `app_version`; `sql_database_url`,
`async_sql_database_url`, `mongo_uri`, `mongo_db_name`, `redis_uri`, `redis_cache_duration`;
`cache_backend` (`ICache`, default `MemoryCache()`), `event_bus` (default
`InMemoryEventBus()`, built with `default_factory` since 9.0 so two `ServerConfig()` no longer
share one bus); `repository_discovery_paths`; and the optional modules `cqrs`, `darwin`,
`event_store`, all typed `t.Any` because annotating them would force `hexcore.config` to
import half the framework.

### CORS {#cors}

`allow_origins` is derived in an `mode="after"` validator. Not passed: `["*"]` under `debug`,
`["http://localhost:<port>"]` outside it. Passed explicitly — even `[]` — it is respected.

⚠️ **`"*"` with `allow_credentials=True` is never valid**, not just in production. The browser
cannot receive `*` with credentials, so Starlette **reflects the attacker's `Origin`** and
adds `Access-Control-Allow-Credentials: true`; any origin can then read authenticated
responses using the victim's cookie, with no XSS required. If you did not declare
`allow_credentials`, it is lowered to `False` with a warning. If you declared both, the app
**does not start**.

### Removed names fail loudly

```python
ServerConfig(event_dispatcher=bus)
# ValueError: ... se eliminó en 7.0 ... Usá 'event_bus'.
```

Without that validator pydantic would **silently discard** the unknown keyword, and the
symptom would surface much later as "my events never arrive".

---

## Environment variables

| Variable | Purpose |
| :-- | :-- |
| `HEXCORE_CONFIG_MODULE` | The configuration module (highest priority) |
| `HEXCORE_CONFIG_MODULES` | Several candidates, comma-separated |
| `HEXCORE_DARWIN_SECRET_KEY` | Darwin's signing key |

---

## CLI

```bash
hexcore init my_project --template hexagonal     # or vertical-slice
hexcore create-domain-module sales               # entities, repositories, services, ...
hexcore make-migrations "add the tickets table"
hexcore migrate
hexcore test src/domain --extra-args "-k users -q"
```

`hexcore init` writes `config.py` **only if it does not exist**, runs `alembic init`, wires
`env.py` with the three protective calls, and runs `ruff format` if ruff is installed. It
leaves a `manage.py` exposing the same CLI, so `python manage.py migrate` works without the
executable being on `PATH`.

Identity has its own sub-app: `hexcore identity generate-secret | generate-keys |
create-tables | check-schema | plugins`. See `darwin.md`.

---

## Extras

Core: `[api]`, `[sql]`, `[mongo]`, `[redis]`, `[rabbitmq]`, `[procrastinate]`, `[celery]`,
`[all]`. Darwin splits into nine more — see `darwin.md`.

A bare `pip install hexcore` brings three dependencies (`pydantic`, `typer`, `croniter`).
Importing a facade never requires an extra; asking it for a symbol does, and the error carries
the install command. Ask from your own code with `hexcore.capabilities.has_extra("redis")` /
`require_extra("sqlalchemy", para="MyRepository")`.
