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
Se agrega a `all`. `[darwin-passkey]` suma `webauthn` (hecho, Fase 9).

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

### Fase 9 — El resto de los plugins ✅

Los cinco: `two_factor` ✅, `oauth` ✅, `impersonate` ✅, `passkey` ✅, `organization` ✅.
Con `magic_link` de la Fase 8, Darwin shippea **seis** plugins.

#### `two_factor` ✅ — TOTP, y el punto de extensión del sign-in

Es el primer plugin que **intercepta** un flujo del núcleo en vez de agregar uno al costado, y
por eso es el que descubrió que el sistema de la Fase 8 tenía un hueco.

⚠️ **El hueco: el router de identidad llama a los servicios directo, no despacha comandos.** Un
plugin enganchado sólo al `HookMiddleware` —que es un middleware del bus— **no vería nunca un
sign-in por HTTP**. Se cerró extrayendo el runner de hooks a `run_hooks(plugins, action, phase,
payload)`, que ahora usan los dos caminos: el middleware para lo que pasa por el bus, y los
servicios en sus puntos de extensión declarados.

El punto de extensión es `SIGN_IN_AUTHENTICATED = "user.sign_in.authenticated"`, y corre en el
único lugar donde un segundo factor puede exigirse: **la contraseña ya se validó y la sesión
todavía no existe**. Antes no se sabe quién es el usuario; después ya hay un par de tokens
emitido que habría que revocar — y el que se olvida de revocarlo dejó el 2FA en decorativo. Es
una acción y no un evento porque un hook acá puede **abortar**: un evento se publica después del
hecho y no tiene forma de impedirlo.

`run_hooks` deja pasar los `IdentityError` **sin envolverlos**, y eso es lo que hace todo el
mecanismo posible: el hook lanza `TwoFactorRequiredError` y el borde lo mapea a su status.
Envolverlo en el `RuntimeError` de "el hook falló" convertiría el desafío en un 500. Cualquier
otra excepción sí se envuelve y propaga: falla cerrando.

**Punto de extensión nuevo en el contrato de plugin: `exception_status_map()`.** Las excepciones
del plugin viven en el plugin —el núcleo no tiene por qué conocer los modos de falla del 2FA— y
`create_app` mergea el mapa de los plugins entre el de identidad y el del consumidor. Sin esto,
la excepción de un plugin saldría como un 500 con el traceback, o el consumidor tendría que
mapearla a mano, que es pedirle que sepa los internos del plugin.

**El sign-in se parte en dos, y el primer paso no emite nada.** La alternativa que se ve seguido
—emitir una sesión "parcial" con scope reducido— pone en manos del cliente un token real que
después hay que acordarse de restringir en cada endpoint, y el endpoint que se olvida es el que
convierte el 2FA en decorativo. Acá el primer paso devuelve un 401 con un desafío y **cero**
tokens, cero cookies, cero filas en `session`.

**El desafío vive en `verification` con `purpose="two_factor"`**, no en un JWT: la tabla ya tiene
el canje atómico, así que el desafío es de un solo uso y revocable sin escribir nada nuevo. Un
desafío stateless sería replayeable durante todo su TTL. Lleva el `user_id` adelante separado por
un punto (`{uuid}.{token}`) para poder canjearlo con `consume(identifier, purpose, hash)` sin
agregarle al puerto un `consume_by_hash` que sacaría el identificador de la clave de canje. TTL
de 5 minutos: lo que tarda alguien en buscar el teléfono.

⚠️ **El desafío se consume ANTES de verificar el código.** Al revés, quien tenga el desafío podría
probar códigos indefinidamente sobre el mismo; así, cada intento cuesta un desafío nuevo — o sea
la contraseña.

**TOTP en stdlib, sin `pyotp`.** El algoritmo son treinta líneas —contador de 8 bytes big-endian,
HMAC-SHA1, truncado dinámico, módulo— y el HMAC lo hace la stdlib: una dependencia acá no compra
corrección, compra superficie de cadena de suministro en el camino de autenticación. Mismo
criterio que el códec del sobre firmado. SHA-1 **no es un desvío**: es lo que especifica la RFC
4226 y lo único que implementan Google Authenticator, Authy y 1Password — y el uso de HMAC no
depende de la resistencia a colisiones, que es lo único que SHA-1 tiene roto. Emitir con SHA-256
daría códigos que la app del usuario no puede generar. El test del apéndice D de la RFC 4226 es
el único que prueba que la implementación es correcta y no sólo autoconsistente.

**Ventana de ±1 paso, y `verify_totp` devuelve el paso con el que matcheó, no un booleano.**
Devolverlo es lo que permite persistirlo, y sin eso no hay defensa de replay: un código vale
hasta 90 segundos, así que quien lo lee por encima del hombro o lo saca de un formulario de
phishing lo puede volver a usar. `last_used_step` convierte "es válido" en "es válido y no se
usó". El bucle recorre la ventana entera y **no corta al primer match**: salir temprano haría que
el tiempo de respuesta diga qué paso acertó, o sea cuánto deriva el reloj del usuario.

**El secreto TOTP no se puede hashear** —para verificar un código hay que recalcularlo— así que se
cifra: **JWE compacto `dir` + `A256GCM` de `joserfc`**, que ya es dependencia del extra porque
firma los tokens. Es AEAD, así que el texto cifrado está autenticado: alguien con escritura en la
base no puede sustituir el secreto de un usuario por uno que él conoce sin que el descifrado
falle. Un XOR con una clave derivada dejaría esa puerta abierta, y `cryptography` sería una
dependencia nueva para algo que la que ya está resuelve. La clave se **deriva** de `secret_key`
con una etiqueta propia: reusar el mismo material para cifrar secretos TOTP, firmar sobres y
derivar valores anti-CSRF hace que romper uno rompa los tres.

**Inscribir no activa.** `confirmed_at IS NULL` = inscripto e inactivo. Si inscribir activara el
factor, el usuario que guardó mal el QR queda afuera en el siguiente login y sólo lo saca de ahí
una intervención humana. **Desactivar exige un código válido**, y es la operación que más
protección necesita, no menos: sin eso, quien roba una sesión con el 2FA ya pasado apaga el
segundo factor y se queda con la cuenta. Y no se puede desactivar impersonando: sería la escalada
más barata del sistema.

`UNIQUE` sobre `user_id`, y no es cosmético: dos filas dejarían que el secreto de una inscripción
abandonada siga sirviendo para entrar, y ningún flujo lo borraría nunca. `upsert` borra e inserta
en vez de hacer `ON CONFLICT DO UPDATE` porque una re-inscripción **es** un factor nuevo:
arrastrar el `last_used_step` o los `failed_attempts` del anterior dejaría al usuario nuevo
bloqueado por los intentos del viejo.

`MAX_FAILED_ATTEMPTS = 5`: un OTP de 6 dígitos con ventana ±1 deja 3 códigos válidos de 10⁶, o sea
3 en un millón por intento — con 20 intentos por ventana y reintentos indefinidos el ataque cierra
en horas. 5 y no 3 porque un usuario con el reloj corrido falla dos veces legítimamente. El techo
se chequea **antes de calcular nada**: seguir verificando regala intentos, y calcular el HMAC
igual haría que el tiempo diga si la fila existe.

Lo que el plugin **no** hace, a propósito: no aporta códigos de respaldo. Un código de respaldo es
una credencial de un solo uso y alta entropía, o sea exactamente lo que `verification` ya modela,
así que va como un plugin aparte que dependa de este.

`VerificationPurpose` gana `"two_factor"`. Los propósitos de los plugins se enumeran en el núcleo
igual que `"magic_link"`, y no porque el núcleo los conozca: es el tipo de una columna y un
`Literal` no se extiende desde afuera. Tiparla `str` perdería la garantía justo donde importa,
porque el propósito es parte de la clave de canje.

Tests (`test_darwin_two_factor.py`, 71): el vector de la RFC 4226; estabilidad dentro del paso y
cambio al siguiente; la ventana de ±1 y el rechazo a dos pasos; `after_step`; basura de todos los
largos; tolerancia a espacios y guiones; el `issuer` dos veces en la URI. Del cifrado: ida y
vuelta, el secreto ausente del texto cifrado, nonce distinto por llamada, otra clave no descifra,
y **alterar un byte hace fallar el descifrado** (la propiedad AEAD). De los flujos: inscribir no
activa y el sign-in sigue en un paso; confirmar activa; el primer paso **no deja ninguna fila en
`session`**; contraseña mala sigue dando `InvalidCredentialsError` (el hook corre después);
desafío de un solo uso; el desafío se consume aunque el código sea malo; reemitir invalida el
anterior; vencido; el desafío de otro no sirve; seis desafíos malformados dan 401 y no 500;
**el mismo código no sirve dos veces**; **ocho canjes concurrentes, gana uno**; el paso siguiente
sí sirve; el techo de intentos bloquea incluso al código correcto; un código válido resetea los
intentos; desactivar exige código. Del borde: el 401 sin cookies, el flujo completo por HTTP
(login → estado → inscribir → confirmar → login parcial → canje), 409 del plugin llegando por
`exception_status_map()`, y el rate limit frenando el canje número once. Y dos tests de que el
punto de extensión **no es de `two_factor`**: un bloqueo por país corta el sign-in igual, y un
hook con un bug no deja entrar a nadie.

#### `oauth` ✅ — Authorization Code + PKCE, y la vinculación que no se hace sola

Reusa la tabla `account` del núcleo, que ya está diseñada para esto: `provider_id` +
`account_id` con `UNIQUE`, más las seis columnas de tokens. Aporta una tabla propia sólo para el
`state` en vuelo.

⚠️ **La decisión que más importa: por default NO se vincula por coincidencia de mail.** Es la toma
de cuentas más común de OAuth. Si Ana tiene cuenta local con `ana@ejemplo.com` y el flujo trae una
identidad de proveedor con ese mail, vincularlas automáticamente deja que cualquiera que consiga
registrar `ana@ejemplo.com` en *cualquier* IdP configurado entre a su cuenta — y hay IdPs que no
verifican el mail, u otros donde se puede cambiar sin re-verificar. El default es
`LinkPolicy.NEVER`, y la coincidencia produce un 409 que le dice al usuario que inicie sesión con
su método actual y vincule desde los ajustes. La vinculación explícita es la única segura.

`VERIFIED_EMAIL` existe para despliegues con un único IdP corporativo, y exige **las dos**
verificaciones —la del proveedor y la de la cuenta local— porque cada una sola deja una mitad del
agujero abierta. `ANY_EMAIL` es la insegura, disponible sólo para migraciones desde sistemas que
ya lo hacían, con la advertencia puesta en el código.

**PKCE es obligatorio y `S256`, nunca `plain`.** `plain` manda el verificador en la URL de
autorización, o sea que cualquiera que vea esa URL —historial, log de proxy, `Referer`— puede
canjear el código. La RFC 7636 permite `plain` sólo para clientes que no pueden hacer SHA-256, que
en Python no existen.

**Por qué el `state` tiene tabla propia y no reusa `verification`.** Necesita guardar el
`code_verifier` de PKCE, el `redirect_uri` con el que se inició, y a qué usuario se vincula;
`verification` tiene un `value_hash` y nada más. Y el verificador **no puede** viajar en el
`state` —que es lo que permitiría meterlo en la tabla genérica— porque el `state` va en la URL:
un verificador en la URL anula PKCE por completo. Guardarlo del lado del servidor es toda la
protección.

El `state` se guarda **hasheado** (viaja por la URL y queda en logs del proveedor), es de un solo
uso vía `UPDATE ... WHERE consumed_at IS NULL RETURNING`, está atado al `provider_id` —un `state`
de Google no se canjea en el callback de GitHub— y **se consume antes de hablar con el
proveedor**, así que un `state` inválido no gasta una llamada de red contra un tercero. Un canje
fallido **igual lo consume**: es un vale de un solo uso para *intentar*, y dejarlo vivo permitiría
reintentar indefinidamente.

**El `redirect_uri` se valida dos veces**: contra una allowlist al iniciar, y contra el guardado
en el callback. El proveedor ya valida el suyo, pero eso no cubre dos URIs ambas registradas: sin
el segundo chequeo, un flujo iniciado para una se puede completar en la otra. Con la allowlist
vacía no se valida —deliberado, para desarrollo— y un `StartupStep` lo avisa por log en el
arranque en vez de fallar.

**El perfil sale del `userinfo`, no del `id_token`.** Verificar un `id_token` bien exige traer y
cachear el JWKS de cada proveedor y validar `iss`/`aud`/firma; usarlo sin verificar es peor que no
usarlo, porque viene del mismo canal que un atacante controlaría. El `userinfo` da lo mismo sobre
un canal ya autenticado con un access token que el proveedor emitió recién.

**`email_verified` por default es `False`**, y un proveedor que no lo informa se trata como no
verificado. GitHub lo marca `False` **siempre**, porque `/user` no informa el campo: asumirlo
sería tomar la palabra de algo que el proveedor no dice. Un usuario creado por OAuth copia el
valor del proveedor y no asume `True`: esa afirmación después la usan otros flujos, como el reset
de contraseña.

**Los tokens del proveedor se guardan cifrados** con `SecretBox` (JWE `dir` + `A256GCM`, AEAD,
clave derivada por etiqueta). Son credenciales de otro sistema: un dump que las entregue en claro
es un incidente en la API del tercero además del propio, y el usuario ni se enteraría de que su
cuenta de Google quedó expuesta por una base nuestra. El cifrado se **factorizó** de
`two_factor`: `SecretBox` con `label` es la pieza reusable, y `TotpSecretCipher` quedó como una
subclase de cuatro líneas que fija su etiqueta. Cada propósito tiene su etiqueta y por lo tanto su
clave.

**Desvincular se niega a dejar la cuenta sin ningún método de acceso.** Desvincular el único
proveedor de un usuario que no tiene contraseña lo deja afuera de su propia cuenta, y ese botón
está a un click en cualquier pantalla de ajustes. Y una identidad ya vinculada a otra cuenta **no
se mueve**: moverla dejaría a la primera sin su método de acceso.

**A quién se vincula sale del `state`**, fijado al iniciar el flujo, y no del callback — que lo
controla en parte quien maneja el navegador. Ni vincular ni desvincular se permiten estando
impersonado: sería tomarle la cuenta a la persona que estás impersonando.

Los proveedores son **datos y una función**: tres URLs, el client, los scopes y cómo se lee el
`userinfo`. No hay una clase por proveedor con métodos sobreescribibles, porque lo único que varía
de verdad es la forma del JSON. Vienen preconfigurados Google, GitHub, Microsoft, GitLab y
Discord, como **funciones** —necesitan `client_id` y `client_secret`, y una constante con los
campos vacíos invita a olvidarse de llenar uno—. Las URLs se exigen HTTPS (por HTTP viajarían en
claro el `code` y el `client_secret`), con `localhost` permitido para desarrollo. Google pide
`access_type=offline` por default: sin eso **no devuelve refresh token**, y el access token
guardado deja de servir en una hora sin forma de renovarlo — el detalle que hace que la
integración parezca funcionar en el test y falle al día siguiente. No hay descubrimiento por
`.well-known`: sería una llamada de red en el arranque contra un tercero, y las URLs de los
proveedores grandes no cambian.

El cliente HTTP está detrás de un puerto (`AbstractOAuthHttpClient`) con un adaptador de `httpx`
en el extra nuevo **`[darwin-oauth]`**. El puerto no es ceremonia: los 69 tests ejercitan el flujo
completo con un doble que puede *mentir* —devolver un mail que no verificó, un `account_id`
distinto, un 400— sin levantar un servidor ni necesitar el extra. El adaptador pone timeout
explícito (sin él `httpx` espera indefinidamente y un proveedor colgado ocupa workers),
`follow_redirects=False` (un `token_url` que redirige mandaría el `client_secret` a otro host),
`Accept: application/json` (GitHub responde form-urlencoded si no se le pide), y **no propaga el
cuerpo de error del proveedor** al usuario: puede traer el `client_id` o un fragmento del secreto.

⚠️ **El callback devuelve JSON, no un redirect al frontend.** Un redirect con los tokens en el
fragmento o en la query los deja en el historial del navegador y en el `Referer` de la página
siguiente.

El puerto y la entidad del `state` viven en `domain.py` y no en `repository.py`, y eso lo
descubrió `tests/test_optional_dependencies.py`: `repository.py` importa sqlalchemy en el nivel
superior, así que tenerlos ahí hacía que importar el servicio exigiera el extra `[sql]`.

Tests (`test_darwin_oauth.py`, 69): PKCE en el rango de la RFC, el desafío no revela el
verificador, `S256` fijo. De los proveedores: los cinco preconfigurados se arman, el secreto no
aparece en el `repr`, HTTP se rechaza y `localhost` se permite, `email_verified` normaliza el
string `"true"`, GitHub nunca lo marca verificado, dos proveedores con el mismo `id` se rechazan.
Del inicio: la URL lleva los ocho parámetros de la spec, **el verificador no está en la URL** y el
desafío de la URL corresponde al verificador guardado, el `state` va hasheado, un
`redirect_uri` fuera de la allowlist se rechaza. Del callback: crea la primera vez y entra la
segunda, el canje recibe el verificador y el secreto, el perfil sale del `userinfo` con el access
token, `email_verified` se respeta. Del `state`: un solo uso, inventado, **no se llama al
proveedor con un `state` malo**, vencido, `redirect_uri` que no coincide, y **ocho callbacks
concurrentes donde gana uno**. De la vinculación: **el test de la toma de cuentas** (Ana tiene
cuenta, el atacante trae su mail, no entra) y que el rechazo no deja nada vinculado a medias; las
dos verificaciones de `VERIFIED_EMAIL` por separado; `ANY_EMAIL` documentada como lo que es; a
quién se vincula sale del `state` (probado con dos usuarios); una identidad ajena no se mueve. De
los tokens: se guardan cifrados, se descifran para llamar a la API, el vencimiento sale del
`expires_in`, y sin refresh token no explota. De desvincular: no deja la cuenta sin acceso, con
contraseña sí se puede. Del borde: las siete rutas, el 404 del proveedor no configurado, el 409
del mail coincidente llegando por `exception_status_map()`, y el flujo de vinculación completo por
HTTP. Más un test de que `oauth` y `two_factor` conviven en el mismo registro sin chocar tablas ni
mapas.

#### `impersonate` ✅ — el plugin que justifica los dos principales

Es el plugin que existe para probar que la decisión central de Darwin era la correcta: que
`AuthContext` tenga **dos** principales —`actor`, quien ejecuta, y `subject`, a quién afecta— en
vez de un `user_id` con un flag al costado. Con eso, una impersonación es una sesión normal con los
dos campos distintos, y todo el resto —revocación, transporte, CSRF, auditoría, el sobre que cruza
la cola— funciona sin saber que hay una impersonación en curso.

**No aporta ninguna tabla.** La fila de `session` ya lleva `actor_user_id`, `subject_user_id`,
`impersonation_reason`, `impersonation_granted_by` e `impersonation_expires_at` desde la Fase 3,
justamente para que este plugin no tuviera que inventar nada. Lo que agrega es la **autorización**
y los tres endpoints.

**La política es un puerto, no una lista de scopes.** Un scope alcanza para "¿puede impersonar?" y
no para "¿puede impersonar *a esta persona*?", que es la pregunta que importa: un agente de soporte
que puede entrar como cualquier cliente no debería poder entrar como el CTO. El default
—`ScopeImpersonationPolicy`— cubre el caso común y cierra las cuatro puertas, en este orden:

1. **No hay cadenas.** Si A impersona a B y desde ahí impersona a C, la auditoría de la segunda
   dice que el actor es B — que nunca hizo nada. Es la forma más barata de borrar la traza, y por
   eso se corta antes que cualquier otro chequeo.
2. **No se impersona a uno mismo.** No es peligroso, pero produciría una sesión con
   `actor == subject` marcada como impersonada, que es exactamente el estado que el validador de
   `AuthContext` prohíbe.
3. **El actor necesita el scope**, consultado del actor y nunca del subject.
4. **El sujeto puede estar protegido**: por tener él mismo el permiso de impersonar (escalada
   lateral con la traza borrada, protegido por default) o por tener un scope de la lista de
   protegidos.

⚠️ **Una sesión impersonada NO se puede refrescar, y ese es el mecanismo que hace real el techo de
60 minutos.** El chequeo está en `SessionService.refresh` —es una propiedad del núcleo, porque la
fila ya sabe si es impersonada— y va **antes** de `consume_for_rotation`: consumir la fila y
después rechazar dejaría la sesión inutilizable por lo que le queda de hora. Sin este rechazo, el
techo sería "60 minutos por rotación", o sea ninguno: el operador extendería la sesión
indefinidamente sin volver a pedir permiso ni dejar un segundo registro de auditoría.
`IMPERSONATION_CAP` pasó a ser una constante nombrada, con el porqué del número en su comentario.

**Impersonar no presta permisos.** Los scopes del token impersonado son los del **actor**: el
operador ve lo que el otro ve y puede hacer lo que él mismo puede hacer. Si fueran los del subject,
impersonar a un admin daría los permisos del admin, y la política que protege a los admins sería
la única defensa — una capa donde tendría que haber dos.

**La sesión del operador no se toca.** Empezar no la revoca, así que terminar es descartar el token
de impersonación: no hay que reconstruir nada, y si el operador cierra la pestaña la impersonación
muere sola con su techo. Sin esta propiedad, "volver" sería un segundo intercambio que puede fallar
a mitad de camino.

**La política decide antes de que exista cualquier sesión.** Si autorizara después de crearla, un
rechazo dejaría una sesión impersonada huérfana que hay que revocar — y el camino de limpieza es
el que falla. Hay un test que cuenta las filas de `session` antes y después de un rechazo.

**Un principal de sistema no puede impersonar**: `"cron:cerrar-registros"` no es una persona, no
tiene fila en `user`, y no hay a quién responsabilizar del acceso.

Los comandos **no llevan `actor_id`**: sale del contexto ambiental. Un campo que el llamador
rellena es un campo que el llamador puede mentir, y acá mentirlo sería impersonar en nombre de
otro. Que el contexto llegue al worker lo resuelve el sobre firmado de la Fase 6 — y hay un test
que lo comprueba de punta a punta, incluido que re-adjuntar el sobre a otro mensaje no verifica.

`describe()` **lee la fila de `session`**, y es la segunda excepción deliberada al "cero DB en el
camino caliente" después de `/auth/me`. El motivo y el vencimiento real no viajan en el token: el
motivo es texto de largo arbitrario, y el vencimiento del techo sería un claim más pagado en cada
petición para un caso raro. Es una lectura por carga de página, no por request.

El `StartupStep` del plugin **avisa si no hay sink de auditoría**. Avisa y no falla —la auditoría
es opcional en el resto de Darwin, y hacerla obligatoria acá rompería un despliegue que
funciona— pero una impersonación sin auditoría es exactamente lo que este plugin promete que no
pasa, así que el aviso va en el arranque y no en un docstring.

Tests (`test_darwin_impersonate.py`, 48): las cuatro puertas de la política, cada una por
separado; la protección de impersonadores apagable, con el test documentando qué se pierde; una
política propia; un `extra` mal formado que no da 500 ni pase libre. De los requisitos: sin
contexto, motivo vacío en tres formas, sujeto inexistente con el error genérico, principal de
sistema. De la sesión: los dos principales en la fila, el techo de una hora, `act`/`sub`/`imp` en
el token, **impersonar no presta permisos**, el contexto reconstruido consulta al actor, y la
sesión del operador sobrevive. Del refresh: se rechaza, **el rechazo no consume la fila** ni
dispara la detección de reuso, y una sesión normal sí se rota. De la auditoría: inicio y fin con
los dos principales, el `operator_session_id` para poder correlacionar, y que un rechazo no deja
sesión. Del sobre: el contexto impersonado cruza la cola con los dos principales, sale con
`transport="worker"`, y no verifica re-adjuntado a otro mensaje. Del borde: el flujo completo, 403
sin el scope, 401 sin sesión, 422 con motivo vacío, 409 al impersonarse a sí mismo, 409 al
terminar una sesión normal, y **403 al refrescar impersonando**.

Encontrado por un test, no por la revisión: `/{user_id}` estaba declarado **antes** de `/stop`, y
FastAPI resuelve las rutas en orden de registro — así que `POST /auth/impersonate/stop` matcheaba
la ruta paramétrica e intentaba parsear `"stop"` como UUID, devolviendo 422 en vez de terminar la
impersonación.

#### `passkey` ✅ — WebAuthn, y lo único que se guarda es público

Es el plugin con la mejor propiedad de seguridad del módulo, y la propiedad es esta: **lo que se
guarda es público**. No hay nada en `darwin_passkey` que un atacante con un dump de la base pueda
usar para autenticarse, ni acá ni en otro sitio.

| Método | Qué se guarda | Un dump sirve para… |
| :-- | :-- | :-- |
| Contraseña | hash de Argon2id | atacar por diccionario, offline |
| TOTP | secreto compartido, cifrado | generar códigos, si además tenés la clave de la app |
| **Passkey** | **clave pública** | **nada** |

Y encima el navegador la ata al origen, así que un sitio clonado no puede reenviar la aserción. Es
la razón por la que este plugin es el único de Darwin que no tiene ningún secreto que proteger.

**Por qué acá hay una dependencia y en `two_factor` no.** El TOTP son treinta líneas de `hmac` y
aritmética: una librería no compra corrección, compra superficie de cadena de suministro. WebAuthn
es lo contrario —CBOR, claves COSE, cuatro formatos de attestation, cadenas de certificados, un
contador de firmas— y escribirlo a mano sería criptografía propia en el camino de autenticación. Va
`py_webauthn` en el extra nuevo **`[darwin-passkey]`**, detrás de un puerto.

⚠️ **El contador de firmas es la única señal de compromiso que WebAuthn da**, y el puerto existe
sobre todo para poder probarla: con hardware real, un contador que no avanza es imposible de
reproducir. Muchas implementaciones lo descartan porque "algunos autenticadores no lo incrementan";
acá se distingue el autenticador que **nunca** lo usa (contador 0 siempre, se acepta) del que lo
usaba y dejó de avanzar (se rechaza y no se abre la sesión). El chequeo es sobre la **regresión**,
no sobre que el número sea mayor que cero. Y el contador **no sube si la firma no valida**: subirlo
antes de verificar dejaría que una firma inválida desincronice al autenticador legítimo — negación
de servicio contra una cuenta, gratis.

**El desafío se guarda en claro, y a diferencia del resto de Darwin eso es lo correcto.** Un
desafío WebAuthn es un nonce público: viaja al navegador y vuelve, y conocerlo no permite
autenticarse porque hace falta la clave privada del autenticador. No es un token de sesión. Hubo
una primera versión que lo hasheaba —copiando el patrón de `verification`— y tenía un defecto de
diseño: con el desafío hasheado, el `expected_challenge` que el verificador compara tiene que salir
del `clientDataJSON` del propio cliente (el hash sólo sirve para *encontrar* la fila), así que la
comparación queda entre un valor y sí mismo. Sigue siendo sólida —el hash coincidió, o sea que el
valor es el que el servidor emitió— pero es circular de leer, y un chequeo de seguridad que hay que
razonar dos veces para ver que sirve es un chequeo que alguien va a "simplificar". Guardándolo en
claro, el `expected_challenge` sale de la fila y la comparación es la que el protocolo pide.

Dos tablas y no una: las credenciales viven para siempre y los desafíos viven treinta segundos.
Juntas darían una tabla donde el 99% de las filas son basura de un minuto atrás.

**El `user_id` sale del desafío, nunca del cuerpo del request.** Aceptarlo del cliente dejaría
registrar una credencial propia en la cuenta de otro — toma de cuenta directa, en un endpoint que
parece administrativo. Y si el desafío se emitió para un usuario concreto, la credencial tiene que
ser suya: sin ese chequeo, alguien pide un desafío "para Ana" y lo completa con su propia
credencial, y la firma valida.

`excludeCredentials` va siempre con lo que el usuario ya tiene: sin eso el navegador le ofrece
registrar de nuevo una credencial existente y el flujo falla al guardar, con un error de base en
vez de un mensaje. Una credencial ya registrada **no se mueve** de cuenta: si es de otro, moverla le
saca un método de acceso; si es del mismo, sobreescribir el contador reiniciaría la detección de
clonado.

**Borrar la última credencial se rechaza si no hay otro método de acceso** —contraseña o proveedor
vinculado, consultado en `account`, el mismo chequeo que hace `oauth.unlink`. El botón está a un
click en cualquier pantalla de ajustes y el usuario que lo aprieta no tiene forma de volver.
Borrar la de otro devuelve el **mismo** error que "no existe": un 403 distinto le confirmaría a
quien prueba ids que la credencial existe.

Del adaptador: **`origins` es obligatorio** —es el chequeo anti-phishing, y se exige al construir,
no al primer login—; `attestation="none"` por default (pedir attestation obliga a mantener cadenas
de certificados de fabricante y rechaza autenticadores de plataforma válidos);
`residentKey="preferred"` (`required` rechazaría llaves que sirven como segundo factor, y
`preferred` habilita igual el login sin usuario declarado en las que pueden); y el detalle del
error **va al log, no a la respuesta** — `py_webauthn` dice exactamente qué chequeo falló, y
devolverlo es darle a quien prueba el camino para el siguiente intento.

`POST /auth/passkey/authenticate/options` **no revela si la cuenta existe**: un mail desconocido
devuelve opciones sin `allowCredentials`, la misma forma exacta que el flujo sin mail. El resumen
que sale al cliente **no lleva `credential_id` ni `public_key`**: no son secretos, pero no le
sirven a la interfaz, y menos respuesta es menos superficie. Registrar y borrar **no se permiten
estando impersonado**: registrar una credencial propia en la cuenta de la persona que estás
impersonando es tomarle la cuenta, y de forma permanente.

Un `StartupStep` avisa si el `rp_id` es `localhost`: es lo correcto en desarrollo —el único host
que los navegadores aceptan sin HTTPS— y shippearlo a producción deja el login roto para todos, con
un error del navegador que no dice qué está mal.

Tests (`test_darwin_passkey.py`, 65): base64url en las tres direcciones. Del registro: el flujo
completo, **el `user_id` sale del desafío**, `excludeCredentials`, credencial ya registrada,
credencial de otro que no se mueve, verificación fallida que no guarda. Del desafío: se guarda en
claro con el valor exacto, **el `expected_challenge` sale de la fila** (lo asevera el autenticador
falso, así que si el servicio le pasara el del cliente el test falla), un solo uso, registro↔login
en los dos sentidos, vencido, inventado, seis respuestas corruptas que dan 401 y no 500, y **ocho
logins concurrentes donde gana uno**. Del contador: avanza, **no avanza → corta**, retrocede →
corta, el que nunca lo usa se acepta tres veces seguidas, el que lo usaba y volvió a 0 se rechaza,
y no sube si la firma no valida. De la autenticación: abre la sesión, con usuario limita las
credenciales ofrecidas, sin usuario descubre quién es, la credencial de otro no completa un desafío
dirigido, una desconocida no entra, una sin id se rechaza. Del ciclo de vida: listar por usuario,
borrar con contraseña, **no borrar la última sin otro método**, con dos sí, la de otro da
not-found. Del adaptador real: sin `origins` no se construye, sin `rp_id` tampoco, las opciones son
válidas y el desafío de la URL corresponde, dos desafíos no se repiten, y una respuesta basura da
el error genérico. Del borde: el flujo completo por HTTP, el resumen sin la credencial, el mail
desconocido indistinguible, 401 sin sesión, **401 con contador clonado**, 409 al borrar la última,
404 con una inexistente. Más un test de que los cuatro plugins conviven en un registro con sus
cinco mixins sin chocar.

#### `organization` ✅ — multi-tenancy, y las invariantes que casi nadie sostiene

Tres tablas —`organization`, `member`, `invitation`— y una jerarquía de tres roles: `owner` >
`admin` > `member`. **No hay un sistema de permisos por organización**, y es deliberado: HexCore ya
tiene `RoleRegistry` para que el consumidor declare su modelo de autorización, y un segundo sistema
adentro del plugin le daría dos lugares donde mirar cuando algo no autoriza. Lo que el plugin
garantiza es **quién puede administrar a quién**; qué puede hacer un `member` en tu producto es
tuyo.

`OrgRole.rank` existe porque el orden alfabético de `admin` < `member` < `owner` es exactamente el
equivocado: un `>` sobre los nombres pasaría los tests por casualidad en algunos pares. Y
`outranks` es **estricto**: un par no administra a un par.

⚠️ **Invariante 1: una organización nunca queda sin `owner`.** Sacar o degradar al último la vuelve
inadministrable —nadie puede invitar, nadie puede cambiar roles— y salir de ahí requiere un `UPDATE`
a mano en producción.

La primera implementación contaba los owners en la base y después actualizaba, con un comentario que
decía que contar en la base era lo que hacía la operación segura. **Eso era falso, y lo demostró un
test**: contar y después escribir sigue siendo check-then-act, y dos degradaciones concurrentes
—cada una viendo "hay 2 owners"— dejaron la organización con **cero**. La corrección es meter la
condición adentro del `WHERE` del `UPDATE`/`DELETE`, como subconsulta correlacionada `EXISTS`: una
sola sentencia, y la decisión la toma la base. Es el mismo patrón que `consume_for_rotation` y
`consume_step`, y el episodio deja claro que "la consulta va a la base" no es lo mismo que "la
operación es atómica". `count_by_role` sigue existiendo pero quedó marcado como **informativo**:
sirve para mostrarle al usuario "sos el único owner" antes de que apriete el botón, no para decidir.

⚠️ **Invariante 2: nadie asciende a alguien por encima de sí mismo, ni actúa sobre un par o un
superior.** Tres chequeos, cada uno cerrando una escalada: no se invita con un rol mayor al propio
(un `admin` que invita un cómplice como `owner` es la escalada más barata del modelo, y pasa por un
endpoint que suena inofensivo), no se asciende uno mismo, y no se degrada ni se saca a un par ni a
un superior.

⚠️ **Invariante 3: la invitación está atada al mail, y el mail tiene que estar verificado.**
Reenviar el link de invitación es exactamente lo que la gente hace; sin el chequeo, quien lo reciba
entra con el rol que se le había dado a otro. Y sin exigir la verificación, alguien registra una
cuenta con el mail del invitado y le roba la invitación **sin acceso a la casilla**. El mail se
chequea **antes** de consumir la invitación: consumirla primero y rechazar después la gastaría, y el
invitado legítimo tendría que pedir otra por un intento que no era suyo.

El token de invitación se guarda **hasheado** —el link viaja por mail y queda en el buzón, en los
logs del proveedor y en el historial del cliente— y se canjea con un `UPDATE ... WHERE status =
'pending' RETURNING`: sin eso, dos aceptaciones concurrentes crearían dos membresías y el `UNIQUE`
rechazaría la segunda con un error de base en vez de con un mensaje. Revocar **marca** la fila y no
la borra: una invitación revocada es información de auditoría — dice que alguien invitó y después se
arrepintió, y `invited_by` dice quién.

**`require_role` es una lectura por operación con alcance de organización, y no va en el token.** Un
`org_role` en el access token queda obsoleto cuando alguien degrada a un `admin`, y seguiría
valiendo hasta que el token venza — que es exactamente lo que no se quiere de un cambio de permisos.

**El `owner` se crea en el mismo flujo que la organización**, no en un paso aparte: una organización
sin `owner` es inadministrable desde el segundo cero, y dos pasos separados garantizan que alguna
vez uno falle en el medio. **El slug no se puede cambiar**: rompe cada link guardado, cada bookmark
y cada integración que lo tenga fijo, así que el campo no está en el cuerpo del `PATCH`.

**Irse uno mismo no requiere ningún rol** —salvo ser el último `owner`—: nadie tiene que pedir
permiso para dejar de trabajar en un lugar. Y **la lista de miembros exige ser miembro**: un
endpoint que la devuelve sin chequear es una fuente de datos para prospección y para ingeniería
social. Aceptar una invitación **no se permite impersonando**: metería a la persona impersonada en
una organización sin que se enterara.

Tests (`test_darwin_organization.py`, 80): el orden de los roles, y un test que documenta que el
alfabético sería el equivocado; nueve casos de `slugify` y que nunca deja caracteres de URL. De
crear: el creador queda `owner`, slug propio, slug repetido, metadata, y que el slug no se mueve al
actualizar. De autorización: no-miembro, `member` que no llega a `admin`, `owner` que llega a todo,
la lista de miembros que no es pública. De invitar: el flujo completo, **el token hasheado**, un
`member` que no puede invitar, **un `admin` que no puede invitar como `owner`**, hasta su propio
nivel sí, miembro existente, normalización del mail, techo de miembros. De aceptar: **reenviar el
link no sirve**, el rechazo no gasta la invitación, **un mail sin verificar no acepta**, un solo
uso, vencida, token inventado, y **ocho aceptaciones concurrentes donde gana una**. De revocar:
invalida, deja rastro con `invited_by`, dos veces falla, la de otra organización da el mismo error
que "no existe". Del último owner: no se degrada, no se saca, con dos uno se puede ir, **dos
degradaciones concurrentes dejan al menos uno**, borrar la organización entera sí se permite. De la
jerarquía: un `admin` no se asciende, no degrada a un par, no degrada al `owner`, sí administra a un
`member`, no saca a otro `admin`. De irse: solo sí, no-miembro no, un `member` no saca a otro,
sacar y volver a invitar funciona. De multi-tenancy: la misma persona con roles distintos en dos
organizaciones. Del borde: el flujo completo por HTTP, **que `/organizations/invitations/accept` no
choque con la ruta paramétrica** (tienen la misma cantidad de segmentos), 401 sin sesión, 403 de
no-miembro, 409 de slug repetido, 409 al degradar al último owner, las pendientes sin el token, y
que el `PATCH` ignore un slug mandado. Más un test de que **los seis plugins conviven** en un
registro con sus siete mixins y sus 29 excepciones mapeadas.

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
