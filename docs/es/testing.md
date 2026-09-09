# Testing

`hexcore.testing` trae los dobles y los helpers que hacen falta para probar una app hexagonal
sin levantar Redis, Postgres ni un broker. No necesita ningún extra.

```python
from hexcore.testing import (
    FakeLockProvider,
    FakeRepository,
    FakeUnitOfWork,
    InMemoryTaskEnqueuer,
    RecordedEnqueue,
    build_test_buses,
    override_cqrs,
)
```

---

## Los buses de prueba

```python
from hexcore.testing import build_test_buses

buses = build_test_buses()
buses.registry.register_command_handler(SendEmailCommand, SendEmailHandler())

await buses.command_bus.dispatch(SendEmailCommand(user_id="1", template="welcome"))

assert buses.enqueuer.command_names == ["SendEmailCommand"]
assert buses.enqueuer.commands[0].queue == "high_priority"
```

`build_test_buses()` monta los tres buses **con** enqueuer y serializer. Ése es el error más
común al testear CQRS: montarlos sin ellos, y que el primer `@background_command` explote con un
`RuntimeError` que no dice nada del test que lo provocó.

Lo que devuelve (`TestBuses`) es una `NamedTuple` con `registry`, `command_bus`, `query_bus`,
`event_bus`, `enqueuer` y `serializer`. Podés pasarle un registry propio o un enqueuer propio:
`build_test_buses(mi_registry, enqueuer=mi_enqueuer)`.

### Probar el otro lado: que el worker lo ejecuta

El mismo bus encola fuera del worker y ejecuta dentro. Para probar las dos mitades:

```python
import hexcore.cqrs as cqrs

consumer = cqrs.CQRSConsumer(buses.command_bus, buses.event_bus)

await buses.command_bus.dispatch(SendEmailCommand(user_id="1", template="welcome"))
assert manejados == []                       # todavía no corrió: se encoló

await consumer.process_command(buses.enqueuer.commands[0].payload)
assert manejados == ["1"]                    # el consumer lo ejecutó
```

Es el contrato del Smart Routing, y conviene tenerlo cubierto en tu propio suite: es el que se
rompe si alguien construye buses separados para la web y el worker.

---

## `InMemoryTaskEnqueuer`

Implementa `ITaskEnqueuer` guardando todo en listas:

| Atributo | Qué tiene |
| :-- | :-- |
| `commands` | `list[RecordedEnqueue]` — los comandos encolados |
| `events` | Los eventos |
| `handlers` | Los handlers de background |
| `tasks` | Las tareas |
| `command_names` / `task_names` / `handler_names` | Atajos: sólo los nombres |
| `clear()` | Vacía todo, para reusar el doble entre casos |

Cada `RecordedEnqueue` trae `kind`, `name`, `payload` y `queue`, así que se puede afirmar sobre
la cola además de sobre el nombre:

```python
assert enqueuer.tasks[0].name.endswith("clean_old_records_task")
assert enqueuer.tasks[0].queue == "maintenance"
```

Para probar el camino de error —qué hace tu código cuando el broker no acepta el mensaje— el
doble puede fallar a pedido:

```python
enqueuer = InMemoryTaskEnqueuer(fail_on={"SendEmailCommand"})
```

---

## `override_cqrs`

```python
from hexcore.testing import override_cqrs

with override_cqrs(app, command_bus=buses.command_bus):
    respuesta = client.post("/tickets", json={"titulo": "x"})
```

Sobreescribe los providers (`provide_command_bus`, `provide_query_bus`, `provide_event_bus`,
`provide_registry`) en `app.dependency_overrides`.

Guarda el valor previo de cada override, así que **se puede anidar y restaura aunque el bloque
lance**. `app.dependency_overrides` es un dict de instancia: un override que no se limpia se
filtra a todos los tests que reusen la app, y el que falla es uno que no tiene nada que ver.

---

## `FakeLockProvider`

```python
from hexcore.testing import FakeLockProvider

FakeLockProvider()                  # concede siempre
FakeLockProvider(grant=False)       # niega siempre
FakeLockProvider(shared=True)       # lock real en memoria, compartido
```

El modo `shared=True` es el interesante: se comporta como un lock de verdad entre instancias
distintas del provider, así que sirve para probar **dos schedulers concurrentes** —el caso que
el lock existe para cubrir— sin levantar Redis.

`raise_on_acquire=RuntimeError("redis caído")` cubre la tercera rama, que es la que decide el
`on_error` de los providers reales: qué pasa cuando el lock no contesta.

---

## Repositorios y UoW falsos

```python
from hexcore.testing import FakeRepository, FakeUnitOfWork

repo = FakeRepository(entities=[ticket_a, ticket_b])
uow = FakeUnitOfWork({"tickets": repo})

async with uow:
    ticket = await uow.tickets.get_by_id(ticket_a.id)
    await uow.commit()

assert uow.commits == 1
assert uow.rollbacks == 0
```

`FakeUnitOfWork` **cuenta** los `commit()` y los `rollback()` —no los marca con un booleano— y
acumula los eventos de dominio, así que se puede afirmar sobre lo que se publicó sin cablear un
bus. Contarlos es lo que deja ver un doble commit, que es el bug que
[`TransactionMiddleware` provoca](./cqrs.md) sobre un handler que ya gestiona su transacción.

`add_repository("tickets", repo)` es la alternativa fluida al dict, y devuelve el propio UoW.

`FakeRepository` registra además cuántas veces se llamó a cada método (`count_calls("save")`) y
sabe hacer `snapshot()` / `restore()`, que es cómo se prueba un rollback sin base de datos:

```python
antes = repo.snapshot()
...
repo.restore(antes)
```

---

## Fixtures de pytest

Activalas desde tu `conftest.py`:

```python
pytest_plugins = ["hexcore.testing.fixtures"]
```

| Fixture | Qué da |
| :-- | :-- |
| `anyio_backend` | `"asyncio"`, para los tests marcados `@pytest.mark.anyio` |
| `task_enqueuer` | Un `InMemoryTaskEnqueuer` |
| `lock_provider` | Un `FakeLockProvider` |
| `cqrs_buses` | Los `TestBuses`, ya cableados con el enqueuer de arriba |
| `sqlite_engine` | Un engine SQLite en memoria, con `StaticPool` |
| `sqlite_session` | Una `AsyncSession` sobre ese engine |
| `uow` | Un UoW sobre esa sesión |

`sqlite_engine` usa `StaticPool` a propósito: sin él, cada conexión a `:memory:` abre una base
**distinta**, y el test que crea la tabla no es el que la consulta.

---

## Testear la capa HTTP

```python
from fastapi.testclient import TestClient

with TestClient(app) as client:          # el `with` es lo que corre el lifespan
    assert client.get("/health").status_code == 200
```

⚠️ **El `with` no es opcional.** `TestClient(app)` sin contexto no ejecuta el lifespan, así que
el engine no se inicializa y el primer endpoint que toque la base falla con un error que apunta
a `init_engine` y no al test.

Para aislar la base entre tests, un `SqlEngineStep` con SQLite en memoria:

```python
from hexcore.fastapi import build_lifespan, create_app, SqlEngineStep

app = create_app(lifespan=build_lifespan(SqlEngineStep("sqlite+aiosqlite:///:memory:")))
```

---

## Testear identidad

Darwin trae sus propios dobles en `hexcore.darwin.testing`:

```python
from hexcore.darwin.testing import (
    FakeUserRepository,
    FakeSessionRepository,
    PlainTextHasher,
    authenticated_context,
    configure_test_identity,
    create_test_user,
)
```

| Helper | Qué hace |
| :-- | :-- |
| `configure_test_identity(config=None, *, seed_users=(), clock=None, now=None, plugins=None, **overrides)` | Cablea el contenedor entero con los fakes y una clave de prueba |
| `create_test_user(container, email, password, *, verified=True, scopes=())` | Da de alta un usuario **en** ese contenedor |
| `make_user(email, *, verified=True, scopes=())` | Sólo la entidad, sin persistirla |
| `authenticated_context(user, *, scopes=(), roles=(), transport="bearer")` | Un `AuthContext` autenticado, para usar como override |
| `impersonated_context(actor, subject, *, reason="test")` | Un contexto impersonado válido |
| `system_context(name="test:proceso")` | El contexto de sistema, para jobs y migraciones |
| `PlainTextHasher` | Reemplaza Argon2: hashear de verdad hace que un suite de auth tarde minutos |
| `RecordingAuditSink` | Guarda los `AuditRecord` para afirmar sobre ellos |
| `FixedClock` | Reloj congelado, para probar expiraciones sin dormir |

`FixedClock` es lo que permite testear el TTL de un token sin un `sleep`: adelantás el reloj y
verificás que el token dejó de valer. Se pasa con `configure_test_identity(now=...)`.

Y hay fixtures propias, con el mismo mecanismo:

```python
pytest_plugins = ["hexcore.darwin.testing.fixtures"]
```

Traen `identity_clock`, `identity_audit`, `identity_users` e `identity_container` — esta última
ya cableada con las tres anteriores y limpiada al terminar cada test, que es lo que evita que el
contenedor de identidad de un caso se filtre al siguiente.

---

## Reglas del suite del propio HexCore

Si contribuís al repositorio:

```sh
uv sync --extra all --group dev
uv run python -m pytest -q
```

- **El CI falla si algún test se saltea.** Un skip significa que faltó un extra en el entorno, y
  reportar verde sin haber ejecutado media suite es peor que fallar.
- Tres marcadores quedan fuera de la corrida normal porque son lentos o necesitan servicios:
  `pytest -m packaging` (construye la wheel de verdad), `pytest -m typing` (invoca pyright),
  `pytest -m mongo` (necesita un MongoDB en `HEXCORE_TEST_MONGO_URI`).
- `mongo` está **deseleccionado**, no salteado: un salteo rompería el gate de cero-skips.

---

## Siguiente

→ **[CLI](./cli.md)**.
