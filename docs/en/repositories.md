# Repositories and entities

The domain model, its events, and the two repository implementations the framework ships.

---

## Entities

```python
from hexcore.domain.base import BaseEntity


class Ticket(BaseEntity):
    title: str
    closed: bool = False
```

`BaseEntity` is a pydantic model and already carries:

| Field | Type | Default |
| :-- | :-- | :-- |
| `id` | `UUID` | `uuid4()` |
| `created_at` | `datetime` | now, in UTC |
| `updated_at` | `datetime` | now, in UTC |
| `is_active` | `bool \| None` | `True` |

It is configured with `from_attributes=True` (so it can be built from an ORM model) and
`validate_assignment=True` (so assigning an invalid field fails where it is assigned, not three
layers down).

### Soft deletes

```python
ticket.deactivate()     # is_active = False
```

The generic repositories delete softly: `delete()` deactivates the row rather than issuing a
`DELETE`. `get_active_by_id()` is the variant that ignores deactivated rows.

---

## Domain events

```python
from hexcore.domain.events import EntityCreatedEvent


class TicketCreated(EntityCreatedEvent[Ticket]):
    pass


ticket = Ticket(title="Something broke")
ticket.register_event(TicketCreated(entity_id=ticket.id, entity_data=ticket))
```

| Class | Own fields |
| :-- | :-- |
| `DomainEvent` | `event_id`, `occurred_on`, and `event_name` as a property |
| `EntityCreatedEvent[T]` | `entity_id`, `entity_data: T` |
| `EntityUpdatedEvent[T]` | `entity_id`, `entity_data: T` |
| `EntityDeletedEvent` | `entity_id` |

Events are **frozen**: once emitted, an event is not modified. Mutating an event in flight would
make two subscribers see different things depending on the order they ran in.

### How they reach the bus

The entity **accumulates** them; the Unit of Work **publishes them after the commit**:

```python
async with uow:
    ticket.register_event(TicketCreated(entity_id=ticket.id, entity_data=ticket))
    await uow.tickets.save(ticket)
    await uow.commit()        # published here, and not before
```

Publishing before the commit would announce something that may still not happen.
`pull_domain_events()` takes them off the entity and clears them, so a second commit does not
resend them.

### Subscribing

```python
async def project(event: TicketCreated) -> None:
    ...

event_bus.subscribe(TicketCreated, project)
await event_bus.publish(event)
```

The canonical names are `subscribe()` and `publish()`. The old `register()` and `dispatch()` were
removed in 7.0.

The in-memory bus is fine for one process. For fan-out across replicas there are
`RedisEventBus`, `PostgresEventBus` (LISTEN/NOTIFY, no Redis needed) and `RabbitMQEventBus`: see
**[Queues and workers](./queues-and-workers.md)**.

---

## Generic repositories

There are two implementations with the same interface:

```python
from hexcore.infrastructure.repositories.implementations import (
    BeanieRepository,
    SqlAlchemyRepository,
)
```

Both import **lazily**: `SqlAlchemyRepository` requires `[sql]` and `BeanieRepository` requires
`[mongo]`, only when you ask for the name.

### Defining one

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

Three properties and you have the whole CRUD. The other two are optional:

| Property | Purpose |
| :-- | :-- |
| `entity_cls` | The domain entity it returns |
| `model_cls` / `document_cls` | The SQLAlchemy model or the Beanie document |
| `not_found_exception` | What `get_by_id` raises when there is no row |
| `fields_serializers` | Entity → model, for complex fields |
| `fields_resolvers` | Model → entity, for complex fields |

The last two are `{"field": callable}` maps, and they exist because the automatic conversion —
`to_entity_from_model_or_document` — covers scalars and simple relations, not a value object
serialized to JSON or a list stored in a separate table.

### The methods

| Method | Returns |
| :-- | :-- |
| `get_by_id(entity_id)` | The entity, or `not_found_exception` |
| `get_active_by_id(entity_id)` | The same, ignoring deactivated rows |
| `list_all(limit=None, offset=0)` | `list[T]` |
| `query_all(query)` | `(list[T], total)` |
| `query_cursor(query)` | `CursorPageDTO[T]` |
| `save(entity)` | The saved entity, re-read |
| `delete(entity)` | `None` — a **soft** delete |

### Discovery

The Unit of Work instantiates them for you, reading `config.repository_discovery_paths`, and
exposes them as attributes:

```python
async with sql.uow_scope() as uow:
    ticket = await uow.tickets.get_by_id(ticket_id)
```

If the path set is empty, the UoW **fails to build** with a diagnostic error. It does not guess
paths by folder convention: that tied the framework to one project layout and failed silently
when the layout was different.

---

## Queries: filters, search and sorting

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
        sort=[
            sql.SortConditionDTO(field="created_at", direction=sql.SortDirection.DESC),
        ],
    )
)
```

| Operator | Meaning |
| :-- | :-- |
| `EQ`, `NE` | Equal, not equal |
| `GT`, `GTE`, `LT`, `LTE` | Comparisons |
| `IN`, `NOT_IN` | List membership |
| `CONTAINS`, `STARTSWITH`, `ENDSWITH` | Text |
| `IS_NULL` | Nullness |

A field the entity does not have raises `UnsupportedQueryFieldError`, which the endpoint
translates into a **structured 422** with `field` and `allowed`. Not a raw string: a client
cannot program against a prose error message.

---

## Cursor pagination

`OFFSET 100000` forces the database to scan 100,000 rows just to discard them. For large lists:

```python
import hexcore.sql as sql

page = await repo.query_cursor(
    sql.CursorRequestDTO(
        limit=50,
        sort_field="created_at",
        direction=sql.SortDirection.DESC,
    )
)

page.items          # list[T]
page.next_cursor    # str or None; None means last page
```

The next page is requested by passing that cursor:

```python
following = await repo.query_cursor(sql.CursorRequestDTO(limit=50, cursor=page.next_cursor))
```

The cursor is **opaque on purpose** — base64url of the sort key plus the `id`. If it were
readable, clients would build it by hand and it would freeze as public API: changing the sort
criterion would become a breaking change.

It is composed with the `id` and not just the sort key because two rows with the same
`created_at` would make pagination skip or repeat records at the page boundary.

---

## Beanie documents

```python
from hexcore.infrastructure.repositories.orms.beanie.utils import init_beanie_documents

await init_beanie_documents()
```

Or declaratively, with `hx.BeanieStep(documents=[...])` in the lifespan.

⚠️ **`init_beanie` does not accumulate**: a second call against the same database replaces the
first call's registry. Every document — yours, identity's, the plugins' — has to go into **the
same call**. A `Document` that `init_beanie` never saw fails on the first query with
`CollectionWasNotInitialized`.

---

## Hybrid storage (CQRS with projections)

Commands write to normalized SQL; queries read from denormalized projections in Mongo or Redis,
kept in sync by the event bus:

```python
async def project_ticket(event: TicketCreated) -> None:
    await TicketReadDocument(id=event.entity_id, title=event.entity_data.title).insert()

event_bus.subscribe(TicketCreated, project_ticket)
```

HexCore **does not** generate read models, and that is deliberate: the pattern requires them to
be designed specifically for what your UI queries. A generator would produce a copy of the
normalized table, which is exactly what the projection exists to avoid.

---

## Next

→ **[FastAPI utilities](./fastapi.md)**.
