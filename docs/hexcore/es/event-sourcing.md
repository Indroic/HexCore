# Event Sourcing y el Event Store

> Nuevo en **9.0**.

Persistir los hechos, no sólo el estado. Antes de 9.0, HexCore tenía eventos de dominio y un bus
para publicarlos, y nada más: un evento que nadie consumía se perdía. El docstring de
`AuditLogMixin` ya lo decía al justificar por qué la auditoría se escribe en la misma
transacción que el cambio — *"los eventos son notificaciones y pueden perderse"*.

Eso dejaba tres cosas fuera de alcance:

- **No se puede reconstruir el pasado.** Si una regla de negocio cambia y hay que saber cómo se
  llegó a un estado, el estado no lo dice.
- **Un modelo de lectura nuevo nace vacío.** Sin historial, una proyección sólo puede acumular
  desde el día en que se la escribió.
- **La entrega no es fiable.** Publicar después de comitear tiene una ventana: si el proceso
  muere ahí, el cambio existe y el hecho no llegó a nadie.

---

## Las piezas

| Pieza | Qué es |
| :-- | :-- |
| `StoredEvent` | Un evento persistido, con su stream, su versión y su posición global |
| `AbstractEventStore` | El puerto del almacén: `append`, `read_stream`, `read_all`, `stream_version` |
| `AggregateRoot` | Un agregado cuyo estado es el fold de su stream |
| `AbstractSnapshotStore` | Estado resumido, para no releer streams largos |
| `AbstractProjection` | Un modelo de lectura alimentado evento a evento |
| `AbstractCheckpointStore` | Hasta dónde leyó cada suscripción |
| `EventSourcedRepository` | Cargar y guardar agregados |
| `Projector` | Recorre el orden global y alimenta proyecciones |
| `EventStoreRelay` | Publica al bus lo que ya está en el almacén |

Todo se importa desde `hexcore.eventsourcing`, que es una fachada perezosa: los adaptadores de
SQL, Mongo y Redis conviven ahí, y cada uno sólo exige su extra **en el momento exacto en que se
lo pide**.

```python
import hexcore.eventsourcing as es
```

---

## Dos modos, y confundirlos es donde esto se rompe

### Event log — el estado sigue en sus tablas

El Unit of Work escribe los eventos de tus entidades clásicas en el almacén, **en la misma
transacción que el cambio**. Se conserva el hecho y su orden; el estado sigue viviendo donde
estaba.

```python
from hexcore.eventsourcing import SqlAlchemyEventStore
from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork

store = SqlAlchemyEventStore(session=session)
uow = SqlAlchemyUnitOfWork(session=session, event_store=store)

await uow.commit()   # el evento y el cambio entran juntos, o no entra ninguno
```

Los eventos se escriben con `EXPECTED_VERSION_ANY`: no hay un agregado que haya leído una
versión y quiera defenderla. **Reconstruir estado desde estos streams no funciona** — los
eventos describen lo que pasó, pero el estado no se deriva sólo de ellos.

### Event sourcing — el stream *es* el estado

El agregado no tiene tabla. `EventSourcedRepository.get()` lo reconstruye aplicando su
historial, y `save()` escribe los eventos nuevos con la versión esperada.

```python
from hexcore.eventsourcing import AggregateRoot, EventSourcedRepository, when

class Pedido(AggregateRoot):
    cliente: str
    total: int                      # sin default: no existe un pedido sin total

    @when(PedidoCreado)
    def _crear(self, evento: PedidoCreado) -> None:
        self.cliente = evento.cliente
        self.total = 0

    @when(PedidoPagado)
    def _pagar(self, evento: PedidoPagado) -> None:
        self.total += evento.monto

    def pagar(self, monto: int) -> None:          # acá van las reglas
        if self.cancelado:
            raise ValueError("un pedido cancelado no se puede pagar")
        self.raise_event(PedidoPagado(monto=monto))

repo = EventSourcedRepository(Pedido, store=store)

pedido = await repo.get(pedido_id)
pedido.pagar(4200)
await repo.save(pedido)
```

Los dos modos conviven en la misma aplicación y en el mismo almacén.

---

## El agregado

**El mutador sólo muta.** No valida reglas, no lanza y no emite eventos: lo que llega ahí ya
pasó, y durante un replay se está reconstruyendo historia, no decidiendo. Las reglas van en el
método de negocio que llama a `raise_event()`.

`apply()` tiene doble vida —la usa `raise_event()` y la usa el replay—, y eso es lo que
garantiza que reconstruir un agregado dé exactamente el mismo estado que construirlo en vivo: es
literalmente el mismo código.

### Es hermana de `BaseEntity`, no hija

Las dos heredan de `EventRecorder`, así que un handler que hace `pull_domain_events()` funciona
contra ambas. Pero ninguna hereda de la otra:

1. **La trampa del Unit of Work.** `collect_domain_entities()` recorre la sesión, reconoce
   entidades de dominio con un `isinstance` y les drena los eventos. Un agregado que fuera además
   una `BaseEntity` pegada a un modelo del ORM publicaría **dos veces**: una por el UoW y otra
   por el repositorio.
2. **`validate_assignment`.** `BaseEntity` revalida el modelo entero en cada asignación. Un
   replay de cinco mil eventos con tres asignaciones cada uno son quince mil validaciones
   completas para llegar a un resultado que ya era válido.
3. **Sus campos sobran o mienten.** `is_active` en un agregado es un evento
   (`PedidoCancelado`), no una bandera; y `updated_at` lo pisa el ORM con su `onupdate`.

### `version` es la confirmada, no la actual

Sólo avanza en `mark_events_as_committed()`, que el repositorio llama **si y sólo si** el
`append` salió bien. Es lo que hace reintentable un `ConcurrencyError`: el agregado conserva sus
eventos pendientes y su versión, así que se puede recargar y reaplicar.

### `load_from_history` toma `aggregate_id`

`model_construct()` aplica el `default_factory=uuid4` del campo `id`, así que sin pasarlo dos
cargas del mismo historial dan agregados con `stream_id` distinto — y el `save()` siguiente
escribiría en el lugar equivocado. El repositorio siempre lo pasa.

---

## Concurrencia optimista

```python
from hexcore.eventsourcing import ConcurrencyError

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

## Proyecciones

```python
from hexcore.eventsourcing import AbstractProjection, Projector

class ResumenDePedidos(AbstractProjection):
    name = "resumen_de_pedidos"
    handles = (PedidoEvent,)     # alcanza a todas sus subclases

    async def apply(self, event, stored) -> None:
        ...                      # tiene que ser idempotente

    async def reset(self) -> None:
        ...                      # obligatorio si escribe algo

proyector = Projector(
    store=store,
    checkpoints=checkpoints,
    projections=[ResumenDePedidos()],
    safety_window=50,
)
await proyector.catch_up()
await proyector.rebuild()        # destructivo: llama a reset() y reprocesa desde el evento 1
```

`apply` recibe el `StoredEvent` **además** del evento, porque un evento de dominio no conoce su
`stream_id` ni su posición, y es `frozen` así que no se le puede pegar nada.

`rebuild()` es lo que hace innecesaria una migración de modelo de lectura: si su forma cambia,
se reconstruye. Y por eso mismo **es destructivo** — llama a `reset()` en cada proyección.

Las proyecciones se registran **explícitamente**, sin discovery. Es la misma decisión que
`repository_discovery_paths` documenta al pasar a discovery explícito en v2, y acá la
consecuencia sería peor: una proyección descubierta por accidente entra en el `rebuild()`, que
borra.

### El filtro de `handles`

Trabaja sobre la columna `event_type`, resolviendo el FQN a su clase una vez por tipo:
deserializar un evento que ninguna proyección quiere es trabajo puro, y en un rebuild de años de
historia esa diferencia decide si tarda minutos u horas.

**No** recorre `__subclasses__()` del tipo declarado, que sería más directo y está mal: una
subclase que vive en un módulo todavía no importado no aparece ahí, así que el conjunto de
eventos que recibe una proyección dependería del orden de imports del proceso.

---

## El outbox: Unit of Work y relay

Dos mitades:

- **Entrada**: el UoW escribe el evento en la misma transacción que el cambio. Si el commit
  falla, no queda ni el cambio ni el hecho.
- **Salida**: `EventStoreRelay` lee el orden global y publica al bus, guardando el checkpoint
  **después** de publicar.

**Con SQL, la tabla del event store *es* el outbox.** No hace falta una segunda tabla, y ésa es
la mitad de la razón por la que `StoredEvent.payload` guarda el sobre completo de
`serialize_envelope()`: republicar es reconstruir el sobre y publicarlo, con el actor y el
`request_id` originales intactos.

```python
from hexcore.eventsourcing import EventStoreRelay

relay = EventStoreRelay(store=store, bus=bus, checkpoints=checkpoints)
await relay.run_forever()
```

> ⚠️ **Con el relay activo, el UoW no debe publicar.** Pasale
> `publish_after_commit=False`, o cada evento sale dos veces —en silencio— y con handlers no
> idempotentes eso hace daño real. `EventStoreContainer.publish_after_commit_recomendado` dice
> cuál corresponde, para que la regla no dependa de que alguien la recuerde.

---

## Los cuatro backends

| Backend | Extra | Para qué sirve |
| :-- | :-- | :-- |
| `InMemoryEventStore` | ninguno | Tests, desarrollo, un proceso único que no necesita sobrevivir a su reinicio. **No persiste.** |
| `SqlAlchemyEventStore` | `[sql]` | **El recomendado para el almacén primario.** Transaccional, comparte sesión con el cambio de negocio. |
| `BeanieEventStore` | `[mongo]` | Aplicaciones ya en Mongo. Ver las dos limitaciones de abajo. |
| `RedisEventStore` | `[redis]` | Log rápido de vida corta, o buffer caliente para proyecciones. **No** como almacén primario. |

Los de memoria son adaptadores reales, no dobles de prueba: cumplen el contrato completo,
concurrencia optimista incluida. La suite de contrato corre contra todos — un event store con
dos implementaciones que se comportan distinto no es un puerto, son dos almacenes con la misma
firma.

### Configuración

```python
from hexcore.config import ServerConfig
from hexcore.eventsourcing import EventStoreConfig, ProjectionsConfig

config = ServerConfig(
    event_store=EventStoreConfig(
        backend="hexcore.eventsourcing.SqlAlchemyEventStore",
        projections=ProjectionsConfig(
            projections=["app.proyecciones.ResumenDePedidos"],
            safety_window=50,
        ),
        relay_enabled=True,
    ),
)
```

`EventStoreConfig` es un campo de `ServerConfig`, hermano de `cqrs` y `darwin` — **no** parte de
`CQRSConfig`. Ése está congelado y modela tres buses; meter el event store ahí haría que
`CQRSConfig(enabled=False)` apagara el almacén de la verdad.

```python
from hexcore.eventsourcing import configure_event_store

contenedor = configure_event_store()          # lee ServerConfig.event_store
store = contenedor.store()
```

`configure_event_store()` toma el serializador del contenedor de CQRS cuando hay uno.
Compartirlo no es una optimización: si divergen, un evento escrito por el store y otro publicado
por el bus no comparten formato, y el día que haya que releer el historial con el consumidor del
bus el payload no se va a poder reconstruir.

### La tabla de SQL

Tres reglas, y las tres se rompen de una línea:

1. **Los modelos no heredan `BaseModel[T]`.** `collect_domain_entities()` les pediría una entidad
   de dominio que no tienen, y lo haría *durante* el `commit()`.
2. **El módulo de modelos está en `ensure_framework_models_loaded()`.** Sin eso,
   `alembic revision --autogenerate` le emite `op.drop_table` al event store.
3. **Ninguna columna se llama `metadata`**: pisaría `Base.metadata`.

```python
from hexcore.eventsourcing import create_eventstore_tables

await create_eventstore_tables()     # atajo de desarrollo; en producción, Alembic
```

---

## Lo que este event store **no** garantiza

Esta sección es obligatoria de leer antes de diseñar encima.

### La entrega es at-least-once, no exactly-once

Publicar en un broker y registrar el progreso en la base son dos sistemas distintos; hacerlos
atómicos exige una transacción distribuida que ni el broker ni la base soportan acá.

El relay guarda el checkpoint **después** de publicar, así que una caída en el medio republica el
lote. El orden inverso sería peor: perdería eventos para siempre, porque al reiniciar el
checkpoint diría que ya se entregaron.

**Consecuencia: tus handlers y tus proyecciones tienen que ser idempotentes.** `event_id` es la
clave con la que deduplicar.

### `global_position` es monotónica pero **no contigua**

En SQL la posición sale de una secuencia, y una secuencia **se toma al insertar, no al
comitear**: la transacción que reservó la posición 10 puede comitear después de la que reservó la
11. Un lector que ya pasó por la 11 —un proyector con `WHERE global_position > checkpoint`— no
vería la 10 nunca.

En Mongo pasa lo mismo con otra forma: el contador se incrementa con `$inc` antes del insert, y
una escritura fallida quema la posición que reservó.

Dos herramientas, y ninguna es gratis:

- **`safety_window`** en `Projector` y `EventStoreRelay`: relee N posiciones hacia atrás en cada
  arranque, dándole a las escrituras rezagadas la chance de aparecer. Cuesta reprocesar, que la
  idempotencia ya cubre.
- **`ordering="serialized"`** en `SqlAlchemyEventStore`: toma un `pg_advisory_xact_lock` antes de
  insertar, así las escrituras se ordenan igual que los commits. Cuesta serializar **todas** las
  escrituras del almacén. Sólo PostgreSQL — en otro dialecto falla en vez de aceptar la opción y
  no cumplirla.

### Mongo: sin replica set no hay atomicidad

- El `append` de N eventos **no es atómico**. Un fallo a mitad deja el stream truncado, con
  versiones válidas pero incompletas; el índice único evita duplicados, no huecos.
- El `append` **no puede compartir transacción con el cambio de negocio**, así que el modo event
  log de más arriba pierde su garantía principal.

**En Mongo, el camino sano es Event Sourcing puro**: que el `append` *sea* el cambio. Con replica
set configurado, pasale al store la sesión de la transacción de Mongo.

### Redis: es memoria

Exige como mínimo AOF con `appendfsync everysec`, y aun así se puede perder el último segundo.
Con sólo RDB se pierden minutos. Y `maxmemory-policy allkeys-lru` puede desalojar streams
enteros: el adaptador nunca pasa `MAXLEN` —recortar un log de eventos es destruirlo— pero no
puede protegerse de una política del servidor.

### SQLite: los savepoints del driver

El driver de SQLite de la stdlib emite un `COMMIT` implícito antes de un `SAVEPOINT`, así que la
escritura queda confirmada y un `rollback()` posterior no la deshace. `SqlAlchemyEventStore`
detecta el dialecto y no usa savepoint ahí; el precio es que un `IntegrityError` con sesión
prestada deja abortada la transacción del llamador. En SQLite no hay escritura concurrente real
de todos modos, así que ese error casi no ocurre.

### Un snapshot no es una fuente de verdad

Es una optimización de lectura. Un almacén sin snapshots da exactamente los mismos resultados,
más lento — y por eso un fallo al guardar uno **se loguea y no se propaga**: perder una escritura
por no poder llenar una caché sería absurdo. Un snapshot corrupto se recupera borrándolo, porque
el almacén guarda historial en vez de pisar.

---

## Qué cambió en 9.0

Además de todo lo anterior, que es nuevo:

- **Un solo puerto de bus de eventos.** `hexcore.domain.events.EventBus` es un alias deprecado
  de `AbstractEventBus`; se elimina en 10.0.
- **Los buses despachan por jerarquía**, no por clase exacta. Un handler suscrito a una clase
  base ahora recibe sus subclases, donde antes no recibía nada y tampoco fallaba.
- **`DomainEvent.event_name` usa `removesuffix`.** Cambia las routing keys de
  `RabbitMQEventBus`: un despliegue con mensajes en vuelo necesita las dos claves bindeadas
  durante una versión.
- **`SqlAlchemyUnitOfWork.commit()` recolecta los eventos antes de comitear.** El orden anterior
  recolectaba post-commit, cuando `session.new | dirty | deleted` ya están vacías: el UoW **no
  publicaba ningún evento**, sin error y sin log.
- **`collect_domain_entities()` devuelve una lista, no un set.** `BaseEntity` es un modelo de
  pydantic mutable, así que no es hashable y `set.add()` levantaba `TypeError` — nunca salió a la
  luz porque el defecto anterior impedía que el método llegara a ejecutarse con algo adentro.
- **`ServerConfig.event_bus` usa `default_factory`.** El default anterior era una instancia
  compartida por todo el proceso.
- **`RedisEventBus` confirma el mensaje después de los handlers**, no antes.

Ver [versiones y migración](./versiones-y-migracion.md) para la lista completa.

---

## Siguiente

- **[Arquitectura CQRS](./cqrs.md)** — los buses por los que viajan estos eventos
- **[Repositorios y entidades](./repositorios.md)** — `BaseEntity` y el camino clásico
- **[Capa SQL](./sql.md)** — sesiones, scopes y el unit of work
- **[Testing](./testing.md)** — los adaptadores en memoria y las fixtures
