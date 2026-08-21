# Darwin — Módulo de Identidad Nativo de HexCore

> Documento técnico de arquitectura. Port deliberado de [Better Auth](https://github.com/better-auth/better-auth)
> (TypeScript) a Python + CQRS sobre HexCore 6.x.
>
> Estado: **diseño aprobado, Fases 0-7 implementadas.** Sin fechas ni estimaciones: el orden
> es por dependencia, no por calendario.

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

> **Estado de implementación.** Fases 0-7 completas. Siguen: plugins (8-9) y kit de
> testing (10), que **agregan** superficie sin cambiar la que ya está.
>
> La marca de **API provisional** se retiró en la Fase 7, que era el punto de estabilidad
> declarado: el borde HTTP cerró las formas de `AuthContext`, de los puertos, de los
> transportes y del emisor de tokens. Dejarla "una fase más por las dudas" habría sido el
> mismo desliz que `REMOVED_IN = "6.0"` shippeado en 6.0.0.

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

- La subclase **no puede** llevar el actor en `@background_handler` (eventos), que es la mitad
  del uso de background del framework. Habría que hacer el sobre igual, más adelante, y
  `AuthenticatedCommand` quedaría como API pública a deprecar — exactamente el error que
  este repo ya cometió con `REMOVED_IN = "6.0"` shippeado en 6.0.0.
- El costo real del sobre es chico y **aditivo**. Verificado: los transportes tratan el
  payload como opaco (`procrastinate_adapter.py:36`, `celery_adapter.py:133`,
  `postgres_bus.py:58-63`, `redis_bus.py:60-63`, `rabbitmq.py:88`). Los únicos lectores de
  estructura son `PydanticSerializer` y dos peeks de `__type__`.

**Qué cubre y qué no.** Cubre `Command` (incluido `@background_command`) y
`@background_handler`, o sea los dos caminos que pasan por el serializer. **No cubre
`@background_task`**, y es una limitación estructural, no un pendiente: en una tarea genérica
el payload **es** el dict de kwargs con el que el worker llama a `task_func(**payload)`, así
que una clave `__meta__` colisionaría con un parámetro y rompería la llamada; y no hay
objeto-mensaje al que atar el grant. Una tarea que necesita saber quién la originó recibe ese
dato como **parámetro explícito**. (El único productor de tareas en el árbol es el scheduler
de cron, que no tiene actor de usuario.)

Los dos métodos nuevos son **concretos** en el ABC, así que ninguna implementación existente
de `AbstractSerializer` se rompe:

```python
# hexcore/domain/cqrs/serializer.py
class AbstractSerializer(abc.ABC):
    @abc.abstractmethod
    def serialize(self, message: t.Any) -> dict[str, t.Any]: ...     # sin tocar

    # Concretos: un abstractmethod rompería a todo el que tenga un serializer propio.
    def serialize_envelope(self, message, metadata=None) -> dict[str, t.Any]:
        payload = self.serialize(message)
        if metadata is None:
            metadata = collect_envelope_metadata(message)     # el registro de proveedores
        if metadata:
            payload[ENVELOPE_METADATA_KEY] = dict(metadata)
        return payload

    def deserialize_envelope(self, data) -> tuple[t.Any, dict[str, t.Any]]:
        # Un payload viejo SIN `__meta__` tiene que seguir deserializando: es la razón de que
        # estos métodos sean concretos. Y la clave se **saca** antes de delegar, en vez de
        # confiar en que `deserialize` la ignore: un serializer estricto es legítimo.
        ...
```

Payload resultante:

```json
{
  "__type__": "app.commands.ChargeInvoice",
  "__data__": {"invoice_id": "..."},
  "__meta__": {"auth": "<b64url(json)>.<b64url(hmac_sha256)>"}
}
```

**El núcleo no sabe nada de identidad.** `hexcore/domain/cqrs/envelope.py` es un punto de
extensión con dos registros por clave: *proveedores* (`(message) -> valor | None`, consultados
al encolar) y *restauradores* (`AbstractEnvelopeRestorer`, consultados en el worker). Darwin
registra los suyos bajo la clave `"auth"` en `configure_identity()`, y `reset_identity()` los
deregistra. **Sin nadie registrado el payload queda byte a byte idéntico** al que el framework
generaba antes — es lo que hace que esto sea aditivo y no un cambio rompedor, y hay un test
que lo asserta comparando `serialize_envelope()` con `serialize()`.

Una clave del sobre **sin** restaurador registrado no se ejecuta: lanza `RuntimeError` con la
línea de cableado que falta. El caso real es un worker al que le falta `configure_identity()`,
y ejecutar ahí correría el handler sin la autoridad que el mensaje traía.

`AbstractEnvelopeRestorer.restore` es un context manager **asíncrono**, y las dos cosas
importan: context manager porque el `reset` del `ContextVar` tiene que ocurrir aunque el
handler lance —si no, un job que falla le filtra su contexto al siguiente del mismo worker— y
asíncrono porque la revalidación va contra el almacén.

**El detalle crítico: el grant va atado al mensaje.** El payload firmado incluye
`cid = command_id` (o `event_id`) y `mt = build_fqn(type(message))`. La verificación rechaza si
no coinciden. `mt` se calcula sobre el **tipo del objeto ya reconstruido**, no sobre el
`__type__` del payload: así el chequeo no depende del formato del serializer, y manipular
`__type__` produce otra clase cuyo FQN tampoco coincide.

Sin ese binding, el ataque es: capturar el sobre de un `DeleteAccount` legítimo y
re-adjuntarlo a un `TransferFunds`. El sobre verifica —está bien firmado— y el worker
ejecuta la transferencia con la autoridad del grant de borrado. Es escalación de privilegios
a un `LPUSH` de distancia. `cid` cubre la otra mitad: dos `TransferFunds` son del mismo tipo,
así que sin el id el sobre de una transferencia de $10 sirve para una de $1.000.000.

**El sobre no es un JWT, a propósito.** Se firma con HMAC-SHA256 usando
`IdentityConfig.secret_key`, no con la clave del JWKS, y el formato es distinto del de un
token. Un JWT invita a que alguien lo presente como credencial en un endpoint, y este valor no
es una credencial de portador: no lo emite un login, no lo ve un cliente, y vale sólo adjunto
al mensaje al que se ató. El input del MAC lleva una etiqueta de dominio, así que el mismo
secreto usado en otro protocolo no puede producir una falsificación cruzada. Lleva `v` desde el
día uno: el sobre tiene TTL, así que cuando el formato cambie va a haber sobres de los dos
formatos en la cola durante la ventana del deploy.

**El `transport` restaurado es siempre `"worker"`**, nunca el original. Un job de background no
está sirviendo un request con cookie, y el código que ramifica por transporte —el chequeo
anti-CSRF— tiene que poder distinguirlo. **Y `AuthContext.user` no viaja**: es el modelo
extendido de la app, de tipo arbitrario y sin garantía de ser serializable. Un handler de
background que lo necesite lo carga con `subject_id`.

**El worker re-valida contra la base.** Verificar la firma y el `exp` no alcanza: entre el
encolado y la ejecución la sesión puede haberse revocado. El restaurador chequea que la fila de
`session` esté viva (`is_live_at`, que cubre revocada, consumida y vencida). Un TTL de 24 h en
el sobre sin este chequeo son 24 h de ejecución con una sesión revocada. Sólo se revalida
cuando el actor **tiene** `session_id`: un `SystemPrincipal` no tiene sesión revocable, su
autoridad es el cableado del proceso.

```python
# hexcore/infrastructure/workers/consumer.py
message, metadata = self._serializer.deserialize_envelope(payload)
# El scope va por fuera de `worker_execution()`: si el sobre no verifica, el mensaje no
# llega ni a entrar al bus.
async with restored_envelope_scope(metadata, message):   # firma + cid + mt + exp + la fila
    with worker_execution():
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

### Fase 5 — Aplicación + contenedor ✅ **IMPLEMENTADA**

`configure_identity(...) -> IdentityContainer`, `get_identity_container()`,
`reset_identity()`, `provide_*`. Comandos, queries, handlers.

Tests: sin configurar → `RuntimeError` con la remediación en el mensaje; init perezoso
thread-safe; flujo completo sobre `sqlite_session` (sign-up → verify → sign-in → refresh →
sign-out) y sus caminos de fallo.

### Fase 6 — Propagación del actor ✅

**Modifica el núcleo** (decisión de §3.3). Nuevo: `domain/cqrs/envelope.py` (el punto de
extensión: registros, `restored_envelope_scope`, `message_correlation_id`) y
`darwin/infrastructure/envelope.py` (`AuthEnvelopeCodec`, `AuthEnvelopeRestorer`,
`auth_envelope_provider`). Modificados: `domain/cqrs/serializer.py` (+2 métodos concretos),
`darwin/application/container.py` (`envelope_codec()`, `envelope_restorer()`, y el registro en
`configure_identity` / la baja en `reset_identity`), y los seis call sites —
`in_memory_buses.py` (×2), `postgres_bus.py`, `redis_bus.py`, `rabbitmq.py`,
`procrastinate.py` — más `workers/consumer.py` en sus tres rutas.

`pydantic_serializer.py` **no** se tocó: los dos métodos nuevos son concretos en el ABC y
envuelven `serialize`/`deserialize`, así que hereda el comportamiento sin cambios.

Tests (`test_cqrs_envelope.py`, 24 — el mecanismo pelado, sin Darwin; `test_darwin_envelope.py`,
37): round trip real (sellar → `PydanticSerializer` → `CQRSConsumer.process_command` sobre un
bus de `build_test_buses()` → el handler ve el mismo actor **y** el mismo subject, y el
`transport` es `"worker"`); **payload legado sin `__meta__` sigue deserializando** y
`serialize_envelope() == serialize()` sin proveedores (la razón de que los métodos sean
concretos); sobre manipulado (un byte del payload, un byte de la firma, actor y subject
swapeados, escalar los scopes editando el JSON, otro secreto de firma) →
`WorkerContextIntegrityError`; **grant re-adjuntado a otro comando y a otra instancia del mismo
comando → rechazado** (el binding `cid`/`mt`); sobre vencido y sobre fechado en el futuro, con
la tolerancia de reloj; versión desconocida; sesión revocada, inexistente y ya rotada →
rechazadas contra SQLite real; worker sin Darwin cableado → `RuntimeError` con remediación; y
**la regresión de `IN_WORKER`** (assert de que `is_worker_execution()` es `False` dentro del
middleware incluso despachado desde el consumer, y de que lo que sí está disponible es el
contexto ambiental).

### Fase 7 — Borde HTTP ✅

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

Creados: `infrastructure/transports.py`, `infrastructure/api/{middlewares,dependencies,routers}.py`,
`infrastructure/lifespan.py`, y `derive_csrf_token` en `infrastructure/hashing.py`.

⚠️ **Un middleware de Starlette corre por fuera de `ExceptionMiddleware`**, así que lo que
lanza **no** pasa por los handlers que `register_exception_handlers` instaló: saldría como un
500 con el traceback en texto plano. `CsrfMiddleware` por eso **devuelve** la `JSONResponse`
en vez de propagar `CsrfValidationError`, replicando a mano la forma del cuerpo de error del
framework. Lo encontró el test, no la revisión.

**El valor anti-CSRF es derivado, no aleatorio** (`derive_csrf_token` = HMAC del `sid`). La
cookie de CSRF no puede ser `HttpOnly` —el cliente tiene que leerla para devolverla en el
header— así que un subdominio comprometido **puede escribirla**; con un valor aleatorio,
escribe la cookie y manda el mismo valor en el header, pasando el double-submit con un valor
que eligió él.

**`/auth/me` lee la fila del usuario**, y es la excepción deliberada al "cero DB en el camino
caliente": el mail no viaja en el token porque es PII, y un access token que el cliente guarda
no es el lugar para ponerla. La regla de no tocar la base vale para cada petición autenticada,
no para el endpoint cuyo propósito es devolver los datos del usuario. Lo detectó un test que
esperaba el mail en `/me`.

Tests (`test_darwin_http.py`, 35, con `TestClient` sobre `create_app()`): doble publicación
ContextVar + `request.state` con `reset` en `finally` incluso si el endpoint lanza; el mismo
endpoint por los dos transportes (cookie sin tokens en el cuerpo, Bearer sin `Set-Cookie`, y
`Vary` en las dos); atributos de cookie aserteados **literalmente sobre el header**
(`__Host-`, `HttpOnly`, `Secure`, `SameSite=lax`, `Path=/`, sin `Domain`); **confusión de
transporte en los dos sentidos** (cookie replayeada como Bearer y viceversa → 401); **CSRF**
(POST cross-origin con cookie válida → 403; origen declarado + double-submit → 200; sin
`X-CSRF-Token` → 403; valor forjado → 403; `GET` exento; Bearer exento; POST anónimo exento);
401 lleva `WWW-Authenticate` y 403 **no** (usa el `headers_for` de la Fase 0); fijación de
sesión; sign-out borra cookies **y** revoca; fuerza bruta sobre sign-in con el `rate_limit` ya
corregido.

⚠️ **Nota de testing**: `rate_limit` usa `config.cache_backend`, que es un `MemoryCache`
global del proceso. Una suite que pega en la ruta de login tiene que resetearlo por test, o
los primeros pasan y del sexto en adelante todo da 429 — con el síntoma desconcertante de que
cada test pasa aislado y falla en la suite.

### Fase 8 — Plugins + el de referencia ✅

`domain/plugins.py` (el contrato), `application/plugins.py` (el registro),
`application/hooks.py` (`HookMiddleware`), `plugins/magic_link/` (el de referencia) e
`infrastructure/cli.py` (la sub-app de Typer).

**El contrato es una clase abstracta con un solo miembro obligatorio: `name`.** Los siete
métodos de aporte —`tables()`, `hooks()`, `middlewares()`, `http_middlewares()`, `routers()`,
`startup_steps()`, `register_handlers()`— son **concretos** y devuelven vacío. Así un plugin
declara nada más lo que aporta, y agregar un punto de extensión nuevo más adelante no rompe a
ninguno existente. Con métodos abstractos, cada punto de extensión nuevo sería un cambio
rompedor para todos los plugins del ecosistema.

**El registro valida al cablear, no en el primer request.** Cuatro errores, cada uno nombrando
al culpable: nombre vacío o ausente, nombre duplicado, `requires` que apunta a un plugin que
no está, y ciclo de dependencias (con el ciclo impreso, `a -> b -> c -> a`). Más el conflicto
de mixins: dos plugins que aportan el mismo nombre de tabla se rechazan, porque si no el
consumidor no puede saber cuál está componiendo y el diff de su migración dependería del orden
de importación. `configure_identity` llama a `validate()`, igual que valida el modelo de
usuario.

**El orden de ejecución es topológico y determinista**: por `requires`, con
`(priority, orden de registro)` como desempate. Si dependiera del hash de un set, el mismo
cableado daría cadenas de hooks distintas entre corridas — y un bug que aparece una vez de cada
tres no se diagnostica.

**Los hooks son `(action, phase, handler, priority)` con comodines de `fnmatch`**, y los
específicos corren **antes** que los de comodín: un hook de auditoría con `"*"` quiere ver el
payload final, no el que llegó antes de que los específicos lo ajustaran. La acción sale de
`@identity_action("user.sign_in")` o, si no está declarada, del nombre de la clase pasado a
snake_case. Se declara explícitamente en todo lo que shippea Darwin porque derivarla ata el
hook al nombre de la clase: renombrar el comando rompería en silencio los hooks de todos los
plugins. `hooks_for(action, phase)` está memoizado — corre en cada mensaje, y sin cache cada
uno pagaría un `fnmatch` por hook registrado.

**Los hooks no mutan un `ctx` compartido: encadenan el payload.** Los mensajes son `frozen`,
así que un hook que quiere cambiar algo devuelve una instancia nueva y el resultado alimenta al
siguiente. Es el desvío deliberado de Better Auth, donde los hooks mutan un contexto y el orden
se vuelve parte del contrato sin estar escrito en ningún lado.

**`ShortCircuit` es el mecanismo de control de flujo**: en `before` saltea el handler **y los
`before` que quedaban** (correrlos sería trabajo sobre una decisión ya tomada), y su `.result`
se devuelve como resultado. Es con lo que un plugin responde por su cuenta: 2FA que exige el
segundo factor, un bloqueo por país, una cuota agotada. En `after` el handler **ya corrió**, así
que cortocircuitar reemplaza el resultado y no cancela el efecto — un plugin que quiera impedir
la operación tiene que hacerlo en `before`.

⚠️ **Cualquier otra excepción propaga, envuelta con el nombre del plugin, la fase y la acción.
Falla cerrando.** Tragarla dejaría que un hook de autorización que explota se lea como uno que
autorizó, que es el peor modo de falla posible para un sistema de plugins de auth.

**`magic_link`, el de referencia.** Reusa la tabla `verification` con `purpose="magic_link"` en
vez de aportar una propia: un magic link **es** un token de un solo uso con vencimiento,
`attempts` y `consumed_at`, y una tabla equivalente le dejaría al consumidor dos migraciones y
dos reapers para el mismo concepto. Canjearlo crea una sesión normal, así que revocación,
rotación, transporte y CSRF funcionan sin saber que hubo un magic link. `POST /request`
responde igual exista o no la cuenta —la diferencia va en si manda el mail— porque al revés
sería un oráculo de enumeración en una ruta pública sin autenticación. El canje es **atómico**
(`UPDATE ... WHERE consumed_at IS NULL RETURNING`), así que de dos clicks simultáneos
exactamente uno gana; con un SELECT seguido de un UPDATE, "de un solo uso" sería falso
justamente bajo concurrencia. Pedir uno nuevo invalida los pendientes: sin eso, cinco clicks en
"reenviar" dejan cinco links válidos. TTL de 15 min, no 24 h: es una credencial de portador que
queda en el buzón, en los logs del proveedor y en el historial del cliente. Y verifica el mail
como efecto — quien probó que controla la casilla ya demostró lo que la verificación prueba.

`session_response_body` (antes `_cuerpo`) **se hizo pública** por esto: el router del plugin
devuelve la misma forma que `/auth/sign-in`, y que cada plugin armara su propio cuerpo dejaría
que uno filtre los tokens en el cuerpo estando en modo cookie. Lo señaló pyright con
`reportPrivateUsage`, que es exactamente para lo que sirve.

**`hexcore/darwin/plugins/__init__.py` está vacío a propósito**: no hay discovery. Un plugin se
registra escribiéndolo en el `PluginRegistry`, porque un plugin de auth que se activa solo por
estar instalado es una superficie de ataque de cadena de suministro.

**La sub-app de Typer** — el primer `add_typer` del repo: `identity_cli` con `generate-secret`,
`generate-keys`, `create-tables`, `check-schema` (sale con 1, para poner en un pre-commit) y
`plugins <módulo>` (imprime el orden resuelto, que es la única forma de confirmar que un plugin
corre donde uno cree).

⚠️ **`hexcore/__init__.py` importa `hexcore.infrastructure.cli` eagerly, y ese módulo hace
`add_typer(identity_cli)`.** O sea que todo lo que `hexcore/darwin/infrastructure/cli.py`
importe en el nivel superior se carga con cada `import hexcore`, en cualquier proceso, tenga o
no los extras. Por eso ese módulo importa **sólo `typer` + stdlib** arriba y cada comando
importa lo que necesita adentro de su cuerpo. Es el contrato más frágil de la fase, y lo
custodian dos tests: uno lee el AST del módulo y otro cuenta los `hexcore.darwin.*` de
`sys.modules` en un subproceso limpio.

Tests (`test_darwin_plugins.py` 38, `test_darwin_magic_link.py` 27, `test_darwin_cli.py` 12):
las cuatro validaciones del registro, cada una nombrando al culpable; orden topológico,
transitividad, y los dos desempates; el registro vacío es *truthy* (sin `__bool__` explícito,
un `if registro:` lo descartaría — el mismo defecto que `InMemoryTaskEnqueuer` documenta);
comodines con `fnmatchcase` (`"User.*"` **no** matchea `user.sign_in`); específico antes que
comodín aunque el comodín tenga prioridad menor; memoización de `hooks_for` aserteada contando
llamadas, e invalidación al registrar; los cinco comportamientos de `ShortCircuit` y de la
excepción que propaga; `HookMiddleware` compuesto en un `MiddlewarePipeline` real. De
`magic_link`: el flujo completo contra SQLite; el token guardado como hash y no en claro
(leyendo la fila por SQL, porque el repositorio no expone un `find` y no debería); **ocho
canjes concurrentes del mismo token, exactamente uno gana**; reemitir invalida el anterior;
vencido y justo-antes-de-vencer; el mail queda verificado; una cuenta bloqueada no entra por el
link; y el borde HTTP con las dos respuestas indistinguibles y el rate limit frenando el cuarto
pedido.

Encontrado por los tests, no por la revisión: `generate-keys` llamaba
`clave.model_dump(mode="json")` sobre un `SigningKey`, que **no es un modelo pydantic** —a
propósito, para que un `model_dump()` accidental no volque la privada. El comando estaba roto
en el único camino que importa. Ahora emite los dos JWK parseados, con el aviso por stderr para
no ensuciar lo que se redirige al secret manager.

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
| `hexcore/domain/cqrs/envelope.py` | ✅ **Hecho (Fase 6):** archivo nuevo. El punto de extensión del sobre — registros por clave, `restored_envelope_scope`, `message_correlation_id`. Stdlib puro, sin saber nada de identidad. | 6 | aditivo |
| `hexcore/domain/cqrs/serializer.py` | ✅ **Hecho (Fase 6):** +2 métodos **concretos** para el sobre. Ninguna subclase existente se rompe. | 6 | aditivo |
| `hexcore/infrastructure/cqrs/pydantic_serializer.py` | ✅ **Sin cambios.** Se planeaba un override por simetría y no hace falta: hereda los métodos concretos, que envuelven `serialize`/`deserialize`. | 6 | — |
| `hexcore/infrastructure/workers/consumer.py` | ✅ **Hecho (Fase 6):** `deserialize_envelope` + `restored_envelope_scope` en las tres rutas. `process_task` queda afuera (§3.3). | 6 | aditivo |
| `application/cqrs/in_memory_buses.py`, `infrastructure/cqrs/{rabbitmq,postgres_bus,redis_bus,procrastinate}.py` | ✅ **Hecho (Fase 6):** `serialize()` → `serialize_envelope()` en los seis sitios de encolado, y restauración en los cuatro consumos. `task_queues/*` **no** se toca: tratan el payload como opaco. | 6 | aditivo |
| `hexcore/infrastructure/api/app.py` | ✅ **Hecho (Fase 7):** `AppFeatures` += `auth_context`, `csrf` (default **off**); los dos middlewares de Darwin se registran **antes** de `RequestIDMiddleware` (o sea corren adentro, así que `get_request_id()` ya está poblado); `_con_darwin()` mergea `IDENTITY_EXCEPTION_STATUS_MAP` y la fábrica de `WWW-Authenticate`, importándolos **dentro de la función** para no acoplar la capa `api` a Darwin en tiempo de import. | 7 | aditivo |
| `hexcore/infrastructure/cli.py` | `app.add_typer(darwin_cli, name="identity")`; `ensure_identity_schema_loaded()` en el `env.py` generado. | 8 | aditivo |
| `hexcore/infrastructure/repositories/orms/sqlalchemy/utils.py` | `import_all_models`: `iter_modules` → `walk_packages` (§5.3). Arregla un `DROP TABLE` latente que ya afecta a `hexcore_cron_jobs`. | 8 | fix |
| `hexcore/domain/auth/*`, `hexcore/__init__.py` | Absorbidos y deprecados. | 10 | deprecación |
| `hexcore/_deprecation.py` | ✅ **Hecho (Fase 0):** `REMOVED_IN` → 7.0. | 0 | fix |
| `pyproject.toml` | Extras `darwin`, `darwin-passkey`, agregados a `all`. | 2 / 9 | aditivo |
| `tests/test_optional_dependencies.py` | Filas nuevas en las fases 2, 3, 4, 6 y 9. ⚠️ `argon2-cffi` se importa como `argon2`. ⚠️ **No** se puede chequear contra `pydantic`: no es opcional — `hexcore/__init__.py` lo importa eager, así que esconderlo rompe cualquier import del paquete. | 2-9 | aditivo |
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
