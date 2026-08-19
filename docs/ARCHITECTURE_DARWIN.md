# Darwin — Módulo de Identidad Nativo de HexCore

> Documento técnico de arquitectura. Port deliberado de [Better Auth](https://github.com/better-auth/better-auth)
> (TypeScript) a Python + CQRS sobre HexCore 6.x.
>
> Estado: **diseño aprobado, Fase 0 implementada.** Sin fechas ni estimaciones: el orden es
> por dependencia, no por calendario.

---

## Índice

1. [Propuesta de naming](#1-propuesta-de-naming)
2. [Arquitectura de datos](#2-arquitectura-de-datos)
3. [`AuthContext`: Actor vs Subject](#3-authcontext-actor-vs-subject)
4. [Extensibilidad del modelo de usuario](#4-extensibilidad-del-modelo-de-usuario)
5. [Sistema de plugins](#5-sistema-de-plugins)
6. [JWT + base de datos híbrido](#6-jwt--base-de-datos-híbrido)
7. [Dualidad de transporte](#7-dualidad-de-transporte)
8. [Plan de desarrollo](#8-plan-de-desarrollo)
9. [Cambios en archivos existentes](#9-cambios-en-archivos-existentes)
10. [Apéndice: hallazgos de seguridad preexistentes](#apéndice-hallazgos-de-seguridad-preexistentes)

---

## 1. Propuesta de naming

"Auth Manager" es genérico y no dice nada. Se evaluaron tres alternativas:

| | **Darwin** ✅ | **Keystone** | **Sigil** |
| :-- | :-- | :-- | :-- |
| **Concepto** | Evolución. El módulo **evoluciona** vía plugins: el core es mínimo y las capacidades (2FA, OAuth, Magic Link, Impersonate, Passkey) se seleccionan según el entorno de cada app. Selección natural de estrategias de autenticación. | La piedra clave del arco: la pieza que traba todas las demás. Identidad es lo que traba los puertos del hexágono — todo adaptador necesita saber quién llama. | Un sello que identifica y autentica a la vez a quien lo porta. HexCore → hex → hexagrama → sigil; y el acto central del módulo es literalmente firmar y sellar tokens. |
| **Paquete** | `hexcore/darwin/` | `hexcore/keystone/` | `hexcore/sigil/` |
| **Fachada** | `hexcore/darwin.py` | `hexcore/keystone.py` | `hexcore/sigil.py` |
| **Campo en `ServerConfig`** | `darwin: t.Any = None` | `keystone` | `sigil` |
| **Prefijo de tablas** | `darwin_` | `keystone_` | `sigil_` |
| **Extra de pip** | `[darwin]` | `[keystone]` | `[sigil]` |
| **Costo** | Ninguno conocido. Sin colisión en PyPI ni en el ecosistema Python. | **OpenStack Keystone es un servicio de identidad.** Toda búsqueda, todo resultado de Stack Overflow y todo autocompletado de LLM va a ser sobre OpenStack. Fatal para la discoverability. | El menos autodescriptivo: quien escanea la lista de paquetes no aprende que `sigil` es auth. |

> **Estado de implementación.** Fases 0, 1, 2, 3 y 4 completas. Siguen: aplicación y
> contenedor (5), propagación del actor (6), borde HTTP (7), plugins (8-9), kit de testing
> (10).

**Elegido: `Darwin`.** Es el único de los tres que es simultáneamente (a) inequívoco sobre
qué hace, (b) libre de colisiones, y (c) lo bastante corto para que `ServerConfig.darwin`,
`darwin_user`, `DarwinPlugin` y `hexcore.darwin` se lean naturalmente en todas las capas.
La metáfora de Keystone encaja mejor con "hexagonal", pero cederle la superficie de búsqueda
a OpenStack es un mal negocio para una librería cuyo lema es "un import obvio por tarea".

**Prefijo de tablas: `darwin_`.** Se consideró `hexcore_`, siguiendo el precedente literal
de `DEFAULT_TABLE_NAME = "hexcore_cron_jobs"` — HexCore namespacea por framework, no por
subsistema. Se eligió `darwin_` porque las tablas de identidad son las únicas del framework
que el consumidor va a *ver y referenciar* constantemente (FKs desde sus propias tablas,
JOINs, queries a mano), y `darwin_user` comunica de dónde sale mucho mejor que
`hexcore_user`. La inconsistencia con `hexcore_cron_jobs` es real y se acepta: es una tabla
interna que nadie referencia.

Nombres concretos: `darwin_user`, `darwin_session`, `darwin_account`,
`darwin_verification`, `darwin_audit_log`, `darwin_jwks`. Plugins: `darwin_two_factor`,
`darwin_passkey`, `darwin_organization`, `darwin_member`, `darwin_invitation`.

---

## 2. Arquitectura de datos

### 2.1 Las dos reglas no negociables

Todo modelo de identidad las cumple, y hay tests que lo verifican. El precedente es
[`hexcore/infrastructure/cqrs/cron_sql.py`](../hexcore/infrastructure/cqrs/cron_sql.py), el
único caso en el árbol de una tabla propia del framework.

**Regla 1 — los modelos NO heredan `BaseModel[T]`.**

`BaseModel[T].get_domain_entity()` devuelve `self._domain_entity` **sin default**
([`orms/sqlalchemy/__init__.py:40-41`](../hexcore/infrastructure/repositories/orms/sqlalchemy/__init__.py#L40-L41)).
`SqlAlchemyUnitOfWork.collect_domain_entities()` lo llama para **todo** `BaseModel` que la
sesión tenga trackeado ([`uow/__init__.py:98-102`](../hexcore/infrastructure/uow/__init__.py#L98-L102)).

Escenario de fallo concreto, y es por petición, no un caso borde:

```
handler de login: uow.session.add(SessionModel(...))   # sin set_domain_entity()
uow.commit()
  └─ await self.session.commit()        ✅ la fila YA está persistida
  └─ await self.dispatch_events()
       └─ collect_domain_entities()
            └─ model.get_domain_entity()  ✗ AttributeError: '_domain_entity'
```

`commit()` no rollbackea (el `try/finally` sólo hace `clear_tracked_entities()`) y
`__aexit__` tampoco ([`uow/__init__.py:63-73`](../hexcore/infrastructure/uow/__init__.py#L63-L73)).
Resultado: **sesión creada en la base, 500 al usuario.** El usuario ve un login fallido
estando efectivamente logueado.

**Regla 2 — los repositorios NO heredan `BaseSQLAlchemyRepository`.**

`_repository_key_from_class_name()` saca el sufijo `repository|repo` y baja a minúsculas, así
que `UserRepository` → clave `user`, y una colisión **levanta `ValueError`**
([`repositories/utils.py:315-319`](../hexcore/infrastructure/repositories/utils.py#L315-L319)).
Un `UserRepository` shippeado por el framework rompería el UoW de **todo** consumidor que
tenga el suyo — que es prácticamente todos.

En su lugar: puertos `Abstract*` propios, adaptadores que abren su propia sesión por
operación vía `session_scope`, y `model=` inyectable. Exactamente `SqlAlchemyCronJobRepository`.

Tests que lo fijan (espejan [`test_cron_sql.py:66-90`](../tests/test_cron_sql.py#L66-L90)):

```python
def test_los_modelos_no_heredan_basemodel():
    assert not issubclass(UserModel, BaseModel)
    assert issubclass(UserModel, Base)

def test_los_repositorios_no_son_autodescubribles():
    assert not issubclass(SqlAlchemyUserRepository, BaseSQLAlchemyRepository)
    assert SqlAlchemyUserRepository not in _todas_las_subclases(BaseSQLAlchemyRepository)

def test_un_userrepository_de_la_app_no_colisiona():
    """El test que prueba que no le rompimos el UoW a nadie."""
    class UserRepository(BaseSQLAlchemyRepository): ...   # el de la app
    assert discover_sql_repositories()["user"] is UserRepository   # y no levanta
```

### 2.2 Mixin-first, para que el consumidor pueda renombrar

Cada tabla es un mixin sin `__tablename__` más una clase concreta que lo aplica. Es lo que
permite renombrar sin forkear, y es requisito para la estrategia de extensibilidad (§4).

```python
# hexcore/darwin/infrastructure/models_mixins.py
#
# Sin `__tablename__` y sin heredar de `Base`: importar este módulo NO agrega nada a
# `Base.metadata`. Es lo que permite que el consumidor declare sus propias tablas
# concretas sin que las nuestras se registren de prepo.

class UserModelMixin:
    """Columnas de `user`, portadas de Better Auth."""

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    email_verified: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    name: Mapped[str | None] = mapped_column(String(255))
    image: Mapped[str | None] = mapped_column(String(2048))

    # Fuera de Better Auth, y necesario: revocación masiva en un solo UPDATE (§6.4).
    token_generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Paridad con `additionalFields`. Escalares no consultados, sin constraints.
    extra: Mapped[dict] = mapped_column(_JSON_PORTABLE, nullable=False, default=dict)

    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    @declared_attr.directive
    def __table_args__(cls) -> tuple:
        # `@declared_attr` y no un atributo: los índices tienen que derivar del
        # `__tablename__` de la clase concreta, que el mixin todavía no conoce. Un
        # `Index("ix_darwin_user_email", ...)` fijo rompería en cuanto alguien renombre.
        return (
            UniqueConstraint("email", name=f"uq_{cls.__tablename__}_email"),
            Index(f"ix_{cls.__tablename__}_created_at", "created_at"),
        )


class UserModel(UserModelMixin, Base):
    __tablename__ = DEFAULT_USER_TABLE   # "darwin_user"
```

`_JSON_PORTABLE` es `JSON().with_variant(JSONB(), "postgresql")`: `JSONB` a secas rompe la
suite, que corre sobre el fixture `sqlite_engine` de
[`hexcore/testing/fixtures.py`](../hexcore/testing/fixtures.py).

### 2.3 `session`: dos principales, no uno

Esta es la decisión que hace auditable la impersonación, y es un desvío deliberado de Better
Auth (que tiene `userId` + un `impersonatedBy` opcional).

```python
class SessionModelMixin:
    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)

    # Better Auth tiene un solo `userId`. Acá hay DOS, y ninguno es opcional:
    #   actor   = la persona física que ejecuta. Nunca se deduce, nunca se hereda.
    #   subject = la cuenta afectada. En una sesión normal, es el mismo actor.
    # Con un solo id, la impersonación es indistinguible del uso normal: la fila queda
    # escrita "por" la víctima y el soporte que la escribió desaparece del registro.
    actor_user_id: Mapped[UUID] = mapped_column(ForeignKey(...), nullable=False)
    subject_user_id: Mapped[UUID] = mapped_column(ForeignKey(...), nullable=False)

    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)   # §2.4
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    ip_address: Mapped[str | None] = mapped_column(String(45))
    user_agent: Mapped[str | None] = mapped_column(String(1024))

    # De qué transporte nació. Atado al token (§7) para que una cookie no se pueda
    # replayear como Bearer esquivando CSRF.
    transport: Mapped[str] = mapped_column(String(16), nullable=False)

    # Rotación de refresh con detección de reuso (§6.5): las sesiones rotadas comparten
    # familia, y reusar una consumida revoca la familia entera.
    family_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
```

### 2.4 La columna deliberadamente controvertida: `token_hash`

Better Auth guarda `session.token` en claro. Acá se guarda **SHA-256 del token**.

Razón: un dump de la tabla `session` de Better Auth es un set de credenciales de sesión
utilizables. Con hash, no. El costo es que no se puede hacer `WHERE token = ?` para buscar
por token — pero no hace falta: el token que se presenta se hashea y se busca por el hash,
que está indexado. Es la misma operación con el mismo costo.

No se usa Argon2 acá: el token es aleatorio de 256 bits, no una contraseña. No hay que
defenderse de un ataque de diccionario, así que SHA-256 alcanza y es tres órdenes de
magnitud más rápido en el camino caliente.

### 2.5 Inventario de índices y constraints

| Tabla | Constraint / Índice | Por qué |
| :-- | :-- | :-- |
| `darwin_user` | `UNIQUE (email)` | Sin esto, dos signups concurrentes con el mismo mail crean dos cuentas. La unicidad va en la base, no en el handler. |
| | `INDEX (created_at)` | Paginación del panel de admin. |
| `darwin_session` | `INDEX (token_hash)` UNIQUE | Camino de verificación. Único porque un token no puede pertenecer a dos sesiones. |
| | `INDEX (subject_user_id, revoked_at)` | "listar mis sesiones" y "cerrar todas". |
| | `INDEX (actor_user_id)` | Auditoría: "qué hizo este operador". |
| | `INDEX (expires_at)` | El cron reaper barre por vencimiento. |
| | `INDEX (family_id)` | Revocación de familia ante reuso de refresh. |
| | `FK (actor_user_id) → user.id ON DELETE CASCADE` | |
| | `FK (subject_user_id) → user.id ON DELETE CASCADE` | |
| `darwin_account` | `UNIQUE (provider_id, account_id)` | La misma cuenta de Google no se puede linkear dos veces. Es el constraint que hace segura la vinculación OAuth. |
| | `INDEX (user_id)` | "con qué proveedores entra este usuario". |
| `darwin_verification` | `INDEX (identifier)` | Búsqueda por mail/teléfono en el flujo de verificación. |
| | `INDEX (expires_at)` | Barrido del reaper. |
| | `UNIQUE (identifier, purpose)` parcial `WHERE consumed_at IS NULL` | Un solo token vivo por propósito e identificador. Evita que 50 clicks en "reenviar" dejen 50 tokens válidos. |
| `darwin_audit_log` | `INDEX (actor_user_id, occurred_at)` | La consulta de auditoría real: "qué hizo X y cuándo". |
| | `INDEX (subject_user_id, occurred_at)` | "qué le pasó a la cuenta de Y". |

### 2.6 Convención de nombres de constraints

`Base.metadata.naming_convention` hoy tiene **sólo** `{"ix": "ix_%(column_0_label)s"}`. Sin
`uq`/`fk`/`pk`/`ck`, las migraciones autogeneradas llevan nombres que asigna el backend:
SQLite no los puede dropear y difieren entre dev y prod.

Darwin necesita uniques y FKs, así que **necesita** la convención completa. Y agregarla
después es en sí una migración rompedora (hay que renombrar todo constraint existente), así
que va **antes** de la primera tabla de identidad:

```python
NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
```

### 2.7 DBML

```dbml
Table darwin_user {
  id uuid [pk]
  email varchar(320) [not null, unique]
  email_verified boolean [not null, default: false]
  name varchar(255)
  image varchar(2048)
  token_generation integer [not null, default: 0, note: 'revocación masiva en un UPDATE']
  extra jsonb [not null, default: `{}`]
  is_active boolean [not null, default: true]
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  indexes { created_at }
}

Table darwin_session {
  id uuid [pk]
  actor_user_id uuid [not null, ref: > darwin_user.id, note: 'quién EJECUTA']
  subject_user_id uuid [not null, ref: > darwin_user.id, note: 'a quién AFECTA']
  token_hash varchar(64) [not null, unique, note: 'SHA-256, nunca el token en claro']
  family_id uuid [not null, note: 'linaje de rotación de refresh']
  transport varchar(16) [not null, note: 'cookie | bearer']
  expires_at timestamptz [not null]
  revoked_at timestamptz
  ip_address varchar(45)
  user_agent varchar(1024)
  created_at timestamptz [not null]
  indexes {
    (subject_user_id, revoked_at)
    actor_user_id
    expires_at
    family_id
  }
}

Table darwin_account {
  id uuid [pk]
  user_id uuid [not null, ref: > darwin_user.id]
  provider_id varchar(64) [not null]
  account_id varchar(255) [not null]
  password varchar(255) [note: 'Argon2id; sólo para el provider "credential"']
  access_token text [note: 'cifrado en reposo']
  refresh_token text [note: 'cifrado en reposo']
  id_token text
  scope varchar(1024)
  access_token_expires_at timestamptz
  refresh_token_expires_at timestamptz
  created_at timestamptz [not null]
  updated_at timestamptz [not null]
  indexes {
    (provider_id, account_id) [unique]
    user_id
  }
}

Table darwin_verification {
  id uuid [pk]
  identifier varchar(320) [not null]
  value_hash varchar(64) [not null, note: 'hash, no el código en claro']
  purpose varchar(32) [not null, note: 'email | password_reset | magic_link | otp']
  expires_at timestamptz [not null]
  consumed_at timestamptz [note: 'uso único: se marca al consumir']
  attempts integer [not null, default: 0, note: 'techo de fuerza bruta del OTP']
  created_at timestamptz [not null]
  indexes { identifier, expires_at }
}

Table darwin_audit_log {
  id uuid [pk]
  actor_user_id uuid [ref: > darwin_user.id]
  subject_user_id uuid [ref: > darwin_user.id]
  action varchar(64) [not null]
  impersonated boolean [not null, default: false]
  request_id varchar(64) [note: 'correlación con el REQUEST_ID de la capa HTTP']
  metadata jsonb [not null, default: `{}`]
  occurred_at timestamptz [not null]
  indexes {
    (actor_user_id, occurred_at)
    (subject_user_id, occurred_at)
  }
}

Table darwin_jwks {
  kid varchar(64) [pk]
  algorithm varchar(16) [not null]
  public_key text [not null]
  private_key text [not null, note: 'cifrado en reposo']
  status varchar(16) [not null, note: 'active | verify_only | retired']
  created_at timestamptz [not null]
  retired_at timestamptz
}
```

### 2.8 Creación de tablas y Alembic

Copia el patrón de `create_cron_tables`, incluido el `op.create_table` equivalente en el
docstring:

```python
async def create_identity_tables(
    engine: AsyncEngine | None = None,
    *,
    models: t.Sequence[type] | None = None,
) -> None:
    """
    Crea las tablas de Darwin. Idempotente (`checkfirst=True`).

    Atajo para desarrollo y tests. **En producción usá Alembic.** El equivalente:

        op.create_table(
            "darwin_user",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("email", sa.String(320), nullable=False),
            ...
            sa.PrimaryKeyConstraint("id", name="pk_darwin_user"),
            sa.UniqueConstraint("email", name="uq_darwin_user_email"),
        )
        ...

    Uso::

        await create_identity_tables()
    """
```

⚠️ **El riesgo de Alembic está documentado en §5.3 y es la trampa más grave del diseño.**

---

## 3. `AuthContext`: Actor vs Subject

### 3.1 La restricción de la que sale todo

HexCore no tiene contexto de request. Concretamente:

| Lo que Better Auth asume | La realidad de HexCore |
| :-- | :-- |
| Un `ctx` request-scoped que se pasa a todo handler y hook | `AbstractCommandHandler.handle(command)` — **un solo argumento** |
| Middlewares que reciben y mutan `ctx` | `AbstractMiddleware.handle(message, next_handler)` — **sin parámetro de contexto** |
| Un solo proceso, `ctx` vive todo el request | El bus cruza procesos: el serializer sólo lleva payload + `__type__` |

Cambiar `AbstractMiddleware.handle` rompe `MiddlewarePipeline`, los 4 middlewares que se
shippean, los 3 buses in-memory, rabbitmq/postgres/redis y `tests/test_cqrs_middlewares.py`.
No se toca.

**La salida: un `ContextVar`,** que es el mecanismo que
[`domain/cqrs/context.py`](../hexcore/domain/cqrs/context.py) ya establece para `IN_WORKER`,
y que [`RequestIDMiddleware`](../hexcore/infrastructure/api/middlewares.py#L47) ya usa con la
doble publicación ContextVar + `request.state`.

### 3.2 El invariante que hace imposible la impersonación no auditable

```python
Transport = t.Literal["cookie", "bearer", "internal", "worker"]


class Principal(BaseModel):
    """Un sujeto identificado. Inmutable."""
    model_config = ConfigDict(frozen=True)

    user_id: UUID
    session_id: UUID | None = None
    email: str | None = None
    roles: frozenset[str] = frozenset()
    scopes: frozenset[str] = frozenset()


class Impersonation(BaseModel):
    """El permiso explícito que hace que actor != subject sea legítimo."""
    model_config = ConfigDict(frozen=True)

    granted_by: UUID
    reason: str
    granted_at: datetime
    expires_at: datetime


class AuthContext(BaseModel, t.Generic[TUser]):
    """
    Quién ejecuta vs a quién afecta.

    El invariante lo garantiza un validador, no la disciplina: si `subject` difiere de
    `actor`, `impersonation` es **obligatorio**. Un contexto impersonado no auditable
    **no se puede construir**. Eso es el "sin magia negra" del requerimiento: no hay
    ningún camino donde alguien actúe como otro y el sistema no lo sepa.
    """
    model_config = ConfigDict(frozen=True)

    actor: Principal
    subject: Principal
    transport: Transport
    impersonation: Impersonation | None = None
    user: TUser | None = None          # el modelo extendido de la app (§4)

    @model_validator(mode="after")
    def _la_impersonacion_es_auditable(self) -> "AuthContext[TUser]":
        distintos = self.actor.user_id != self.subject.user_id
        if distintos and self.impersonation is None:
            raise ValueError(
                "subject difiere de actor sin un permiso de impersonación. Un contexto "
                "impersonado tiene que ser auditable por construcción: pasá "
                "`impersonation=Impersonation(granted_by=..., reason=...)`."
            )
        if not distintos and self.impersonation is not None:
            raise ValueError(
                "hay permiso de impersonación pero actor y subject son el mismo. Eso "
                "ensuciaría la auditoría con impersonaciones que nunca pasaron."
            )
        return self
```

Publicación ambiental, calcada de `IN_WORKER`:

```python
AUTH_CONTEXT: ContextVar[AuthContext | None] = ContextVar("darwin_auth", default=None)


def current_auth() -> AuthContext | None:
    """El contexto en curso, o `None` fuera de uno. Nunca lanza."""
    return AUTH_CONTEXT.get()


def require_auth() -> AuthContext:
    """Igual, pero lanza `UnauthenticatedError` si no hay. Para handlers."""
    ...


@contextmanager
def auth_scope(context: AuthContext) -> t.Iterator[None]:
    token = AUTH_CONTEXT.set(context)
    try:
        yield
    finally:
        AUTH_CONTEXT.reset(token)
```

### 3.3 Cruzar la cola: sobre firmado en el serializer

**Decisión: sobre en el serializer, firmado y atado al mensaje.** Se descartó la alternativa
de una subclase `AuthenticatedCommand(Command)` con un campo `auth`.

Por qué el sobre y no la subclase:

- La subclase **no puede** llevar el actor en `@background_handler` (eventos) ni en
  `@background_task` (funciones sueltas). Habría que hacer el sobre igual, más adelante, y
  `AuthenticatedCommand` quedaría como API pública a deprecar — exactamente el error que
  este repo ya cometió con `REMOVED_IN = "6.0"` shippeado en 6.0.0.
- El costo real del sobre es chico y **aditivo**. Verificado: los 5 transportes tratan el
  payload como opaco (`procrastinate_adapter.py:36`, `celery_adapter.py:133`,
  `postgres_bus.py:58-63`, `redis_bus.py:60-63`, `rabbitmq.py:88`). Los únicos lectores de
  estructura son `PydanticSerializer` y dos peeks de `__type__`.

Los dos métodos nuevos son **concretos** en el ABC, así que ninguna implementación existente
de `AbstractSerializer` se rompe:

```python
class AbstractSerializer(abc.ABC):
    @abc.abstractmethod
    def serialize(self, message: t.Any) -> dict: ...      # sin tocar

    # Concretos: agregar un abstractmethod rompería a todo el que tenga un serializer propio.
    def serialize_envelope(self, message: t.Any, metadata: t.Mapping | None = None) -> dict:
        payload = self.serialize(message)
        if metadata:
            payload["__meta__"] = dict(metadata)
        return payload

    def deserialize_envelope(self, data: dict) -> tuple[t.Any, dict]:
        metadata = data.get("__meta__") or {}
        # Un payload viejo SIN `__meta__` tiene que seguir deserializando: es la razón de
        # que estos métodos sean concretos.
        return self.deserialize({k: v for k, v in data.items() if k != "__meta__"}), metadata
```

Payload resultante:

```json
{
  "__type__": "app.commands.ChargeInvoice",
  "__data__": {"invoice_id": "..."},
  "__meta__": {"auth": "<b64url(json)>.<b64url(hmac_sha256)>"}
}
```

**El detalle crítico: el grant va atado al mensaje.** El payload firmado incluye
`cid = command_id` (que todo `Command` ya tiene) y `mt = __type__`. La verificación rechaza
si no coinciden.

Sin ese binding, el ataque es: capturar el sobre de un `DeleteAccount` legítimo y
re-adjuntarlo a un `TransferFunds`. El sobre verifica —está bien firmado— y el worker
ejecuta la transferencia con la autoridad del grant de borrado. Es escalación de privilegios
a un `LPUSH` de distancia.

**El worker re-valida contra la base.** Verificar la firma y el `exp` no alcanza: entre el
encolado y la ejecución la sesión puede haberse revocado. El consumer chequea que la fila de
`session` esté viva. Un TTL de 24 h en el sobre sin este chequeo son 24 h de ejecución con
una sesión revocada.

```python
# hexcore/infrastructure/workers/consumer.py
message, metadata = self._serializer.deserialize_envelope(payload)
context = self._auth_codec.verify(metadata.get("auth"), message)  # firma + cid + mt + exp
await self._sessions.assert_live(context.actor.session_id)        # y la fila de session
with auth_scope(context), worker_execution():
    await self._command_bus.dispatch(message)
```

⚠️ **Trampa verificada, y hay un test que la fija.** No se puede ramificar sobre
`is_worker_execution()` dentro de un middleware: `InMemoryCommandBus.dispatch` envuelve el
pipeline en `local_execution()`
([`in_memory_buses.py:85`](../hexcore/application/cqrs/in_memory_buses.py#L85)), que pone
`IN_WORKER=False` **antes** de que corra cualquier middleware. Dentro de `handle()` el flag
es siempre `False`. La regla correcta es **"¿hay contexto ambiental?"**, que además es
independiente del orden y funciona igual en los cinco buses.

---

## 4. Extensibilidad del modelo de usuario

### 4.1 Las cuatro opciones

| | Mecanismo | FK desde las tablas de la app | Indexable | Alembic | Type safety |
| :-- | :-- | :-- | :-- | :-- | :-- |
| **1. Mixin abstracto** | La app declara el `User` concreto con sus columnas | ✅ trivial | ✅ | nativo | `Mapped[...]` completo |
| **2. JSONB `extra`** | Claves en una columna | ❌ | sólo GIN | invisible | ninguna |
| **3. Perfil unido** | La app tiene su `app_user_profile(user_id PK FK)` | ✅ | ✅ | nativo | completo |
| **4. Híbrido** ✅ | 1 + 3 + 2 según la clase de dato | — | — | — | — |

### 4.2 El híbrido, como procedimiento de decisión

- **Columnas que los flujos de auth leen → opción 1 (mixin).** `username`, `phone_number`,
  `locale`, `tenant_id`, `banned_until`. Necesitan `NOT NULL`, uniques e índices, y las
  queries del propio framework pueden filtrar por ellas.
- **Datos de negocio → opción 3 (perfil unido). Éste es el consejo por defecto.** Plan de
  facturación, avatares, preferencias, estado de onboarding. Mantiene angosta la tabla que
  está en el camino caliente de cada refresh, y mantiene *tus* migraciones afuera de *la
  tabla del framework*. Es lo que la mayoría de los frameworks recomienda mal, empujando a
  un `User` de 60 columnas.
- **Flags escalares y paridad con `additionalFields` → opción 2 (`user.extra`)**.
  `two_factor_enabled`, `has_seen_tour`. Sin constraints, sin queries.

### 4.3 Cómo el framework se mantiene genérico

Contenedor resuelto una vez + inyección `model=`, con la forma exacta de `CQRSContainer`
(`RLock`, propiedades perezosas cacheadas, `configure_*`/`get_*`/`reset_*`, `RuntimeError`
con remediación copiable, y `provide_*` triviales que existen sólo para que los tests hagan
`app.dependency_overrides[provide_x] = ...`):

```python
class IdentityConfig(BaseModel):
    """
    Configuración de Darwin. Se cuelga de `ServerConfig.darwin`.

    Mismo precedente que `ServerConfig.cqrs`: campo opcional, `None` = deshabilitado, sin
    impacto en los módulos existentes.
    """
    user_model: type = UserModel          # el concreto de la app, si lo extendió
    session_model: type = SessionModel
    ...
```

Validación al arrancar, en el estilo de `_assert_enqueuer_for_background_commands`:

```python
def _validar_user_model(model: type) -> None:
    """
    Falla al **construir**, no en el primer login.

    Rechaza: heredar de `BaseModel[T]` (§2.1 regla 1), no heredar del mixin, y declarar
    dos tablas de usuario. El error trae el reemplazo copiable.
    """
```

### 4.4 Personalización del `AuthContext`

Las dos cosas, porque resuelven problemas distintos:

- **Hook resolver** para inyectar datos: `IdentityConfig.context_resolver` recibe el
  `AuthContext` base y devuelve uno enriquecido. Es lo que permite que un Command Handler
  reciba el usuario extendido sin que el framework sepa nada de la app.
- **Subclase tipada con parámetro genérico** para que Pyright lo sepa:
  `AuthContext[MiUsuario]` hace que `require_auth().user` tipe `MiUsuario | None` y no `Any`.
  Sin esto la personalización funciona pero no se ve en el IDE, que es la mitad del valor.

```python
class MiContexto(AuthContext[MiUsuario]):
    tenant: Tenant

config = IdentityConfig(
    user_model=MiUsuario,
    context_class=MiContexto,
    context_resolver=cargar_tenant,     # async (AuthContext) -> MiContexto
)
```

---

## 5. Sistema de plugins

### 5.1 Cada aporte se compone con algo que HexCore ya tiene

No es un universo paralelo. Es la regla de diseño del módulo.

| Better Auth | Equivalente en Darwin | Reusa |
| :-- | :-- | :-- |
| `schema: { table: { fields } }` | `tables()` → mixins que **el consumidor** declara (§5.3) | `Base`, el patrón mixin de `cron_sql` |
| `endpoints: {...}` | `routers()` → `Sequence[MountableRouter]` | `MountableRouter`, `mount_routers`, `build_root_router` |
| `hooks: { before, after }` | `hooks()` → `Sequence[HookBinding]` | la cadena `AbstractMiddleware` |
| `middlewares: [...]` | `middlewares()` (CQRS) + `http_middlewares()` (Starlette) | `MiddlewarePipeline`, `BaseHTTPMiddleware` |
| `init()` | `startup_steps()` → `Sequence[StartupStep]` | `StartupStep`, `build_lifespan` |
| `rateLimit: [...]` | `rate_limits()` → reglas aplicadas como `dependencies` de ruta | `rate_limit`, `forwarded_ip_key` |
| `$Infer` | ❌ **rechazado** | — |

### 5.2 Lo que se rechaza de Better Auth, y por qué

Cada uno de estos cambia un error de compilación en el archivo del consumidor por una
sorpresa en runtime dentro del framework:

- **Schema contribuido por el plugin** → §5.3.
- **`$Infer` / inferencia de tipos del cliente** → no tiene equivalente en Python. Se
  reemplaza por modelos pydantic de respuesta en los routers.
- **Registro de plugins con lifecycle propio** → sería un cuarto mecanismo de extensión al
  lado de `AbstractMiddleware` + `StartupStep` + `DomainEvent`, y nadie sabría cuál usar.
- **Hooks que mutan un `ctx`** → los hooks son funciones puras que devuelven un reemplazo.
  `ValidationMiddleware` ya sienta el precedente de pasar una instancia reconstruida a
  `next_handler` ([`middlewares.py:135-140`](../hexcore/infrastructure/cqrs/middlewares.py#L135-L140)).
- **Discovery por entry-points** → registro explícito. Un plugin que se activa por estar
  instalado es un plugin que nadie puede desactivar.

### 5.3 ⚠️ La trampa de Alembic: el diseño DROPEA tus tablas

**Esto ya pasa hoy con `hexcore_cron_jobs`. No es hipotético.**

Mecanismo, verificado:

1. `import_all_models` usa `pkgutil.iter_modules`, **no** `walk_packages`
   ([`utils.py:428-430`](../hexcore/infrastructure/repositories/orms/sqlalchemy/utils.py#L428-L430)),
   así que los subpaquetes anidados nunca se importan.
2. `cli._setup_alembic` parchea el `env.py` para llamarlo sobre **el paquete `models` del
   consumidor solamente**, y setea `target_metadata = Base.metadata`.
3. Nada importa las tablas de HexCore.

Escenario de fallo:

```
CronSeedStep(create_tables=True)   -> hexcore_cron_jobs existe en la base
CronJobModel nunca fue importado   -> ausente de Base.metadata
alembic revision --autogenerate    -> op.drop_table("hexcore_cron_jobs")
```

Con Darwin eso es `DROP TABLE darwin_user, darwin_session, darwin_account, darwin_verification`:
**todo el almacén de identidad, borrado por una migración de rutina, en silencio.**

**La solución convierte un problema irresoluble de Alembic en una línea de código del
consumidor:** los plugins shippean **sólo mixins**. El consumidor declara la clase concreta
**en su propio paquete `models/`**, así que `import_all_models` la ve, `Base.metadata` queda
completa y el autogenerate es correcto.

```python
# myapp/models/identity.py  — en el paquete del consumidor, no en el framework
from hexcore.darwin.models import UserModelMixin, TwoFactorMixin
from hexcore.sql import Base

class User(UserModelMixin, Base):
    __tablename__ = "darwin_user"
    plan: Mapped[str] = mapped_column(String(32), default="free")   # extensión propia

class TwoFactor(TwoFactorMixin, Base):
    __tablename__ = "darwin_two_factor"
```

Y resuelve de paso la colisión de columnas entre plugins: dos mixins que chocan dan un error
de MRO de Python **al importar**, en el archivo del consumidor, con el nombre de su clase en
el traceback. La alternativa (mutar un mixin compartido con `setattr`) hace que la segunda
escritura gane en silencio y que el diff de la migración dependa del orden de importación,
que `pkgutil.iter_modules` no garantiza entre sistemas de archivos.

Se shippea además un `ensure_identity_schema_loaded()` para agregar al `env.py` generado, y
un chequeo de arranque que avisa si una tabla de Darwin no está en `Base.metadata`.

### 5.4 Los eventos se emiten como hojas concretas

`InMemoryEventBus.publish` despacha por **clase exacta**
(`self._handlers.get(type(event), [])`, [`in_memory_buses.py:144`](../hexcore/application/cqrs/in_memory_buses.py#L144)):
no hay recorrido de MRO, así que suscribirse a una clase base **no recibe nada**.

Consecuencia de diseño: **no** se shippea un `AuthEvent` base invitando a suscribirse. Se
emiten hojas concretas (`UserSignedInEvent`, `SessionRevokedEvent`,
`ImpersonationStartedEvent`) y se documenta la limitación.

Y `event_name` usa `.replace("Event", "")`, **no** `removesuffix`
([`events.py:28`](../hexcore/domain/events.py#L28)): verificado que
`EventLogCreatedEvent` → `"LOGCREATED"`. Todo evento de Darwin lleva "Event" **sólo como
sufijo**.

---

## 6. JWT + base de datos híbrido

### 6.1 El claim set, con los cuatro defectos de `TokenClaims` corregidos

El `TokenClaims` que ya se shippea
([`domain/auth/value_objects.py`](../hexcore/domain/auth/value_objects.py)) tiene cuatro
problemas, y el tercero es descalificante:

| Defecto | Consecuencia |
| :-- | :-- |
| `client_id: str` obligatorio | No puede representar un token de sesión de primera parte sin inventar un client id |
| `scopes: t.List[Enum] = []` | Default mutable, y `Enum` pelado no es serializable con `model_dump(mode="json")` |
| **Sin `sid`** | **La revocación es imposible por construcción**: el token no se puede atar a una fila de sesión |
| Sin `aud` / `nbf` / `typ` | Sin `aud` no se distingue transporte (§7); sin `typ`, un refresh se puede presentar como access |

```python
class AccessTokenClaims(BaseModel):
    model_config = ConfigDict(frozen=True)

    iss: str
    sub: UUID                     # subject: la cuenta afectada
    act: UUID                     # actor: quién ejecuta. Distinto de `sub` sólo si impersona
    sid: UUID                     # OBLIGATORIO: ata el token a la fila de session
    aud: str                      # atado al transporte (§7)
    typ: t.Literal["at+jwt", "rt+jwt"]
    gen: int                      # generación del usuario, para revocación masiva
    exp: int
    iat: int
    nbf: int
    jti: UUID = Field(default_factory=uuid4)
    scopes: frozenset[str] = frozenset()      # frozenset: inmutable y serializable
    imp: bool = False                         # impersonando, para que el audit no dependa de comparar act/sub
```

### 6.2 Algoritmo y claves

- **`Ed25519`** por defecto. Firmas cortas, verificación rápida, sin parámetros que
  configurar mal (a diferencia de RSA, donde el largo de clave es una decisión).
  Se usa el identificador de curva y **no** el `EdDSA` genérico: RFC 9864 lo deprecó, y
  `joserfc` emite un `SecurityWarning` si se usa. Nacer con un `alg` deprecado es deuda
  caro de migrar, porque los tokens ya emitidos lo llevan en la cabecera.
- **La verificación pinea la lista de algoritmos**, nunca lee el `alg` del token. `none` se
  rechaza siempre. Nunca se honra `jku`, `x5u` ni un `jwk` embebido.
- Claves simétricas y asimétricas en almacenes **disjuntos**: es lo que evita la confusión
  HS256-firmado-con-la-clave-pública.
- `kid` obligatorio en el header, con caché negativa para los desconocidos (si no, un flood
  de `kid` inventados es un ataque de amplificación contra el almacén de claves).

### 6.3 Rotación sin logout masivo

Tres estados: `active` (firma y verifica) → `verify_only` (sólo verifica) → `retired`.
Rotar es: publicar la nueva a los verificadores → esperar → cambiar el firmante → retirar la
vieja después del TTL máximo.

⚠️ **Los verificadores incluyen los workers** (§3.3), así que el conjunto de claves tiene que
ser alcanzable desde el proceso worker. Es un requisito de cableado, no sólo de criptografía,
y es fácil de olvidar hasta que un job falla en producción.

### 6.4 Revocación: tres capas, cero DB en el camino caliente

**Capa 1 — `exp` corto.** Verificación puramente criptográfica, cero I/O. TTL ≤ 120 s.

**Capa 2 — denylist de `sid` en `ICache`.** Una lectura de cache por request.

```python
class CacheRevocationList:
    """
    Denylist de `sid` sobre el puerto `ICache`.

    **El vencimiento va DENTRO del valor, no en el TTL del backend.** No es paranoia:
    `MemoryCache.set()` ignora el parámetro `expire` por completo y nunca desaloja
    (verificado), así que una revocación puesta con TTL sería *permanente* con el backend
    por defecto y la lista crecería sin techo. Es el mismo motivo por el que `rate_limit`
    guarda su `reset_at` adentro del valor.

    Falla **cerrando** (`on_cache_error="deny"`), al revés que `rate_limit`, y la
    diferencia es deliberada: dejar pasar un request sin limitar es una molestia, dejar
    pasar un token revocado es la vulnerabilidad que esta clase existe para evitar.
    """
```

Semántica: **se permite si no está en la lista.** Es correcto porque el `until` de la entrada
siempre cubre la vida restante de todo token que lleve ese `sid`.

**Capa 3 — contador de generación.** `user.token_generation` se incrementa con el cambio de
contraseña, "cerrar todo", cambio de rol y alta de 2FA. El JWT lleva `gen`; la verificación
compara contra un `darwin:gen:<uid>` cacheado (TTL 60 s, se autopobla). Esto hace que
"invalidá todo lo de este usuario" sea **un solo UPDATE**, sin importar cuántas sesiones tenga.

**Rechazado: filtro de Bloom.** Un falso positivo en una lista de *denegación* desloguea a un
inocente al azar, y un Bloom plano no puede borrar, así que la revocación es permanente
mientras viva el filtro. El cache con TTL por `sid` es estrictamente mejor en los dos ejes.

**La fila de `session` se lee sólo en:** refresh, fallo del cache con `deny` (camino acotado,
con `logger.critical`), "listar mis sesiones", el reaper, y **el worker** (§3.3). Nunca en un
request autenticado normal.

### 6.5 Rotación de refresh con detección de reuso

Rotación en cada uso, con linaje (`family_id`). Reusar un token ya consumido **revoca la
familia entera** y publica `SessionReuseDetectedEvent`.

⚠️ El chequeo de "ya consumido" y la inserción del nuevo tienen que ser **una sola sentencia
atómica** (`UPDATE ... WHERE consumed_at IS NULL RETURNING`), no leer-y-después-escribir. Es
exactamente la carrera que `rate_limit` tenía y que se corrigió en la Fase 0.

### 6.6 Librerías

| Necesidad | Elegido | Por qué |
| :-- | :-- | :-- |
| JWT | **`joserfc`** | Mantenido por el autor de Authlib, soporta JWK/JWKS/rotación de primera, y su API obliga a pasar la lista de algoritmos permitidos — el default seguro es estructural, no documental. `pyjwt` acepta `algorithms=` opcional en algunas rutas. |
| Hash de contraseñas | **`argon2-cffi`** | Argon2id es el ganador del PHC y la recomendación de OWASP. Verifica hashes de bcrypt legados vía `passlib` sólo si hace falta migrar. |

Extra nuevo: `[darwin]` = `fastapi`, `sqlalchemy`, `alembic`, `joserfc`, `argon2-cffi`.
Se agrega a `all`. `[darwin-passkey]` suma `webauthn`.

**No se agrega `freezegun` ni `time-machine`:** el reloj es un puerto `Clock` inyectado, así
que los tests de TTL no necesitan parchear el tiempo global.

---

## 7. Dualidad de transporte

### 7.1 Una abstracción, dos adaptadores

```python
class AbstractTransport(abc.ABC):
    @abc.abstractmethod
    def extract(self, request: Request) -> str | None: ...
    @abc.abstractmethod
    def emit(self, response: Response, tokens: TokenPair) -> None: ...


class TransportResolver:
    """
    Resuelve el transporte una vez, y el resto del código no vuelve a ramificar.

    Regla: `Authorization: Bearer` gana; si no, la cookie. Un header
    `X-Darwin-Transport` explícito manda sobre ambos, para clientes que quieren fijarlo.
    """
```

### 7.2 ⚠️ El transporte va atado al token

**Esto es lo que evita el ataque de confusión de transporte.** Un token emitido para cookie,
replayeado como `Authorization: Bearer`, esquivaría `SameSite` y el chequeo CSRF por completo.

Mitigación: `aud` distinto por transporte, y el verificador acepta **sólo** el `aud` que
corresponde al transporte por el que llegó. Y **nunca** se hace el fallback "no hay cookie →
probemos el header" sobre el mismo tipo de token.

### 7.3 CSRF, sólo en el camino de cookie

`SameSite` **solo no alcanza**: falla contra un atacante en un subdominio adyacente y no
cubre todos los POST de nivel superior que `Lax` permite.

- Cookie: prefijo `__Host-` + `HttpOnly` + `Secure` + `SameSite=Lax` + `Path=/` + sin `Domain`.
- **Más** un chequeo anti-CSRF explícito en las rutas que cambian estado: double-submit con
  un valor derivado por HMAC del `sid` (no aleatorio, para que un subdominio no lo pueda
  fijar) y allowlist de `Origin` / `Sec-Fetch-Site`.
- `GET` exento; rutas exentas configurables.
- `trusted_origins=["*"]` es error de construcción.

### 7.4 Un endpoint, los dos transportes

```python
@router.post("/sign-in")
async def sign_in(payload: SignInDTO, transport: AbstractTransport = Depends(resolve_transport)):
    tokens = await bus.dispatch(SignIn(...))
    response = JSONResponse(...)
    transport.emit(response, tokens)   # cookie -> Set-Cookie; bearer -> tokens en el body
    return response
```

El cliente web recibe `Set-Cookie` y **ningún token en el body**; Expo/React Native recibe
los tokens en el body y **ningún** `Set-Cookie`. `Vary` se setea para que ningún cache
mezcle las dos respuestas.

---

## 8. Plan de desarrollo

Ordenado por dependencia. Sin fechas ni estimaciones.

### Fase 0 — Prerrequisitos ✅ **IMPLEMENTADA**

Bloqueante: nada de identidad se escribe antes. Ver §Apéndice para el detalle de los
hallazgos y el [CHANGELOG](../CHANGELOG.md).

- CORS credencial-reflejado corregido + fail-fast + 9 tests.
- Rate limit: `client_ip_key` ya no confía en XFF, `forwarded_ip_key()` con proxies
  declarados, conteo atómico vía `SupportsAtomicWindow`.
- Empaquetado: `[build-system]`, `package-data`, `packages.find`, `__init__.py` raíz muerto
  borrado, 6 tests sobre la wheel.
- `REMOVED_IN` 6.0 → 7.0 + test de guardia; política de soporte del README al día.
- `headers_for` en los exception handlers (RFC 6750).
- Gate de tipado midiendo: `[tool.pyright]` strict, ratchet, baseline de **216 errores**.

### Fase 1 — Convención de nombres de constraints

**Antes** de la primera tabla (§2.6): agregarla después es una migración rompedora.
Modifica `orms/sqlalchemy/__init__.py`. Test: los nombres generados son estables y no
dependen del backend.

### Fase 2 — Dominio puro ✅ **IMPLEMENTADA**

Sólo stdlib + pydantic. Sin SQL, sin crypto, sin HTTP.

Crea `hexcore/darwin/domain/{entities,value_objects,context,permissions,events,ports,exceptions}.py`
y la fachada `hexcore/darwin.py` (patrón `_EXPORTS` + `__getattr__`).

API: `Principal`, `Impersonation`, `AuthContext[TUser]`, `AUTH_CONTEXT`, `current_auth`,
`require_auth`, `auth_scope`; `AccessTokenClaims`; `Role`, `RoleRegistry`; los puertos
`Abstract*`; la jerarquía `IdentityError`; `IDENTITY_EXCEPTION_STATUS_MAP`.

Tests: **el invariante de impersonación en los dos sentidos** (subject≠actor sin permiso →
`ValidationError`; permiso con subject==actor → `ValidationError`); anidamiento de
`auth_scope` y `reset` ante excepción; `RoleRegistry` bajo `ThreadPoolExecutor`.
→ **extiende `tests/test_optional_dependencies.py`**: `("sqlalchemy", "hexcore.darwin")`,
`("fastapi", "hexcore.darwin")`, `("joserfc", "hexcore.darwin")`, `("argon2", "hexcore.darwin")`.

### Fase 3 — Persistencia ✅ **IMPLEMENTADA**

Las 6 tablas, con **las dos reglas verificadas por test** (§2.1) y el test de no-colisión.

Además: `import models_mixins` no agrega nada a `Base.metadata`; `import models` agrega
exactamente 6 tablas; renombrado vía mixin con retargeteo de FK; `create_identity_tables()`
idempotente; round-trip de tz-awareness; email duplicado → `IntegrityError` mapeado a
`EmailAlreadyRegisteredError`.

### Fase 4 — Crypto ✅ **IMPLEMENTADA** (**el núcleo de seguridad de la suite**)

`hashing`, `keys`, `tokens`, `revocation`.

Tests adversariales: **confusión de `alg`** (`none`; HS256 firmado con la clave pública
Ed25519 como secreto HMAC; `RS256` con allowlist `["Ed25519"]`); **confusión de `typ`**
(`rt+jwt` donde se exige `at+jwt`); `kid` desconocido/retirado/ausente + caché negativa
contada bajo flood; `aud`/`iss` que no coinciden; `nbf` futuro y `exp` pasado con y sin el
skew configurado; **rotación** (token de la clave anterior verifica en `verify_only`, falla
en `retired`); **la regresión de `MemoryCache` como test ejecutable** (revocar, avanzar el
`Clock` más allá del `until`, `is_revoked() is False` — falla si la implementación confía en
el TTL del backend); **timing** (el path de login hashea un dummy fijo cuando no hay fila, y
se cuentan las llamadas a `hmac.compare_digest`).

### Fase 5 — Aplicación + contenedor

`configure_identity(...) -> IdentityContainer`, `get_identity_container()`,
`reset_identity()`, `provide_*`. Comandos, queries, handlers.

Tests: sin configurar → `RuntimeError` con la remediación en el mensaje; init perezoso
thread-safe; flujo completo sobre `sqlite_session` (sign-up → verify → sign-in → refresh →
sign-out) y sus caminos de fallo.

### Fase 6 — Propagación del actor + auditoría

**Modifica el núcleo** (decisión de §3.3): `domain/cqrs/serializer.py` (+2 métodos
concretos), `infrastructure/cqrs/pydantic_serializer.py`,
`infrastructure/workers/consumer.py`, y los call sites de `serialize()` en los 5 transportes.

Tests: round trip real (sellar → `PydanticSerializer` → `CQRSConsumer.process_command` sobre
un bus de `build_test_buses()` → el handler ve el mismo actor **y** el mismo subject);
**payload legado sin `__meta__` sigue deserializando** (la razón de que los métodos sean
concretos); sobre manipulado (un byte del payload, un byte de la firma, actor y subject
swapeados) → `WorkerContextIntegrityError`; **grant re-adjuntado a otro comando → rechazado**
(el binding `cid`/`mt` de §3.3); **la regresión de `IN_WORKER`** (assert de que
`is_worker_execution()` es `False` dentro del middleware incluso despachado desde el
consumer, documentando por qué no hay que consultarlo).

### Fase 7 — Borde HTTP

`transports`, `api/{middlewares,dependencies,routers}`, `lifespan` (`IdentityStep`,
`JwksStep`, `SessionReaperStep`).

Modifica `api/app.py`: `AppFeatures` gana `auth_context: bool = False` y `csrf: bool = False`
(**apagados por default**: prender auth en silencio cambiaría el comportamiento de toda app
existente). `AuthContextMiddleware` se registra **antes** de `RequestIDMiddleware` (orden
inverso → auth corre adentro de request-id, así que `get_request_id()` ya está poblado).

`IDENTITY_EXCEPTION_STATUS_MAP` se mergea en `create_app`, **no** se agrega a
`DEFAULT_EXCEPTION_STATUS_MAP`: importar las excepciones de Darwin en tiempo de import de la
capa `api` la acoplaría al módulo y rompería el contrato de dependencias opcionales.

| Excepción | Status |
| :-- | :-- |
| `UnauthenticatedError`, `InvalidCredentialsError`, `TokenExpiredError`, `TokenRevokedError`, `TokenMalformedError` | 401 |
| `InsufficientScopeError`, `CsrfValidationError`, `EmailNotVerifiedError`, `ImpersonationNotPermittedError` | 403 |
| `EmailAlreadyRegisteredError` | 409 |
| `AccountLockedError` | 423 |
| `WorkerContextIntegrityError` | 500 |

`IdentityError` (la base) **se omite a propósito**: `_specificity` ordena por profundidad de
MRO, así que registrarla como 400 haría que una excepción nueva sin mapear se tragara como
400 en vez de aparecer como 500 en los tests.

Tests: doble publicación ContextVar + `request.state` con `reset` en `finally` incluso si el
endpoint lanza; el mismo endpoint por los dos transportes; atributos de cookie aserteados
literalmente; **CSRF** (POST cross-origin con cookie válida → 403; `Origin` que coincide →
200; sin `X-CSRF-Token` → 403; valor forjado por un subdominio → 403); 401 lleva
`WWW-Authenticate` (usa el `headers_for` de la Fase 0); **fijación de sesión** (el token
cambia en login, cambio de contraseña, alta de 2FA, e inicio y fin de impersonación —
aserteado como desigualdad, por evento); **replay/carrera de revocación**; fuerza bruta sobre
sign-in con el `rate_limit` ya corregido.

### Fase 8 — Plugins + el de referencia

`HookMiddleware`, el registro, y `magic_link` como plugin de referencia.
Modifica `cli.py` con `app.add_typer(darwin_cli, name="identity")` — el primer `add_typer` del
repo. ⚠️ `hexcore/__init__.py` importa `cli` **eagerly**, así que ese módulo puede importar
sólo `typer` + stdlib en el top level; todo import pesado va adentro del cuerpo del comando.

Tests: nombre duplicado / `requires` faltante / ciclo / conflicto de tabla → cada uno su
error al construir, nombrando al culpable. Hooks: mutación de `payload` llega al handler;
`ShortCircuit` en `before` saltea el handler **y** los `before` restantes; una excepción que
no es `ShortCircuit` propaga (falla cerrando).

### Fase 9 — El resto de los plugins

`two_factor` → `oauth` → `impersonate` (**depende de la Fase 6**; techo de 60 min no
renovable; refresh → 403; toda acción auditada con los dos principales) → `passkey` →
`organization`.

### Fase 10 — Kit de testing, docs, deprecaciones

`hexcore/darwin/testing/` con `FakeUserRepository`, `FakeClock`, `authenticated_context()`,
`impersonated_client`.

Cierra además el hueco detectado: `hexcore/testing/` **no tiene fake de repositorio ni de
UoW**. Van al kit general, no al de Darwin, porque sirven mucho más allá de identidad.

Deprecaciones: `domain/auth/value_objects.TokenClaims` → `AccessTokenClaims` y
`domain/auth/permissions.PermissionsRegistry` → `RoleRegistry`, vía `deprecated_aliases`.
Los dos están re-exportados en `hexcore/__init__.py` y en su `__all__`, así que se mueven
detrás de un `__getattr__` de módulo: `from hexcore import TokenClaims` sigue funcionando
(PEP 562 cubre los `from`-imports) y ahora avisa.

⚠️ Las docs son ejecutables: [`tests/test_documentation_examples.py`](../tests/test_documentation_examples.py)
corre los bloques `Uso::`.

---

## 9. Cambios en archivos existentes

| Archivo | Cambio | Fase | Tipo |
| :-- | :-- | :-- | :-- |
| `hexcore/config.py` | `darwin: t.Any = None` + comentario de tipo, calcando el precedente de `cqrs`. **`secret_key` NO va acá**: todo campo de `ServerConfig` tiene default, y un secreto de firma con default es lo peor que puede shippear una librería. Vive en `IdentityConfig` como `SecretStr` sin default, leído de `HEXCORE_DARWIN_SECRET_KEY` por un validador que lanza con remediación si falta y `debug=False`. | 2 | aditivo |
| `hexcore/config.py` | ✅ **Hecho (Fase 0):** fix del CORS credencial-reflejado. | 0 | fix |
| `hexcore/infrastructure/api/rate_limit.py` | ✅ **Hecho (Fase 0):** XFF, atomicidad. | 0 | fix |
| `hexcore/infrastructure/api/exception_handlers.py` | ✅ **Hecho (Fase 0):** `headers_for`. Fase 7 sólo mergea el mapa de Darwin. | 0 / 7 | aditivo |
| `hexcore/infrastructure/repositories/orms/sqlalchemy/__init__.py` | `naming_convention` completa (§2.6). | 1 | fix |
| `hexcore/domain/cqrs/serializer.py` | +2 métodos **concretos** para el sobre. Ninguna subclase existente se rompe. | 6 | aditivo |
| `hexcore/infrastructure/cqrs/pydantic_serializer.py` | Override por simetría. | 6 | aditivo |
| `hexcore/infrastructure/workers/consumer.py` | Verifica el sobre, re-chequea la sesión, rehidrata el ContextVar. | 6 | aditivo |
| `infrastructure/cqrs/{rabbitmq,postgres_bus,redis_bus,procrastinate}.py`, `task_queues/*` | `serialize()` → `serialize_envelope()`. ~1 línea cada uno. | 6 | aditivo |
| `hexcore/infrastructure/api/app.py` | `AppFeatures` += `auth_context`, `csrf` (default **off**); orden de middlewares; merge del mapa. | 7 | aditivo |
| `hexcore/infrastructure/cli.py` | `app.add_typer(darwin_cli, name="identity")`; `ensure_identity_schema_loaded()` en el `env.py` generado. | 8 | aditivo |
| `hexcore/infrastructure/repositories/orms/sqlalchemy/utils.py` | `import_all_models`: `iter_modules` → `walk_packages` (§5.3). Arregla un `DROP TABLE` latente que ya afecta a `hexcore_cron_jobs`. | 8 | fix |
| `hexcore/domain/auth/*`, `hexcore/__init__.py` | Absorbidos y deprecados. | 10 | deprecación |
| `hexcore/_deprecation.py` | ✅ **Hecho (Fase 0):** `REMOVED_IN` → 7.0. | 0 | fix |
| `pyproject.toml` | Extras `darwin`, `darwin-passkey`, agregados a `all`. | 2 / 9 | aditivo |
| `tests/test_optional_dependencies.py` | Filas nuevas en las fases 2, 3, 4 y 9. ⚠️ `argon2-cffi` se importa como `argon2`. | 2-9 | aditivo |
| `hexcore/testing/{fakes,fixtures}.py` | `FakeUnitOfWork` + `FakeRepository` genéricos. | 10 | aditivo |

**Nada de `hexcore/domain/cqrs/` ni `hexcore/application/cqrs/` cambia excepto el sobre del
serializer en la Fase 6.** `AbstractMiddleware.handle(message, next_handler)`, las formas
frozen de `Command`/`Query`, `AbstractCommandHandler.handle(command)` y los puertos de bus
de un solo argumento quedan **intactos**.

---

## Apéndice: hallazgos de seguridad preexistentes

Cuatro defectos verificados en ejecución, no por lectura. Los cuatro son **latentes hoy** y
Darwin los volvería explotables, así que se corrigieron en la Fase 0 antes de escribir una
línea de identidad.

### A.1 🔴 CRÍTICO — CORS refleja el origen del atacante con credenciales

```
$ python -c "from hexcore.config import ServerConfig; print(ServerConfig(debug=False).allow_origins)"
['*']                                   # allow_credentials = True

# GET /health/live  con  Origin: https://evil.example  +  Cookie: sesion=1
ACAO: https://evil.example        ACAC: true        Vary: Origin
```

**Causa.** `config.py:45-47` declaraba
`allow_origins = ["*" if debug else "http://localhost:{port}"]` en el **cuerpo de la clase**,
donde `debug` es el nombre del cuerpo de clase (siempre `True`). El condicional era código
muerto: el valor era **siempre** `["*"]`, incluso con `ServerConfig(debug=False)`.

**Mecanismo del exploit.** `starlette/middleware/cors.py:159` —
`if self.allow_all_origins and has_cookie:` — no puede emitir `*` junto con credenciales, así
que **refleja el `Origin`** y agrega `ACAC: true`. Sólo se dispara cuando hay cookie, que es
exactamente el caso de una sesión.

**Impacto con Darwin.** Cualquier origen ejecuta
`fetch('https://api/me', {credentials:'include'})`, el navegador manda la cookie de sesión y
CORS autoriza la lectura: **toma de cuenta desde cualquier origen, sin XSS.**

**Corregido.** Derivación en un `model_validator(mode="after")` que ve el `debug`/`port` de
la instancia, más un fail-fast que no arranca con `"*"` + credenciales fuera de `debug`.
9 tests, incluido el de regresión end-to-end contra `create_app()`.

### A.2 🟠 ALTO — El rate limit de login es esquivable

Tres defectos que se componen:

1. **`client_ip_key` confiaba en `X-Forwarded-For` sin condiciones** (`rate_limit.py:37-39`).
   El header lo escribe el cliente: un valor distinto por petición daba un bucket nuevo por
   petición. **El límite de login era un no-op.**
2. **Contador no atómico** (`rate_limit.py:93-110`): `get()` → comparar → `set()` con un
   `await` en el medio. Verificado: con `limit=3` y 20 corutinas concurrentes pasaban las 20.
3. **`on_backend_error="allow"`** (fail-open): Redis caído = credential stuffing ilimitado.

**Corregido.** `client_ip_key` ya no mira el header; `forwarded_ip_key(trusted_proxies=...,
trust_hops=...)` lo honra **sólo** desde un par declarado y cuenta los saltos **desde la
derecha** (la única parte del header que el cliente no controla); conteo atómico vía el
Protocol `SupportsAtomicWindow` (Redis con `SET NX EX` + `INCR` en pipeline; `MemoryCache`
sin `await` entre lectura y escritura). El fail-open se mantiene por compatibilidad y se
documenta; las rutas de auth de Darwin usarán `"deny"`.

### A.3 🟠 ALTO — `autogenerate` de Alembic dropea las tablas del framework

Ver §5.3. **Ya afecta a `hexcore_cron_jobs` hoy.** Con Darwin sería el almacén de identidad
completo. Se corrige en la Fase 8 (`walk_packages`) y se mitiga por diseño desde la Fase 3
(los plugins shippean mixins; el consumidor declara las tablas en su propio paquete).

### A.4 🟡 MEDIO — `get_domain_entity()` explota después del commit

Ver §2.1 regla 1. No es un defecto a corregir sino una restricción a respetar: es la razón
de que los modelos de Darwin no hereden `BaseModel[T]`, y hay un test que lo fija.

### A.5 Descartado del registro de riesgos

`BaseEntity._domain_events: t.List[DomainEvent] = []` **no** es un default mutable
compartido. El guion bajo inicial más la anotación hacen que pydantic v2 lo convierta en un
`ModelPrivateAttr`, cuyo `get_default()` hace `smart_deepcopy` por instancia. Verificado
empíricamente. Se deja anotado para que nadie vuelva a "arreglarlo".
