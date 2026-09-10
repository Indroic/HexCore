# Event Sourcing en HexCore

> Estado: **estable desde 9.0**. Los puertos y la superficie de `hexcore.eventsourcing` están
> sujetos a la política de deprecación normal del proyecto.

---

## Índice

1. [Por qué existe](#1-por-qué-existe)
2. [Las piezas](#2-las-piezas)
3. [Los dos modos: event log y event sourcing](#3-los-dos-modos-event-log-y-event-sourcing)
4. [El agregado](#4-el-agregado)
5. [Concurrencia optimista](#5-concurrencia-optimista)
6. [Proyecciones](#6-proyecciones)
7. [El outbox: UoW y relay](#7-el-outbox-uow-y-relay)
8. [Los cuatro backends](#8-los-cuatro-backends)
9. **[Lo que este event store no garantiza](#9-lo-que-este-event-store-no-garantiza)**
10. [Qué cambió en 9.0](#10-qué-cambió-en-90)

---

## 1. Por qué existe

Antes de 9.0, HexCore tenía eventos de dominio y un bus para publicarlos, y nada más: un
evento que nadie consumía se perdía. El docstring de `AuditLogMixin` ya lo decía al justificar
por qué la auditoría se escribe en la misma transacción que el cambio — *"los eventos son
notificaciones y pueden perderse"*.

Eso deja tres cosas fuera de alcance:

- **No se puede reconstruir el pasado.** Si una regla de negocio cambia y hay que saber cómo
  se llegó a un estado, el estado no lo dice.
- **Un modelo de lectura nuevo nace vacío.** Sin historial, una proyección sólo puede
  acumular desde el día que se la escribió.
- **La entrega no es fiable.** Publicar después de comitear tiene una ventana: si el proceso
  muere ahí, el cambio existe y el hecho no llegó a nadie.

El Event Store resuelve las tres persistiendo los hechos. El Event Sourcing va un paso más:
hace de esos hechos la **fuente de verdad** de un agregado.

---

## 2. Las piezas

| Pieza | Qué es | Dónde |
| :-- | :-- | :-- |
| `StoredEvent` | Un evento persistido, con su stream, su versión y su posición global | `domain/eventsourcing/stored.py` |
| `AbstractEventStore` | El puerto del almacén: `append`, `read_stream`, `read_all`, `stream_version` | `domain/eventsourcing/store.py` |
| `AggregateRoot` | Agregado cuyo estado es el fold de su stream | `domain/eventsourcing/aggregate.py` |
| `AbstractSnapshotStore` | Estado resumido para no releer streams largos | `domain/eventsourcing/snapshots.py` |
| `AbstractProjection` | Un modelo de lectura alimentado evento a evento | `domain/eventsourcing/projections.py` |
| `AbstractCheckpointStore` | Hasta dónde leyó cada suscripción | `domain/eventsourcing/projections.py` |
| `EventSourcedRepository` | Cargar y guardar agregados | `application/eventsourcing/repository.py` |
| `Projector` | Recorre el orden global y alimenta proyecciones | `application/eventsourcing/projector.py` |
| `EventStoreRelay` | Publica al bus lo que ya está en el almacén | `application/eventsourcing/relay.py` |

Todo se importa desde `hexcore.eventsourcing`, que es una fachada perezosa: los adaptadores de
SQL, Mongo y Redis conviven ahí y cada uno sólo exige su extra **en el momento en que se lo
pide**.

---

## 3. Los dos modos: event log y event sourcing

Es la distinción más importante del módulo, y confundirlos es donde esto se rompe.

### Event log — el estado sigue en sus tablas

El Unit of Work escribe los eventos de tus entidades clásicas (`BaseEntity`) en el almacén,
en la misma transacción que el cambio. Se conserva **el hecho y su orden**; el estado sigue
viviendo en las tablas de siempre.

```python
uow = SqlAlchemyUnitOfWork(session=session, event_store=SqlAlchemyEventStore(session=session))
```

Se escribe con `EXPECTED_VERSION_ANY`: no hay un agregado que haya leído una versión y quiera
defenderla. **Reconstruir estado desde estos streams no funciona** — los eventos describen lo
que pasó, pero el estado no se deriva sólo de ellos.

### Event sourcing — el stream *es* el estado

El agregado no tiene tabla. `EventSourcedRepository.get()` lo reconstruye aplicando su
historial, y `save()` escribe los eventos nuevos con la versión esperada.

```python
repo = EventSourcedRepository(Pedido, store=store, snapshots=snapshots, snapshot_every=50)

pedido = await repo.get(pedido_id)
pedido.pagar(4200)
await repo.save(pedido)
```

Los dos modos conviven en la misma aplicación y en el mismo almacén.

---

## 4. El agregado

```python
from hexcore.eventsourcing import AggregateRoot, when

class Pedido(AggregateRoot):
    cliente: str
    total: int          # sin default: el modelo no admite un pedido sin total

    @when(PedidoCreado)
    def _crear(self, evento: PedidoCreado) -> None:
        self.cliente = evento.cliente
        self.total = 0

    @when(PedidoPagado)
    def _pagar(self, evento: PedidoPagado) -> None:
        self.total += evento.monto

    def pagar(self, monto: int) -> None:
        if self.cancelado:
            raise ValueError("un pedido cancelado no se puede pagar")
        self.raise_event(PedidoPagado(monto=monto))
```

**El mutador sólo muta.** No valida reglas, no lanza y no emite eventos: lo que llega ahí ya
pasó, y durante un replay se está reconstruyendo historia, no decidiendo. Las reglas van en el
método de negocio que llama a `raise_event()`.

`apply()` tiene doble vida —la usa `raise_event()` y la usa el replay—, y eso es lo que
garantiza que reconstruir un agregado dé exactamente el mismo estado que construirlo en vivo:
es literalmente el mismo código.

### Es hermana de `BaseEntity`, no hija

Las dos heredan de `EventRecorder`, así que un handler que hace `pull_domain_events()`
funciona contra ambas. Pero ninguna hereda de la otra:

1. **La trampa del UoW.** `collect_domain_entities()` recorre la sesión, reconoce entidades de
   dominio con un `isinstance` y les drena los eventos. Un agregado que fuera además una
   `BaseEntity` pegada a un modelo del ORM publicaría **dos veces**: una por el UoW y otra por
   el repositorio.
2. **`validate_assignment`.** `BaseEntity` revalida el modelo entero en cada asignación. Un
   replay de cinco mil eventos con tres asignaciones cada uno son quince mil validaciones
   completas para llegar a un resultado que ya era válido.
3. **Sus campos sobran o mienten.** `is_active` en un agregado es un evento
   (`PedidoCancelado`), no una bandera; y `updated_at` lo pisa el ORM con su `onupdate`.

### `version` es la confirmada, no la actual

Sólo avanza en `mark_events_as_committed()`, que el repositorio llama **si y sólo si** el
`append` salió bien. Es lo que hace reintentable un `ConcurrencyError`: el agregado conserva
sus eventos pendientes y su versión, así que se puede recargar y reaplicar.

---

## 5. Concurrencia optimista

```python
try:
    await repo.save(pedido)
except ConcurrencyError:
    pedido = await repo.get(pedido_id)   # otro escritor avanzó el stream
    pedido.pagar(4200)                   # se reaplica la decisión de negocio
    await repo.save(pedido)
```

**La garantía real no es el chequeo de versión, es la restricción de unicidad.** Todos los
adaptadores comprueban la versión antes de escribir, y ese chequeo es un TOCTOU: entre la
lectura y la escritura cabe otro escritor. Lo que de verdad impide dos eventos con la misma
versión:

| Backend | Mecanismo |
| :-- | :-- |
| SQL | `UNIQUE(stream_id, version)`; el `IntegrityError` se traduce a `ConcurrencyError` |
| Mongo | Índice único `(stream_id, version)`; `DuplicateKeyError` → `ConcurrencyError` |
| Redis | `XADD` con id explícito `<version>-0`: Redis rechaza un id ≤ al último del stream |
| Memoria | Un `asyncio.Lock` alrededor del append |

`EXPECTED_VERSION_NO_STREAM` (0) exige que el stream no exista, y es lo que hace segura la
creación: dos procesos que creen el mismo id a la vez, uno gana.

---

## 6. Proyecciones

```python
class ResumenDePedidos(AbstractProjection):
    name = "resumen_de_pedidos"
    handles = (PedidoEvent,)     # alcanza a todas sus subclases

    async def apply(self, event: DomainEvent, stored: StoredEvent) -> None:
        ...   # tiene que ser idempotente

    async def reset(self) -> None:
        ...   # obligatorio si escribe algo, o el rebuild queda incorrecto
```

`apply` recibe el `StoredEvent` **además** del evento porque el evento de dominio no conoce su
`stream_id` ni su posición, y es `frozen` así que no se le puede pegar.

`Projector.rebuild()` es lo que hace innecesaria una migración de modelo de lectura: si su
forma cambia, se reconstruye desde el evento 1. Y por eso mismo **es destructivo** — llama a
`reset()` en cada proyección.

Las proyecciones se registran **explícitamente**, sin discovery. Es la misma decisión que
`repository_discovery_paths` documenta al pasar a discovery explícito en v2, y acá la
consecuencia sería peor: una proyección descubierta por accidente entra en el `rebuild()`, que
borra.

---

## 7. El outbox: UoW y relay

Las dos mitades:

- **Entrada**: el UoW escribe el evento en la misma transacción que el cambio. Si el commit
  falla, no queda ni el cambio ni el hecho.
- **Salida**: `EventStoreRelay` lee el orden global y publica al bus, guardando el checkpoint
  **después** de publicar.

**Con SQL, la tabla del event store *es* el outbox.** No hace falta una segunda tabla, y ésa
es la mitad de la razón por la que `StoredEvent.payload` guarda el sobre completo de
`serialize_envelope()`: republicar es reconstruir el sobre y publicarlo, con el actor y el
`request_id` originales intactos.

> ⚠️ **Con el relay activo, el UoW no debe publicar.** Pasale
> `publish_after_commit=False`, o cada evento sale dos veces —en silencio— y con handlers no
> idempotentes eso hace daño real. `EventStoreContainer.publish_after_commit_recomendado` lo
> dice para que no dependa de que alguien lo recuerde.

---

## 8. Los cuatro backends

| Backend | Extra | Para qué sirve |
| :-- | :-- | :-- |
| `InMemoryEventStore` | ninguno | Tests, desarrollo, un proceso único que no necesita sobrevivir a su reinicio. **No persiste.** |
| `SqlAlchemyEventStore` | `[sql]` | **El recomendado para el almacén primario.** Transaccional, comparte sesión con el cambio de negocio. |
| `BeanieEventStore` | `[mongo]` | Aplicaciones ya en Mongo. Ver las dos limitaciones de §9. |
| `RedisEventStore` | `[redis]` | Log rápido de vida corta o buffer caliente para proyecciones. **No** como almacén primario. |

Los de memoria son adaptadores reales, no dobles de prueba: cumplen el contrato completo,
incluida la concurrencia optimista. La suite de contrato
(`tests/test_eventsourcing_store_contract.py`) corre sobre todos: un event store con dos
implementaciones que se comportan distinto no es un puerto, son dos almacenes con la misma
firma.

### La tabla de SQL

Tres reglas, y las tres se rompen de una línea:

1. **Los modelos no heredan `BaseModel[T]`.** `collect_domain_entities()` les pediría una
   entidad de dominio que no tienen, y lo haría *durante* el `commit()`.
2. **El módulo de modelos está en `ensure_framework_models_loaded()`.** Sin eso,
   `alembic revision --autogenerate` le emite `op.drop_table` al event store.
3. **Ninguna columna se llama `metadata`**: pisaría `Base.metadata`.

---

## 9. Lo que este event store **no** garantiza

Esta sección es obligatoria de leer antes de diseñar encima.

### 9.1 La entrega es at-least-once, no exactly-once

Publicar en un broker y marcar el progreso en la base son dos sistemas distintos; hacerlos
atómicos exige una transacción distribuida que ni el broker ni la base soportan acá.

El relay guarda el checkpoint **después** de publicar, así que una caída en el medio
republica el lote. El orden inverso sería peor: perdería eventos para siempre, porque al
reiniciar el checkpoint diría que ya se entregaron.

**Consecuencia: tus handlers y tus proyecciones tienen que ser idempotentes.** `event_id` es
la clave con la que deduplicar.

### 9.2 `global_position` es monotónica pero **no contigua**

En SQL, la posición sale de una secuencia, y una secuencia **se toma al insertar, no al
comitear**: la transacción que reservó la posición 10 puede comitear después de la que reservó
la 11. Un lector que ya pasó por la 11 —un proyector con `WHERE global_position > checkpoint`—
nunca vería la 10.

En Mongo pasa algo parecido: el contador se incrementa con `$inc` antes del insert, y una
escritura fallida quema la posición que reservó.

Herramientas, y ninguna es gratis:

- **`safety_window`** en `Projector` y `EventStoreRelay`: relee N posiciones hacia atrás en
  cada arranque, dándole a las escrituras rezagadas la chance de aparecer. Cuesta reprocesar,
  que la idempotencia ya cubre.
- **`ordering="serialized"`** en `SqlAlchemyEventStore`: toma un `pg_advisory_xact_lock` antes
  de insertar, así las escrituras se ordenan igual que los commits. Cuesta serializar **todas**
  las escrituras del almacén. Sólo PostgreSQL.

### 9.3 Mongo: sin replica set no hay atomicidad

- El `append` de N eventos **no es atómico**. Un fallo a mitad deja el stream truncado, con
  versiones válidas pero incompletas; el índice único evita duplicados, no huecos.
- El `append` **no puede compartir transacción con el cambio de negocio**, así que el modo
  event log del §3 pierde su garantía principal.

**En Mongo, el camino sano es Event Sourcing puro**: que el `append` *sea* el cambio. Con
replica set configurado, pasale la sesión de la transacción de Mongo al store.

### 9.4 Redis: es memoria

Exige como mínimo AOF con `appendfsync everysec`, y aun así se puede perder el último segundo.
Con sólo RDB se pierden minutos. Y `maxmemory-policy allkeys-lru` puede desalojar streams
enteros: el adaptador nunca pasa `MAXLEN` —recortar un log de eventos es destruirlo— pero no
puede protegerse de una política del servidor.

### 9.5 SQLite: los savepoints del driver

El driver de SQLite de la stdlib emite un `COMMIT` implícito antes de un `SAVEPOINT`, así que
la escritura queda confirmada y un `rollback()` posterior no la deshace. `SqlAlchemyEventStore`
detecta el dialecto y no usa savepoint ahí; el precio es que un `IntegrityError` con sesión
prestada deja la transacción abortada. En SQLite no hay escritura concurrente real de todos
modos, así que ese error casi no ocurre.

### 9.6 Un snapshot no es una fuente de verdad

Es una optimización de lectura. Un almacén sin snapshots da exactamente los mismos resultados,
más lento — y por eso un fallo al guardar uno **se loguea y no se propaga**: perder una
escritura por no poder cachear sería absurdo. Un snapshot corrupto se recupera borrándolo,
porque el almacén guarda historial en vez de pisar.

---

## 10. Qué cambió en 9.0

Además de todo lo anterior, que es nuevo:

- **Un solo puerto de bus de eventos.** `hexcore.domain.events.EventBus` es un alias deprecado
  de `AbstractEventBus`; se elimina en 10.0.
- **Los buses despachan por jerarquía**, no por clase exacta. Un handler suscrito a una clase
  base ahora recibe sus subclases, donde antes no recibía nada y no fallaba.
- **`DomainEvent.event_name` usa `removesuffix`.** Cambia las routing keys de
  `RabbitMQEventBus`: un despliegue con mensajes en vuelo necesita bindear las dos claves
  durante una versión.
- **`SqlAlchemyUnitOfWork.commit()` recolecta los eventos antes de comitear.** El orden
  anterior recolectaba post-commit, cuando `session.new | dirty | deleted` ya están vacías: el
  UoW **no publicaba ningún evento**, sin error y sin log.
- **`collect_domain_entities()` devuelve una lista, no un set.** `BaseEntity` es un modelo de
  pydantic mutable, así que no es hashable y `set.add()` levantaba `TypeError` — nunca saltó
  porque el defecto anterior impedía que el método llegara a ejecutarse con algo adentro.
- **`ServerConfig.event_bus` usa `default_factory`.** El default anterior era una instancia
  compartida por todo el proceso.
- **`RedisEventBus` confirma el mensaje después de los handlers**, no antes.
