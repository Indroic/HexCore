# SQL layer

Engine, sessions, scopes and unit of work. Requires the `[sql]` extra.

```python
import hexcore.sql as sql
```

---

## Engine

```python
sql.init_engine()            # at startup
await sql.dispose_engine()   # at shutdown
```

With `build_lifespan(SqlEngineStep())` you do not call these by hand: the step does both, in
order, and the teardown runs even if startup failed later on.

`init_engine()` **with no arguments already produces a production-correct engine**, reading
`config.async_sql_database_url`. Two things are not configurable, because there is only one right
answer:

### `expire_on_commit=False`

With SQLAlchemy's default (`True`), entity attributes expire on commit and the next access
triggers a lazy load on a closed session → `MissingGreenlet` or `DetachedInstanceError`. It is
the number one bug of async SQLAlchemy.

If you genuinely need refresh-after-commit, build your own
`async_sessionmaker(engine, expire_on_commit=True)`.

### DSN normalization

A PaaS `DATABASE_URL` arrives as `postgresql://…` and `create_async_engine` will not accept it.
It is translated to `postgresql+asyncpg://` automatically:

```python
sql.normalize_async_dsn("postgresql://u:p@host/db")
# 'postgresql+asyncpg://u:p@host/db'
```

If your DSN already declares a driver (`postgresql+psycopg://`), it is left alone.

### What *is* configurable

```python
sql.init_engine(
    url="postgresql://user:pass@host/db",
    pool=sql.PoolSettings(size=20, max_overflow=10, recycle=1800),
    echo=True,                    # any create_async_engine kwarg is forwarded
)
```

| `PoolSettings` | Default | Note |
| :-- | :-- | :-- |
| `size` | `None` (SQLAlchemy's) | Permanent connections |
| `max_overflow` | `None` | Extra connections under load |
| `pre_ping` | `True` | **Deliberate**: a pool without pre-ping against Postgres behind a load balancer hands out dead connections on the first failover |
| `recycle` | `1800` | Seconds before recycling a connection |

It is a settings object rather than a list of keywords because these are four decisions about the
same thing: passing them loose invites configuring two and forgetting the other two.

### Accessing the engine and the factory

```python
engine = sql.get_engine()                  # fails if not initialized
factory = sql.get_session_factory()        # async_sessionmaker with expire_on_commit=False
```

---

## Scopes: session and UoW outside a request

`hx.get_session` and `hx.get_sql_uow` are FastAPI dependencies. For everything else — workers,
tasks, cron, scripts, seeds, CLI commands — there are four context managers:

```python
async with sql.session_scope() as session:      # bare session
    rows = (await session.execute(select(CronJobModel))).scalars().all()

async with sql.uow_scope() as uow:              # UoW, NOT entered
    await CloseTicketUseCase(uow).execute(request)

async with sql.open_uow_scope() as uow:         # UoW already entered
    await uow.tickets.save(ticket)
    await uow.commit()

async with sql.nosql_uow_scope() as uow:        # the Beanie equivalent
    ...
```

`session_scope` **deliberately does not build the UoW**: building it runs auto-discovery and
instantiates *every* domain repository, an absurd cost for reading one infrastructure table.

### The transaction convention

`uow_scope` and the `hx.get_sql_uow` dependency yield the UoW **without entering it**, so the use
case controls its own `async with self.uow:` without nesting contexts:

```python
class CloseTicketUseCase:
    def __init__(self, uow) -> None:
        self.uow = uow

    async def execute(self, request) -> None:
        async with self.uow:                  # the use case opens it
            ticket = await self.uow.tickets.get_by_id(request.ticket_id)
            ticket.close()
            await self.uow.tickets.save(ticket)
            await self.uow.commit()
```

If your endpoint works on an **already open** UoW, use `hx.get_sql_uow_open` or
`open_uow_scope`.

---

## Unit of Work

```python
from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork
```

The UoW:

1. **Discovers and instantiates the repositories** listed in
   `config.repository_discovery_paths`, and exposes them as attributes (`uow.tickets`,
   `uow.users`). If the set is empty, it fails to build with a diagnostic error.
2. **Accumulates the domain events** of the entities it touched, and publishes them to the
   `event_bus` **after** the commit. Publishing before the commit would announce something that
   might not happen.
3. **Rolls back** if the block raises.

```python
async with uow:
    await uow.tickets.save(ticket)
    await uow.commit()      # commit, then dispatch of the accumulated events
```

`BeanieUnitOfWork` is the MongoDB equivalent, with the same `IUnitOfWork` interface.

---

## `Base`, `BaseModel` and the naming convention

```python
from hexcore.sql import Base, BaseModel

class TicketModel(BaseModel["Ticket"]):
    __tablename__ = "tickets"

    title: Mapped[str]
```

`BaseModel` already ships `id` (UUID), `is_active`, `created_at` and `updated_at`, plus
`set_domain_entity()` / `get_domain_entity()`, which are what let the repository return the
domain entity without a second query.

`Base.metadata` carries an **explicit naming convention**:

| Kind | Pattern |
| :-- | :-- |
| Index | `ix_%(column_0_label)s` |
| Unique | `uq_%(table_name)s_%(column_0_N_name)s` |
| Check | `ck_%(table_name)s_%(constraint_name)s` |
| Foreign key | `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s` |
| Primary key | `pk_%(table_name)s` |

⚠️ **Without a convention, Alembic cannot generate the `DROP CONSTRAINT` for an anonymous
constraint**: the database gave it an autogenerated name the `MetaData` does not know, and the
downgrade is left to hand-editing. This is a decision to make before the first migration, because
changing it afterwards renames existing constraints.

---

## Alembic

The `env.py` generated by `hexcore init` already has what you need. If you write one by hand,
this is the minimum:

```python
# alembic/env.py
from hexcore.config import LazyConfig
from hexcore.sql import Base, ensure_framework_models_loaded, import_all_models

import myapp.infrastructure.database.models as models

# The tables the framework declares (today: hexcore_cron_jobs). Without this line they
# are outside Base.metadata and `--autogenerate` emits op.drop_table for them.
ensure_framework_models_loaded()

# And yours. Recursive: it walks subpackages of models/ too.
import_all_models(models)

target_metadata = Base.metadata

config.set_main_option("sqlalchemy.url", LazyConfig().get_config().sql_database_url)
```

If you use Darwin there is **one more line** — `ensure_identity_schema_loaded(plugins=[...])` —
and it is the module's most important warning: see
**[Darwin · Storage](./darwin/storage.md)**.

⚠️ All three lines share the same failure mode, and it is the worst one in the framework
**because it does not raise**: a table that exists in the database and is missing from
`Base.metadata` gets an `op.drop_table` in the next autogenerated migration. With data in it. The
migration is generated cleanly and the damage appears when it is applied.

The commands:

```sh
hexcore make-migrations "add the tickets table"   # alembic revision --autogenerate -m ...
hexcore migrate                                   # alembic upgrade head
```

---

## Next

→ **[Repositories and entities](./repositories.md)**.
