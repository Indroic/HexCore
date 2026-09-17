# Almacenamiento, esquema y migraciones

Darwin no sabe en qué se guardan las cosas. El núcleo (`[darwin]`) trae dominio, servicios,
tokens, transportes y plugins, y **ningún** backend. Elegís uno:

```sh
pip install 'hexcore[darwin-sqlalchemy]'   # PostgreSQL, SQLite, MySQL
pip install 'hexcore[darwin-beanie]'       # MongoDB
```

Van separados y no en un solo extra con los dos porque un despliegue elige **uno**: quien elige
Mongo no tiene por qué instalar SQLAlchemy, Alembic y asyncpg — son ~15 MB y una superficie de
cadena de suministro que no usa.

---

## Cómo se elige

```python
IdentityConfig(storage="sqlalchemy")   # explícito
IdentityConfig(storage="beanie")
IdentityConfig()                       # se detecta
```

Cuando es `None` se detecta por lo instalado, con una regla que conviene conocer antes de que te
sorprenda: **si están los dos instalados, se niega**.

```
Hay más de un backend de almacenamiento instalado (beanie, sqlalchemy) y Darwin no
elige por vos: el que quede afuera se nota recién cuando la app arranca contra una
base vacía.

Declaralo:

    IdentityConfig(storage="sqlalchemy", ...)
```

Adivinar por una regla implícita haría que el backend dependa de qué más haya instalado, y el
síntoma —una app que arranca contra una base vacía— aparece lejos de la causa.

Si no hay ninguno, el error nombra los dos comandos.

---

## El contrato de nombre neutro

El núcleo nunca nombra un backend. Cada backend expone los **mismos nombres**:

| Nombre | Qué es |
| :-- | :-- |
| `UserRepository` | Usuarios |
| `SessionRepository` | Sesiones |
| `AccountRepository` | Cuentas vinculadas |
| `VerificationRepository` | Tokens de un solo uso |
| `AuditSink` | Registro de auditoría |
| `KeyStore` | Claves de firma, sobre `darwin_jwks` |

Y para el esquema de los plugins:

| Constante | Dónde | Qué junta |
| :-- | :-- | :-- |
| `PLUGIN_MODELS` | `orms/sqlalchemy/models.py` | Los modelos SQL del plugin |
| `PLUGIN_DOCUMENTS` | `orms/beanie/repository.py` | Los documentos de Beanie |

Es el mismo mecanismo para las dos cosas, y por el mismo motivo: quien las junta no puede
conocer de antemano los nombres propios de cada plugin.

La frontera no es una convención. Hay tests que corren en subprocesos con un finder en
`sys.meta_path` que **bloquea** paquetes: el núcleo importa con los seis plugins bloqueados, y
cada plugin importa con los otros cinco bloqueados.

---

## SQL: Alembic, y el modo de falla que no da error

> **Si usás SQL, esta sección es la más importante del módulo.**

Una tabla que existe en la base y está **ausente de `Base.metadata`** hace que el próximo
`alembic revision --autogenerate` le emita un `op.drop_table`. Con datos adentro. Es el peor
modo de falla de Darwin porque es el único que no falla: la migración se genera limpia y el
daño aparece al aplicarla.

Para que no pase, el `env.py` de Alembic tiene que cargar el esquema de identidad:

```python
# env.py — fragmento relevante
from hexcore.darwin import ensure_identity_schema_loaded

DARWIN_PLUGINS: list[str] = ["two_factor", "passkey"]   # los que uses
ensure_identity_schema_loaded(plugins=DARWIN_PLUGINS)
```

El `env.py` que genera `hexcore` ya trae esa llamada con una lista vacía y el comentario que
explica qué pasa si le falta uno.

### Por qué hay que pasar la lista a mano

Porque descubrir los plugins automáticamente sería **peor**. Los seis plugins que shippea Darwin
viven en la misma distribución, así que están todos en disco tengas el extra o no: descubrir por
presencia le crearía una tabla `darwin_passkey` a quien nunca usó passkeys.

La única fuente honesta de "qué plugins usa este despliegue" es la lista que le pasás a
`configure_identity`. En el `env.py` no hay contenedor todavía, así que ahí hay que repetirla.

### La red de contención

Repetir una lista es una fuente de olvidos, así que el arreglo no termina ahí. Al arrancar sí
hay contenedor, y ahí la lista es exacta: `IdentityStep` compara `Base.metadata` contra el
núcleo más los plugins activos, y si falta algo loguea un error que nombra **las tablas que
faltan y la lista exacta que hay que copiar al `env.py`**.

Esa es la división: la función del `env.py` no puede adivinar y por eso pregunta; el paso de
arranque no necesita adivinar y por eso verifica. Descubrir que faltaba una tabla revisando el
diff de una migración es demasiado tarde para confiar en que alguien lo revise.

### Crear y borrar sin Alembic

Para tests o scripts:

```python
from hexcore.darwin import create_identity_tables, drop_identity_tables

await create_identity_tables(engine, plugins=["two_factor"])
await drop_identity_tables(engine, plugins=["two_factor"])
```

El orden importa y ya está resuelto: `identity_tables` pone las del núcleo primero y las de los
plugins al final, y `drop` invierte la lista completa, así que las de los plugins se borran
primero — el único orden que funciona, porque referencian a `darwin_user`.

---

## Mongo: el mismo agujero, otro síntoma

```python
from hexcore.darwin.infrastructure.orms.beanie.schema import init_identity_documents

await init_identity_documents(db, plugins=["two_factor", "passkey"])
```

Acá no es una migración perdida: un `Document` que `init_beanie` no vio **no funciona**, así que
omitir un plugin activo lo deja fallando en la primera consulta con
`CollectionWasNotInitialized`.

Los plugins van en **esa misma llamada**, no en una suya. `init_beanie` no acumula: la segunda
llamada sobre la misma base reemplaza el registro de la primera, así que inicializar el núcleo y
después cada plugin dejaría funcionando sólo al último. Por eso `plugins=` es un parámetro y no
una función aparte.

---

## Tablas del núcleo

| Tabla | Qué guarda |
| :-- | :-- |
| `darwin_user` | Usuarios |
| `darwin_session` | Sesiones — con `actor_user_id` **y** `subject_user_id` |
| `darwin_account` | Cuentas vinculadas (OAuth, credenciales) |
| `darwin_verification` | Tokens de un solo uso |
| `darwin_audit_log` | Auditoría |
| `darwin_jwks` | Claves de firma |

La sesión persiste **dos** principales y no un solo `user_id`. Es lo que hace auditable la
impersonación, y la decisión es del diseño original del esquema, no del plugin que la usa: un
contexto impersonado sin los dos no se puede construir.

---

## Modelo de usuario propio

Los modelos son **mixins**, no clases mapeadas, así que podés componerlos con tu `Base` y
renombrar tablas:

```python
from hexcore.darwin import UserMixin
from hexcore.sql import Base


class Usuario(UserMixin, Base):
    __tablename__ = "usuarios"
    # tus columnas
```

Y declararlo, **si hace falta**:

```python
IdentityConfig(user_model=Usuario)
```

Casi nunca hace falta: Darwin resuelve la clase concreta de cada tabla buscando cuál de las
clases mapeadas compone el mixin correspondiente, prefiriendo la que está sobre la tabla
canónica. `IdentityConfig` acepta los seis (`user_model`, `session_model`, `account_model`,
`verification_model`, `audit_model`, `jwks_model`) para desempatar cuando mapeás dos clases
sobre el mismo mixin en tablas distintas.

Se valida **al configurar**, no en el primer login: rechaza un `BaseModel[T]` —que explotaría
después del commit— y una clase que no componga el mixin de su tabla.

⚠️ **Por qué la resolución no es simplemente "importar `models.py`".** Ese módulo declara las
seis clases concretas sobre `Base`, así que importarlo para obtener una sola declara también
las otras cinco. Si ya declaraste la tuya sobre `darwin_user`, son dos clases peleando por la
misma tabla y SQLAlchemy corta con `InvalidRequestError: Table 'darwin_user' is already
defined for this MetaData instance` — al primer uso de cualquier repositorio, no al declarar el
modelo, así que el stack trace apunta a una consulta de sesiones. La sugerencia que trae el
error, `extend_existing=True`, es peor: haría que la última clase declarada le pise las
columnas a la primera según el orden de importación.

---

## Migrar a 10.0

Dos cambios de esquema, y el de Mongo es el que muerde.

**SQL.** `darwin_user.email` pasa a nullable y aparecen `darwin_user.username` y
`darwin_session.scopes`. El próximo `alembic revision --autogenerate` los emite solo:

```python
op.alter_column("darwin_user", "email", existing_type=sa.String(320), nullable=True)
op.add_column("darwin_user", sa.Column("username", sa.String(64), nullable=True))
op.create_unique_constraint("uq_darwin_user_username", "darwin_user", ["username"])
op.add_column("darwin_session", sa.Column("scopes", sa.JSON(), nullable=False, server_default="[]"))
```

**Mongo.** Los índices únicos de `email` y `username` tienen que ser **`sparse`**, y el de
`email` que viene de 9.x **no lo es**. Un índice único no-sparse trata todos los documentos sin
el campo como si compartieran el mismo `null`, así que el segundo usuario sin mail falla con
`DuplicateKeyError` al insertar. Beanie **no reconstruye un índice que ya existe**, así que hay
que borrarlo a mano antes de levantar:

```js
db.darwin_user.dropIndex("email_1")
```

En el próximo arranque, `init_beanie` lo vuelve a crear con `sparse: true`.

---

## Las claves de firma se persisten

`darwin_jwks` la lee `KeyStore`, que el contenedor resuelve según el backend. El default sólo
cae a `StaticKeyStore` —claves en memoria, generadas al arrancar— cuando no hay backend
resoluble, y `IdentityStep` **se niega a arrancar** con eso si `debug=False`.

Sembrá la primera clave:

```bash
hexcore identity generate-keys --persist
```

Con claves efímeras, un reload invalida todas las sesiones y, con más de un proceso, cada
worker firma con una clave que los otros no verifican: el síntoma es un 401 intermitente que no
se reproduce en desarrollo, donde hay un solo proceso.

Por el mismo motivo, `IdentityStep` tampoco arranca en producción con el `MemoryCache` por
defecto: la denylist de sesiones y el contador de generación viven ahí, y un cache en memoria es
**por proceso**, así que un `sign-out` corta la sesión en el worker que atendió el pedido y la
deja viva en todos los demás.

---

## Limitación conocida

En Mongo, la auditoría no es transaccional con la operación que audita. En SQL las dos van en la
misma transacción; en Mongo haría falta una transacción multi-documento, que exige replica set.
Si tu despliegue lo tiene, podés inyectar un `AbstractAuditSink` propio que la use.

---

## Ver también

- [Escribir un plugin propio](./plugins-propios.md) — incluida la parte de almacenamiento
- [Los seis plugins incluidos](./plugins-incluidos.md)
