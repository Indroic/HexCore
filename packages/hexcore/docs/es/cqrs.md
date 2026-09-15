# Arquitectura CQRS

HexCore integra CQRS de forma nativa, separando las operaciones de escritura (**Command**) de
las de lectura (**Query**). No hace falta ningún extra: `import hexcore.cqrs` funciona en una
instalación pelada.

```python
import hexcore.cqrs as cqrs
```

---

## Los tres buses

| Bus | Despacha | A cuántos handlers |
| :-- | :-- | :-- |
| `AbstractCommandBus` | `Command` — intenciones de mutación | Uno solo |
| `AbstractQueryBus` | `Query` — intenciones de lectura, sin mutar estado | Uno solo |
| `AbstractEventBus` | `DomainEvent` — hechos consumados | Muchos suscriptores |

La transacción de un comando la gestiona **el handler**. Si preferís que la gestione el bus,
añadí `TransactionMiddleware` con su `uow_factory` — con las advertencias de más abajo.

---

## Lo mínimo

```python
import hexcore.cqrs as cqrs


class CrearTicket(cqrs.Command):
    titulo: str


class CrearTicketHandler:
    async def handle(self, cmd: CrearTicket) -> str:
        ...


registry = cqrs.HandlerRegistry()
registry.register_command_handler(CrearTicket, CrearTicketHandler())

bus = cqrs.InMemoryCommandBus(registry=registry)
await bus.dispatch(CrearTicket(titulo="Algo se rompió"))
```

`Command` y `Query` son modelos pydantic **frozen**: un mensaje en vuelo no se muta. Es lo que
permite que un hook o un middleware lo reemplace por otro sin que nadie vea un estado
intermedio.

### Handlers tipados

```python
from hexcore.domain.cqrs import AbstractCommandHandler


class CrearTicketHandler(AbstractCommandHandler[CrearTicket, str]):
    def __init__(self, uow) -> None:
        self.uow = uow

    async def handle(self, command: CrearTicket) -> str:
        ...
```

Heredar del abstracto no es obligatorio —basta con tener `handle`— pero es lo que le da al
checker el tipo del resultado del `dispatch`.

---

## Factories: resolver dependencias en el dispatch

Un handler que necesita un UoW nuevo por mensaje no puede registrarse como instancia:

```python
registry.register_command_handler(
    CrearTicket,
    cqrs.HandlerRegistry.factory(lambda: CrearTicketHandler(build_uow())),
)
```

`HandlerRegistry.factory()` es un **marcador explícito**. Sin él, un handler que implemente
`__call__` sería indistinguible de un factory, y el registro tendría que adivinar.

`HandlerRegistry(allow_override=True)` permite reemplazar un handler ya registrado; el default
es `False` y lanza `DuplicateHandlerError`, porque en producción un doble registro es casi
siempre un módulo importado dos veces.

El registro es **thread-safe de verdad** (con lock). La versión de 2.x decía serlo sin ningún
lock, y bajo concurrencia instanciaba el handler dos veces.

---

## La factory de buses

La forma recomendada de construir los tres. Propaga enqueuer y serializer, y **falla al
construir** si falta algo:

```python
factory = cqrs.CQRSFactory(cqrs.CQRSConfig(), registry, enqueuer=enqueuer)

command_bus = factory.create_command_bus()
query_bus = factory.create_query_bus()
event_bus = factory.create_event_bus()
```

⚠️ Si el registry tiene comandos marcados con `@background_command` y la factory **no** recibió
`enqueuer`, `create_command_bus()` falla ahí mismo. Antes construía un bus que lanzaba
`RuntimeError` en el primer dispatch, con la petición del usuario ya en vuelo.

En una app FastAPI, `configure_cqrs()` hace lo mismo y deja el contenedor accesible desde las
dependencias:

```python
from hexcore.fastapi import configure_cqrs

container = configure_cqrs(registry, enqueuer=enqueuer)
consumer = container.build_consumer()      # el mismo cableado, para el worker
```

---

## Configuración declarativa

```python
from hexcore.application.cqrs.config import BusConfig, CQRSConfig
from hexcore.config import ServerConfig

config = ServerConfig(
    cqrs=CQRSConfig(
        command_bus=BusConfig(
            middlewares=["hexcore.infrastructure.cqrs.middlewares.LoggingMiddleware"],
        ),
    ),
)
```

| Campo de `BusConfig` | Qué hace |
| :-- | :-- |
| `backend` | Dotted path de la clase del bus |
| `middlewares` | Dotted paths, instanciados con `cls()` |
| `options` | Kwargs que se reenvían **al bus**, no a los middlewares |

`middlewares` admite sólo middlewares construibles **sin argumentos**. Los que necesitan
configuración se instancian a mano y se le pasa el `MiddlewarePipeline` al bus: un dotted path
no puede expresar «con *este* engine».

---

## Middlewares

```python
class Auditoria(cqrs.AbstractMiddleware):
    async def handle(self, message, next_handler: cqrs.NextHandler):
        registrar(message)
        resultado = await next_handler(message)
        return resultado
```

Los que trae el framework:

| Middleware | Qué hace |
| :-- | :-- |
| `LoggingMiddleware` | Loguea cada mensaje, su duración y su resultado |
| `ValidationMiddleware` | Revalida el mensaje antes del handler |
| `RetryMiddleware` | Reintenta el handler ante excepción |
| `TransactionMiddleware` | Abre un UoW, y comitea después del handler |

### ⚠️ `TransactionMiddleware` no es el default

Comitea **después** del handler, así que con un handler que ya gestiona su transacción
comitearías dos veces. Y necesita un `uow_factory` construido con *tu* engine, que no se puede
expresar como dotted path:

```python
cqrs.TransactionMiddleware(
    uow_factory=lambda: SqlAlchemyUnitOfWork(session=session_factory())
)
```

Sin `uow_factory` lanza `ValueError` al construirse. En 4.x y anteriores venía en el default y
armaba la sesión con el session factory *interno* de HexCore en vez del engine de la app.

### ⚠️ `RetryMiddleware` y el retry de la cola se multiplican

Si la cola reintenta 3 veces y el middleware 3 veces dentro de cada intento, el handler corre
hasta **12** veces, no 6. Con un handler no idempotente eso son 12 cobros.

Elegí uno:

- **El de la cola** para `@background_command`: persiste el intento y sobrevive a un reinicio
  del worker.
- **El middleware** para comandos síncronos, donde no hay cola que reintente.

El middleware avisa si detecta las dos cosas juntas.

---

## Excepciones

| Excepción | Cuándo |
| :-- | :-- |
| `CQRSError` | La base de todas |
| `HandlerNotFoundError` | Ningún handler registrado para ese tipo |
| `DuplicateHandlerError` | Dos registros para el mismo tipo sin `allow_override` |
| `DeserializationError` | El payload de la cola no reconstruye el mensaje |

---

## Serialización

```python
serializer = cqrs.PydanticSerializer()
```

Es lo que convierte un mensaje en el `dict` que viaja por la cola y lo reconstruye del otro
lado. Implementa `AbstractSerializer`, así que se puede reemplazar; lo que **no** conviene es
mezclar dos serializers entre el proceso web y el worker, porque el mensaje se serializa con uno
y se deserializa con el otro.

Resolución del tipo: el payload lleva el nombre completo del mensaje y el deserializador lo
importa. Por eso los decoradores rechazan clases definidas dentro de una función — ver
**[Colas y workers](./colas-y-workers.md)**.

---

## El sobre (envelope): metadata que viaja con el mensaje

Cuando un comando cruza a la cola, hay contexto del proceso web que el worker necesita y que no
es parte del payload: quién lo pidió, con qué request-id, en qué tenant.

```python
import hexcore.cqrs as cqrs

cqrs.register_envelope_metadata_provider("tenant", lambda: tenant_actual())
cqrs.register_envelope_restorer("tenant", MiRestaurador())
```

| Función | Para qué |
| :-- | :-- |
| `register_envelope_metadata_provider(clave, proveedor)` | Qué agregar al encolar |
| `register_envelope_restorer(clave, restaurador)` | Cómo reinstalarlo en el worker |
| `collect_envelope_metadata()` | El sobre actual |
| `restored_envelope_scope(...)` | El context manager que lo instala |
| `message_correlation_id()` | El `cid` del mensaje en curso |
| `registered_envelope_keys()` | Diagnóstico |
| `unregister_envelope_key(clave)` / `clear_envelope_registry()` | Limpieza, sobre todo en tests |

Darwin lo usa para que el actor autenticado cruce la cola en un sobre **firmado y atado al
mensaje** (`cid`, `mt`): sin ese binding, un grant capturado de un «borrar cuenta» se
re-adjuntaría a un «transferir fondos». Ver **[Darwin](./darwin/)**.

---

## Migrar desde `UseCase`

Si ya tenés la aplicación escrita con `UseCase`, la migración es progresiva:

```python
import hexcore.cqrs as cqrs

registry.register_command_handler(
    CreateUserCommand,
    cqrs.UseCaseCommandHandler(CreateUserUseCase(uow)),
)
```

Cuando estés listo, el use case pasa a ser un handler puro:

```python
from hexcore.domain.cqrs import AbstractCommandHandler


class CreateUserHandler(AbstractCommandHandler[CreateUserCommand, UserDTO]):
    def __init__(self, uow) -> None:
        self.uow = uow

    async def handle(self, command: CreateUserCommand) -> UserDTO:
        ...
```

`UseCase` no está deprecado: sigue siendo la abstracción correcta para orquestar sin pasar por
un bus. Lo que el adaptador permite es no reescribir todo de golpe.

---

## Siguiente

→ **[Colas y workers](./colas-y-workers.md)** — cómo se ejecuta esto en background.
