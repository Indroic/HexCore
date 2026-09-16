# Capa SQL

Engine, sesiones, scopes y unit of work. Requiere el extra `[sql]`.

```python
import hexcore.sql as sql
```

---

## Engine

```python
sql.init_engine()            # en el arranque
await sql.dispose_engine()   # en el apagado
```

Con `build_lifespan(SqlEngineStep())` no hace falta llamarlos a mano: el step hace las dos
cosas, en orden, y el teardown corre aunque el arranque haya fallado más adelante.

`init_engine()` **sin argumentos ya produce un engine correcto para producción**, leyendo
`config.async_sql_database_url`. Dos cosas no son configurables porque son la única respuesta
correcta:

### `expire_on_commit=False`

Con el default de SQLAlchemy (`True`) los atributos de las entidades expiran al comitear, y el
siguiente acceso dispara un lazy-load sobre una sesión cerrada → `MissingGreenlet` o
`DetachedInstanceError`. Es el bug número uno de SQLAlchemy asíncrono.

Si de verdad necesitás el refresco tras el commit, construí tu propio
`async_sessionmaker(engine, expire_on_commit=True)`.

### Normalización del DSN

Un `DATABASE_URL` de PaaS viene como `postgresql://…` y `create_async_engine` no lo acepta. Se
traduce a `postgresql+asyncpg://` solo:

```python
sql.normalize_async_dsn("postgresql://u:p@host/db")
# 'postgresql+asyncpg://u:p@host/db'
```

Si tu DSN ya declara driver (`postgresql+psycopg://`), se respeta tal cual.

### Lo que sí se configura

```python
sql.init_engine(
    url="postgresql://user:pass@host/db",
    pool=sql.PoolSettings(size=20, max_overflow=10, recycle=1800),
    echo=True,                    # cualquier kwarg de create_async_engine se reenvía
)
```

| `PoolSettings` | Default | Nota |
| :-- | :-- | :-- |
| `size` | `None` (el de SQLAlchemy) | Conexiones permanentes |
| `max_overflow` | `None` | Conexiones extra bajo pico |
| `pre_ping` | `True` | **Deliberado**: un pool sin pre-ping contra Postgres detrás de un balanceador entrega conexiones muertas al primer failover |
| `recycle` | `1800` | Segundos antes de reciclar una conexión |

Va en un objeto de settings y no en una lista de keywords porque son cuatro decisiones sobre lo
mismo: pasarlas sueltas invita a configurar dos y olvidar las otras dos.

### Acceso al engine y a la factory

```python
engine = sql.get_engine()                  # falla si no se inicializó
factory = sql.get_session_factory()        # async_sessionmaker con expire_on_commit=False
```

---

## Scopes: sesión y UoW fuera de un request

`hx.get_session` y `hx.get_sql_uow` son dependencias de FastAPI. Para todo lo demás —workers,
tasks, cron, scripts, seeds, comandos de la CLI— hay cuatro context managers:

```python
async with sql.session_scope() as session:      # sesión pelada
    rows = (await session.execute(select(CronJobModel))).scalars().all()

async with sql.uow_scope() as uow:              # UoW SIN abrir
    await CerrarTicketUseCase(uow).execute(request)

async with sql.open_uow_scope() as uow:         # UoW ya abierto
    await uow.tickets.save(ticket)
    await uow.commit()

async with sql.nosql_uow_scope() as uow:        # el equivalente para Beanie
    ...
```

`session_scope` **no construye el UoW a propósito**: construirlo corre el auto-discovery e
instancia *todos* los repositorios de dominio, un coste absurdo para leer una tabla de
infraestructura.

### La convención de transacción

`uow_scope` y la dependencia `hx.get_sql_uow` ceden el UoW **sin entrar en él**, para que el use
case controle su propio `async with self.uow:` sin anidar contextos:

```python
class CerrarTicketUseCase:
    def __init__(self, uow) -> None:
        self.uow = uow

    async def execute(self, request) -> None:
        async with self.uow:                  # ← el use case abre
            ticket = await self.uow.tickets.get_by_id(request.ticket_id)
            ticket.cerrar()
            await self.uow.tickets.save(ticket)
            await self.uow.commit()
```

Si tu endpoint opera sobre un UoW **ya abierto**, usá `hx.get_sql_uow_open` u `open_uow_scope`.

---

## Unit of Work

```python
from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork
```

El UoW:

1. **Descubre e instancia los repositorios** listados en `config.repository_discovery_paths`, y
   los expone como atributos (`uow.tickets`, `uow.usuarios`). Si el set está vacío, falla al
   construirse con un error diagnóstico.
2. **Acumula los eventos de dominio** de las entidades que tocó, y los publica en el
   `event_bus` **después** del commit. Antes del commit sería anunciar algo que puede no pasar.
3. **Hace rollback** si el bloque lanza.

```python
async with uow:
    await uow.tickets.save(ticket)
    await uow.commit()      # commit y, después, dispatch de los eventos acumulados
```

`BeanieUnitOfWork` es el equivalente para MongoDB, con la misma interfaz `IUnitOfWork`.

---

## `Base`, `BaseModel` y la convención de nombres

```python
from hexcore.sql import Base, BaseModel

class TicketModel(BaseModel["Ticket"]):
    __tablename__ = "tickets"

    titulo: Mapped[str]
```

`BaseModel` ya trae `id` (UUID), `is_active`, `created_at` y `updated_at`, más
`set_domain_entity()` / `get_domain_entity()` que son lo que permite al repositorio devolver la
entidad de dominio sin una segunda consulta.

`Base.metadata` lleva una **convención de nombres explícita**:

| Tipo | Patrón |
| :-- | :-- |
| Índice | `ix_%(column_0_label)s` |
| Único | `uq_%(table_name)s_%(column_0_N_name)s` |
| Check | `ck_%(table_name)s_%(constraint_name)s` |
| Foreign key | `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s` |
| Primary key | `pk_%(table_name)s` |

⚠️ **Sin convención, Alembic no puede generar el `DROP CONSTRAINT` de una constraint anónima**:
la base le puso un nombre autogenerado que el `MetaData` no conoce, y el downgrade queda a mano.
Es una decisión que hay que tomar antes de la primera migración, porque cambiarla después
renombra constraints existentes.

---

## Alembic

El `env.py` que genera `hexcore init` ya trae lo necesario. Si armás uno a mano, esto es lo
mínimo:

```python
# alembic/env.py
from hexcore.config import LazyConfig
from hexcore.sql import Base, ensure_framework_models_loaded, import_all_models

import myapp.infrastructure.database.models as models

# Las tablas que declara el framework (hoy: hexcore_cron_jobs). Sin esta línea quedan
# afuera de Base.metadata y `--autogenerate` les emite un op.drop_table.
ensure_framework_models_loaded()

# Y las tuyas. Es recursivo: también recorre subpaquetes de models/.
import_all_models(models)

target_metadata = Base.metadata

config.set_main_option("sqlalchemy.url", LazyConfig().get_config().sql_database_url)
```

Si usás Darwin, hace falta **una línea más** —`ensure_identity_schema_loaded(plugins=[...])`— y
es la advertencia más importante del módulo: ver
**[Darwin · Almacenamiento](./darwin/almacenamiento.md)**.

⚠️ El modo de falla que comparten las tres líneas es el mismo, y es el peor que tiene el
framework **porque no da error**: una tabla que existe en la base y está ausente de
`Base.metadata` recibe un `op.drop_table` en la próxima migración autogenerada. Con datos
adentro. La migración se genera limpia y el daño aparece al aplicarla.

Los comandos:

```sh
hexcore make-migrations "agrega la tabla de tickets"   # alembic revision --autogenerate -m ...
hexcore migrate                                        # alembic upgrade head
```

---

## Siguiente

→ **[Repositorios y entidades](./repositorios.md)**.
