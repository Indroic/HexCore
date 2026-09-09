# Inicio rápido

Dos archivos. El primero es la app; el segundo, el worker. Todo lo que aparece acá tiene un
default que funciona, así que lo que no necesites lo podés borrar.

---

## La app, completa

```python
# main.py
from hexcore.fastapi import build_lifespan, create_app, SqlEngineStep

app = create_app(
    lifespan=build_lifespan(SqlEngineStep()),
    routers=[usuarios_router, tickets_router],
)
```

```sh
uvicorn main:app --reload
```

Eso ya trae, sin que lo pidas:

- `title` y `version` desde tu `ServerConfig`.
- CORS desde `config.allow_origins`.
- El middleware `X-Request-ID`, que reusa el header entrante si viene.
- El middleware de timing (`X-Response-Time`).
- El mapeo de excepciones de dominio a códigos HTTP.
- `GET /health` (liveness) y `GET /health/ready` (readiness, con sondas reales a SQL, Redis y
  Mongo, concurrentes y con timeout por sonda).
- El engine SQL levantado al arrancar y cerrado al apagar, en ese orden.

Para apagar algo hay **un solo objeto de interruptores**, no ocho keywords:

```python
from hexcore.fastapi import AppFeatures, create_app

app = create_app(features=AppFeatures(cors=False, timing=False))
```

→ Todo el detalle en **[Utilidades FastAPI](./fastapi.md)**.

---

## El worker, completo

```python
# worker.py
import asyncio

import hexcore.cqrs as cqrs
from hexcore.infrastructure.task_queues.procrastinate_adapter import (
    register_hexcore_procrastinate_tasks,
)


async def main() -> None:
    consumer = cqrs.CQRSConsumer(command_bus, event_bus)
    register_hexcore_procrastinate_tasks(procrastinate_app, consumer)

    await cqrs.run_procrastinate_worker(
        procrastinate_app,
        queues=["default", "reactive"],
        concurrency=4,
        scheduler=cqrs.DynamicScheduler(repo, enqueuer, lock_provider=lock),
        on_startup=[lambda: cqrs.seed_cron_jobs(CRON_JOBS)],
    )


if __name__ == "__main__":
    asyncio.run(main())
```

`command_bus` y `event_bus` son **los mismos** que usa el proceso web. No hay dos cableados: el
consumer marca el mensaje como «viene del worker», así que el bus lo ejecuta localmente en vez
de reencolarlo. Eso es el Smart Routing, y está explicado en
**[Colas y workers](./colas-y-workers.md)**.

Si **cualquiera** de los bucles muere —el del worker o el del scheduler— el runner cancela el
resto y el proceso sale con `WorkerDied`, para que el orquestador lo reinicie completo. Correr
con un bucle caído —encolar sin consumir, o al revés— es peor que caerse. `SIGTERM` y `SIGINT`
se traducen a drenaje ordenado.

---

## Un flujo de punta a punta

### 1. La entidad

```python
from uuid import UUID

from hexcore.domain.base import BaseEntity


class Ticket(BaseEntity):
    id: UUID
    titulo: str
    cerrado: bool = False
```

### 2. El comando y su handler

```python
import hexcore.cqrs as cqrs


class CrearTicket(cqrs.Command):
    titulo: str


class CrearTicketHandler:
    def __init__(self, uow) -> None:
        self.uow = uow

    async def handle(self, cmd: CrearTicket) -> str:
        async with self.uow:
            ticket = Ticket(titulo=cmd.titulo)
            await self.uow.tickets.save(ticket)
            await self.uow.commit()
        return str(ticket.id)
```

### 3. El registro y el bus

```python
import hexcore.cqrs as cqrs

registry = cqrs.HandlerRegistry()
registry.register_command_handler(
    CrearTicket,
    cqrs.HandlerRegistry.factory(lambda: CrearTicketHandler(build_uow())),
)
```

`HandlerRegistry.factory()` es un marcador explícito. Sin él, un handler que implemente
`__call__` sería indistinguible de un factory.

### 4. El endpoint

```python
from fastapi import APIRouter, Depends
from hexcore.fastapi import configure_cqrs, provide_command_bus

container = configure_cqrs(registry, enqueuer=enqueuer)   # una vez, al arrancar

router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.post("")
async def crear(cmd: CrearTicket, bus=Depends(provide_command_bus)):
    return {"id": await bus.dispatch(cmd)}
```

`provide_command_bus` existe como **función** por una única razón: poder sobreescribirla con
`app.dependency_overrides` en los tests.

### 5. El test

```python
from hexcore.testing import build_test_buses, override_cqrs

buses = build_test_buses()
buses.registry.register_command_handler(CrearTicket, CrearTicketHandler(FakeUoW()))

with override_cqrs(app, command_bus=buses.command_bus):
    respuesta = client.post("/tickets", json={"titulo": "Algo se rompió"})
```

→ **[Testing](./testing.md)**.

---

## Fuera de un request

`get_session` y `get_sql_uow` son dependencias de FastAPI: sirven en un endpoint y en ningún
otro lado. Para workers, tasks, cron, scripts y seeds hay **scopes**:

```python
import hexcore.sql as sql

async with sql.session_scope() as session:      # sesión pelada
    ...

async with sql.uow_scope() as uow:              # UoW SIN abrir
    await CerrarTicketUseCase(uow).execute(request)

async with sql.open_uow_scope() as uow:         # UoW ya abierto
    await uow.tickets.save(ticket)
    await uow.commit()
```

`session_scope` no construye el UoW **a propósito**: construirlo corre el auto-discovery e
instancia *todos* los repositorios de dominio, un coste absurdo para leer una tabla de
infraestructura.

→ **[Capa SQL](./sql.md)**.

---

## Estructura de proyecto

La CLI genera las dos que el framework soporta bien:

```sh
hexcore init mi_proyecto --template hexagonal
hexcore init mi_proyecto --template vertical-slice
```

Las dos crean `config.py` en la raíz con `repository_discovery_paths` de ejemplo y dejan Alembic
configurado —incluida la llamada a `ensure_identity_schema_loaded` en el `env.py`, que es la que
evita que `--autogenerate` te borre las tablas de identidad—.

→ **[CLI](./cli.md)**.

---

## Siguiente

→ **[Configuración](./configuracion.md)** — `ServerConfig`, discovery y CORS.
