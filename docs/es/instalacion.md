# Instalación

```sh
pip install hexcore
```

Requiere **Python ≥ 3.12**. El núcleo instalado por ese comando trae tres dependencias
(`pydantic`, `typer`, `croniter`) y nada más: todo lo pesado va en **extras**, y los módulos que
las necesitan sólo las importan cuando las usás.

```sh
pip install "hexcore[api,sql,procrastinate]"
pip install "hexcore[all]"
```

---

## Los extras del núcleo

| Extra | Trae | Habilita |
| :-- | :-- | :-- |
| `[api]` | FastAPI | `hexcore.fastapi`: `create_app`, lifespan, middlewares, health, rate limit, streaming |
| `[sql]` | SQLAlchemy, Alembic, asyncpg, aiosqlite | `hexcore.sql`, repositorios SQL, cron en SQL, `PostgresLockProvider` |
| `[mongo]` | Beanie (arrastra PyMongo) | Repositorios y UoW de MongoDB |
| `[redis]` | redis | `RedisEventBus`, `RedisLockProvider`, caché en Redis |
| `[rabbitmq]` | aio-pika, pika | `RabbitMQEventBus` y su worker |
| `[procrastinate]` | Procrastinate | `ProcrastinateEnqueuer`, `run_procrastinate_worker` |
| `[celery]` | Celery | `CeleryEnqueuer`, `run_in_worker_loop` |
| `[all]` | Todo lo anterior más Darwin | — |

## Los extras de Darwin

Darwin, el módulo de identidad, reparte **nueve** extras. Tres son de almacenamiento y seis de
funcionalidad, y el criterio es el mismo en los nueve: **si el consumidor puede decidir no
tenerlo, es un extra**.

| Extra | Trae | Habilita |
| :-- | :-- | :-- |
| `[darwin]` | FastAPI, joserfc, argon2-cffi | El núcleo: dominio, servicios, tokens, transportes, plugins. **Sin almacenamiento.** |
| `[darwin-sqlalchemy]` | `hexcore[darwin]` + SQLAlchemy, Alembic, asyncpg, aiosqlite | Almacenamiento de identidad en SQL |
| `[darwin-beanie]` | `hexcore[darwin]` + Beanie | Almacenamiento de identidad en MongoDB |
| `[darwin-magic-link]` | `hexcore[darwin]` | Login por link de un solo uso |
| `[darwin-two-factor]` | `hexcore[darwin]` | TOTP (RFC 6238) con códigos de respaldo |
| `[darwin-oauth]` | `hexcore[darwin]` + httpx | Authorization Code + PKCE |
| `[darwin-impersonate]` | `hexcore[darwin]` | «Entrar como» otro usuario, auditado |
| `[darwin-passkey]` | `hexcore[darwin]` + webauthn | WebAuthn |
| `[darwin-organization]` | `hexcore[darwin]` | Organizaciones, miembros e invitaciones |

```sh
pip install "hexcore[darwin-sqlalchemy,darwin-two-factor]"
```

### Por qué el almacenamiento va separado

Un despliegue elige **un** backend. Quien elige Mongo no tiene por qué instalar SQLAlchemy,
Alembic y asyncpg: son ~15 MB y una superficie de cadena de suministro que no usa. Y al revés
igual.

### Por qué cada plugin tiene extra propio aunque no sume paquetes

Cuatro de los seis (`magic-link`, `two-factor`, `impersonate`, `organization`) corren con
stdlib más el núcleo, así que hoy declaran cero dependencias nuevas. El extra igual gana su
lugar por tres razones, y ninguna es cosmética:

1. **Es el nombre estable** donde una dependencia futura entra sin cambiarle el comando de
   instalación al consumidor. `[darwin-passkey]` no tenía `webauthn` hasta que lo tuvo.
2. **Hace que el comando funcione.** Cada extra de plugin arrastra `hexcore[darwin]`, así que
   `pip install 'hexcore[darwin-two-factor]'` trae el núcleo que el plugin necesita. Sin la
   autorreferencia ese comando instalaría un plugin sin núcleo: un import roto.
3. **Documenta la superficie** en el único lugar que el consumidor lee antes de instalar. Un
   plugin ausente de esa lista es un plugin que nadie encuentra.

Lo que el extra deliberadamente **no** hace es exigir un backend de almacenamiento. «Uno de
dos» no se expresa en metadata de empaquetado: un extra con los dos le instalaría SQLAlchemy a
quien eligió Mongo, que es justo lo que la separación evita. La elección se resuelve en runtime,
con un error que nombra el extra que falta.

---

## Importar sin extras

La resolución de nombres de las fachadas es **perezosa**, así que esto funciona en una
instalación pelada:

```python
import hexcore.cqrs as cqrs      # ✅ sin ningún extra
import hexcore.darwin            # ✅ no arrastra joserfc, argon2 ni sqlalchemy
```

El extra se exige en el momento exacto en que pedís el símbolo que lo necesita:

```python
repo = cqrs.SqlAlchemyCronJobRepository()   # ⛔ ModuleNotFoundError si falta [sql]
```

…y el error **trae el comando**:

```
SqlAlchemyRepository necesita 'sqlalchemy', que HexCore empaqueta en el extra [sql] y no
está instalado.

    pip install 'hexcore[sql]'
```

Ese mensaje lo produce `require_extra`, y es la razón de que exista: un
`ModuleNotFoundError: No module named 'sqlalchemy'` es correcto y no sirve —no dice que HexCore
lo empaqueta bajo `[sql]`, que es lo único que el consumidor necesita saber—.

Hay tests que lo verifican bloqueando los paquetes en `sys.meta_path`: el núcleo de Darwin
importa con los seis plugins bloqueados, y cada plugin importa con los otros cinco bloqueados.

### Preguntarlo desde tu código

```python
from hexcore.capabilities import has_extra, installed_extras, require_extra

if has_extra("redis"):
    ...

require_extra("sqlalchemy", para="MiRepositorio")
```

`has_extra` pregunta por el **nombre importable**, no por el del extra: `argon2-cffi` se importa
`argon2`, `aio-pika` se importa `aio_pika` y `py_webauthn` se importa `webauthn`. Usa
`importlib.util.find_spec` y no un `try: import`, porque importar tiene efectos —ejecuta el
módulo, lo deja en `sys.modules` y puede costar cientos de milisegundos—.

---

## Con `uv`

```sh
uv add hexcore --extra api --extra sql
```

Para trabajar sobre el repositorio:

```sh
uv sync --extra all --group dev
uv run python -m pytest -q
```

---

## Docker

El repositorio trae un `Dockerfile` de referencia. Para una app propia, lo mínimo:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir "hexcore[api,sql,procrastinate]"
COPY . .
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
```

El worker es **otro** proceso con la misma imagen y otro comando (`python worker.py`). No lo
metas en el mismo contenedor que la API: el runner del worker sale con `WorkerDied` para que el
orquestador lo reinicie, y eso reiniciaría tu API.

---

## Siguiente

→ **[Inicio rápido](./inicio-rapido.md)** — una app y un worker completos.
