# Repositorios y entidades

El modelo de dominio, sus eventos, y las dos implementaciones de repositorio que trae el
framework.

---

## Entidades

```python
from hexcore.domain.base import BaseEntity


class Ticket(BaseEntity):
    titulo: str
    cerrado: bool = False
```

`BaseEntity` es un modelo pydantic y ya trae:

| Campo | Tipo | Default |
| :-- | :-- | :-- |
| `id` | `UUID` | `uuid4()` |
| `created_at` | `datetime` | ahora, en UTC |
| `updated_at` | `datetime` | ahora, en UTC |
| `is_active` | `bool \| None` | `True` |

Está configurada con `from_attributes=True` (para construirse desde un modelo ORM) y
`validate_assignment=True` (para que asignar un campo inválido falle donde se asigna, no tres
capas más abajo).

### Borrado lógico

```python
ticket.deactivate()     # is_active = False
```

Los repositorios genéricos borran lógicamente: `delete()` desactiva la fila en vez de emitir un
`DELETE`. `get_active_by_id()` es la variante que ignora lo desactivado.

---

## Eventos de dominio

```python
from hexcore.domain.events import EntityCreatedEvent


class TicketCreado(EntityCreatedEvent[Ticket]):
    pass


ticket = Ticket(titulo="Algo se rompió")
ticket.register_event(TicketCreado(entity_id=ticket.id, entity_data=ticket))
```

| Clase | Campos propios |
| :-- | :-- |
| `DomainEvent` | `event_id`, `occurred_on`, y `event_name` como propiedad |
| `EntityCreatedEvent[T]` | `entity_id`, `entity_data: T` |
| `EntityUpdatedEvent[T]` | `entity_id`, `entity_data: T` |
| `EntityDeletedEvent` | `entity_id` |

Los eventos son **`frozen`**: una vez emitido, un evento no se modifica. Mutar un evento en
vuelo haría que dos suscriptores vieran cosas distintas según el orden en que corrieron.

### Cómo llegan al bus

La entidad los **acumula**; el Unit of Work los **publica después del commit**:

```python
async with uow:
    ticket.register_event(TicketCreado(entity_id=ticket.id, entity_data=ticket))
    await uow.tickets.save(ticket)
    await uow.commit()        # acá se publican, y no antes
```

Publicar antes del commit sería anunciar algo que todavía puede no pasar. `pull_domain_events()`
los saca y los limpia de la entidad, así que un segundo commit no los reenvía.

### Suscribirse

```python
async def proyectar(evento: TicketCreado) -> None:
    ...

event_bus.subscribe(TicketCreado, proyectar)
await event_bus.publish(evento)
```

Los nombres canónicos son `subscribe()` y `publish()`. Los viejos `register()` y `dispatch()` se
eliminaron en 7.0.

El bus in-memory sirve para un proceso. Para fan-out entre réplicas están `RedisEventBus`,
`PostgresEventBus` (LISTEN/NOTIFY, sin Redis) y `RabbitMQEventBus`: ver
**[Colas y workers](./colas-y-workers.md)**.

---

## Repositorios genéricos

Hay dos implementaciones, con la misma interfaz:

```python
from hexcore.infrastructure.repositories.implementations import (
    BeanieRepository,
    SqlAlchemyRepository,
)
```

Las dos se importan **perezosamente**: `SqlAlchemyRepository` exige `[sql]` y `BeanieRepository`
exige `[mongo]`, sólo cuando pedís el nombre.

### Definir uno

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
        return TicketNoEncontrado
```

Tres propiedades y ya tenés el CRUD entero. Las otras dos son opcionales:

| Propiedad | Para qué |
| :-- | :-- |
| `entity_cls` | La entidad de dominio que devuelve |
| `model_cls` / `document_cls` | El modelo SQLAlchemy o el documento Beanie |
| `not_found_exception` | La excepción que lanza `get_by_id` cuando no hay fila |
| `fields_serializers` | Entidad → modelo, para campos complejos |
| `fields_resolvers` | Modelo → entidad, para campos complejos |

Los dos últimos son mapas `{"campo": callable}`, y existen porque la conversión automática
—`to_entity_from_model_or_document`— cubre escalares y relaciones simples, no un value object
que se serializa a JSON o una lista que se guarda en una tabla aparte.

### Los métodos

| Método | Devuelve |
| :-- | :-- |
| `get_by_id(entity_id)` | La entidad, o `not_found_exception` |
| `get_active_by_id(entity_id)` | Igual, ignorando lo desactivado |
| `list_all(limit=None, offset=0)` | `list[T]` |
| `query_all(query)` | `(list[T], total)` |
| `query_cursor(query)` | `CursorPageDTO[T]` |
| `save(entity)` | La entidad guardada, releída |
| `delete(entity)` | `None` — borrado **lógico** |

### El descubrimiento

El Unit of Work los instancia solo, leyendo `config.repository_discovery_paths`, y los expone
como atributos:

```python
async with sql.uow_scope() as uow:
    ticket = await uow.tickets.get_by_id(ticket_id)
```

Si el set de paths está vacío, el UoW **falla al construirse** con un error diagnóstico. No
adivina rutas por convención de carpetas: eso ataba el framework a una estructura de proyecto y
fallaba en silencio cuando la estructura era otra.

---

## Consultas: filtros, búsqueda y orden

```python
import hexcore.sql as sql

items, total = await repo.query_all(
    sql.QueryRequestDTO(
        limit=50,
        offset=0,
        search="factura",
        search_fields=["titulo", "descripcion"],
        filters=[
            sql.FilterConditionDTO(
                field="estado",
                operator=sql.FilterOperator.IN,
                value=["abierto", "en_curso"],
            ),
        ],
        sort=[
            sql.SortConditionDTO(field="created_at", direction=sql.SortDirection.DESC),
        ],
    )
)
```

| Operador | Significado |
| :-- | :-- |
| `EQ`, `NE` | Igual, distinto |
| `GT`, `GTE`, `LT`, `LTE` | Comparaciones |
| `IN`, `NOT_IN` | Pertenencia a una lista |
| `CONTAINS`, `STARTSWITH`, `ENDSWITH` | Texto |
| `IS_NULL` | Nulidad |

Un campo que la entidad no tiene levanta `UnsupportedQueryFieldError`, que el endpoint traduce a
un **422 estructurado** con `field` y `allowed`. No un string crudo: un cliente no puede
programar contra un mensaje de error en prosa.

---

## Paginación por cursor

`OFFSET 100000` obliga a la base a escanear 100.000 filas para descartarlas. Para listados
grandes:

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
page.next_cursor    # str o None; None es la última página
```

La página siguiente se pide pasando ese cursor:

```python
siguiente = await repo.query_cursor(sql.CursorRequestDTO(limit=50, cursor=page.next_cursor))
```

El cursor es **opaco a propósito** —base64url de la clave de orden más el `id`—. Si fuera
legible, los clientes lo construirían a mano y quedaría congelado como API pública: cambiar el
criterio de orden pasaría a ser un breaking change.

Va compuesto con el `id` y no sólo con la clave de orden porque dos filas con el mismo
`created_at` harían que la paginación saltee o repita registros en el borde de la página.

---

## Documentos Beanie

```python
from hexcore.infrastructure.repositories.orms.beanie.utils import init_beanie_documents

await init_beanie_documents()
```

O declarativamente, con `hx.BeanieStep(documents=[...])` en el lifespan.

⚠️ **`init_beanie` no acumula**: una segunda llamada sobre la misma base reemplaza el registro de
la primera. Todos los documentos —los tuyos, los de identidad, los de los plugins— tienen que
entrar en **la misma llamada**. Un `Document` que `init_beanie` no vio falla en la primera
consulta con `CollectionWasNotInitialized`.

---

## Almacenamiento híbrido (CQRS con proyecciones)

Los comandos escriben en SQL normalizado; las queries leen de proyecciones desnormalizadas en
Mongo o Redis, sincronizadas por el event bus:

```python
async def proyectar_ticket(evento: TicketCreado) -> None:
    await TicketReadDocument(id=evento.entity_id, titulo=evento.entity_data.titulo).insert()

event_bus.subscribe(TicketCreado, proyectar_ticket)
```

HexCore **no** genera los modelos de lectura, y es deliberado: el patrón pide que estén
diseñados específicamente para lo que tu UI consulta. Un generador produciría una copia de la
tabla normalizada, que es exactamente lo que la proyección viene a evitar.

---

## Siguiente

→ **[Utilidades FastAPI](./fastapi.md)**.
