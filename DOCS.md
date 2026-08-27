# Documentación HexCore: Estructura, Documentos, Repositorios y Abstracciones

## 1. Estructura de Directorios y Documentos

HexCore organiza el código siguiendo los principios DDD y arquitectura hexagonal. Los principales componentes son:

- **src/domain/**  
  Módulos de dominio, cada uno con entidades, repositorios, servicios, objetos de valor, eventos, enums y excepciones.
  ```
  src/domain/{modulo}/
    ├─ __init__.py
    ├─ entities.py
    ├─ repositories.py
    ├─ services.py
    ├─ value_objects.py
    ├─ events.py
    ├─ enums.py
    └─ exceptions.py
  ```

- **src/application/**  
  Casos de uso y DTOs para orquestar la lógica de negocio.

- **src/infrastructure/**  
  Implementaciones técnicas: ORM/ODM, CLI, caché, base de datos, repositorios, unit of work.

- **src/infrastructure/database/**
  - **models/**: Modelos SQLAlchemy (relacional).
  - **documents/**: Documentos Beanie (ODM para MongoDB).

- **hexcore/darwin/** — módulo de identidad nativo.
  ```
  hexcore/darwin/
    ├─ __init__.py          # fachada lazy (_EXPORTS + PEP 562)
    ├─ __init__.pyi          # stubs generados (scripts/gen_stubs.py)
    ├─ domain/
    │   ├─ context.py        # AuthContext, Principal, current_auth, require_auth
    │   ├─ entities.py       # User, IdentitySession, Account, Verification
    │   ├─ events.py         # UserRegisteredEvent, SessionCreatedEvent, …
    │   ├─ exceptions.py     # IdentityError, AuthenticationError, …
    │   ├─ permissions.py    # Role, Permission, RoleRegistry
    │   ├─ plugins.py        # DarwinPlugin, HookBinding, HookPhase, ShortCircuit
    │   ├─ ports.py          # Abstract*Repository, AbstractClock, AbstractPasswordHasher
    │   └─ value_objects.py  # Email, AccessTokenClaims, TokenPair, TokenType
    ├─ application/
    │   ├─ commands.py       # SignUp, SignIn, VerifyEmail, RefreshSession, …
    │   ├─ config.py         # IdentityConfig, TokenConfig, CookieConfig, PasswordPolicy
    │   ├─ container.py      # configure_identity, get_identity_container, reset_identity
    │   ├─ hooks.py          # HookMiddleware, run_hooks
    │   ├─ plugins.py        # PluginRegistry, PluginError
    │   └─ services.py       # IdentityService, SessionService
    ├─ infrastructure/
    │   ├─ api/
    │   │   ├─ dependencies.py  # provide_auth, require_authenticated, require_scopes, …
    │   │   ├─ middlewares.py   # AuthContextMiddleware, CsrfMiddleware
    │   │   └─ routers.py      # build_identity_router
    │   ├─ orms/
    │   │   ├─ selection.py     # resolve_storage_backend
    │   │   ├─ sqlalchemy/      # modelos, mixins, repositorios, esquema
    │   │   └─ beanie/          # documentos, repositorios, esquema
    │   ├─ clock.py          # SystemClock, FixedClock
    │   ├─ envelope.py       # AuthEnvelopeCodec, AuthEnvelopeRestorer
    │   ├─ hashing.py        # Argon2PasswordHasher, hash_token, generate_token
    │   ├─ keys.py           # SigningKey, AbstractKeyStore, StaticKeyStore
    │   ├─ lifespan.py       # IdentityStep, SessionReaperStep, identity_startup_steps
    │   ├─ revocation.py     # CacheRevocationList, GenerationGuard
    │   ├─ tokens.py         # JoserfcTokenIssuer, JoserfcTokenVerifier
    │   └─ transports.py     # CookieTransport, BearerTransport, TransportResolver
    ├─ plugins/
    │   ├─ storage.py        # resolve_storage_backend, plugin_repositories
    │   ├─ magic_link/       # MagicLinkPlugin
    │   ├─ two_factor/       # TwoFactorPlugin
    │   ├─ oauth/            # OAuthPlugin
    │   ├─ impersonate/      # ImpersonatePlugin
    │   ├─ passkey/          # PasskeyPlugin
    │   └─ organization/     # OrganizationPlugin
    └─ testing/              # fixtures y fakes para tests de identidad
  ```

- **tests/**  
  Pruebas para cada módulo de dominio e infraestructura.

---

## 0. Arranque: una app HexCore en una pantalla

Este es el arranque completo de una aplicación estándar. Todo lo que aparece aquí tiene un
default que funciona, así que lo que no necesites lo puedes quitar.

```python
# main.py
from hexcore.fastapi import build_lifespan, create_app, SqlEngineStep

app = create_app(
    lifespan=build_lifespan(SqlEngineStep()),
    routers=[usuarios_router, tickets_router],
)
```

`create_app()` a secas ya da una app usable: título y versión salen de `ServerConfig`, CORS
de `config.allow_origins`, y `/health` (liveness) y `/health/ready` (readiness, con sondas
reales a SQL/Redis/Mongo) existen. También trae el middleware `X-Request-ID`, el de timing y
el mapeo de excepciones de dominio a HTTP.

Para desactivar algo, hay un solo objeto de interruptores:

```python
from hexcore.fastapi import AppFeatures

app = create_app(features=AppFeatures(cors=False))
```

### Los tres imports

Hay un módulo fachada por tarea. Las rutas largas siguen funcionando; estas son las que
usa esta documentación.

```python
import hexcore.fastapi as hx    # create_app, build_lifespan, providers, middlewares, health
import hexcore.cqrs as cqrs     # Command, Query, handlers, decoradores, buses, worker, cron
import hexcore.sql as sql       # init_engine, session_scope, uow_scope, Base, DTOs de query
```

### El worker, en una llamada

```python
# worker.py
import hexcore.cqrs as cqrs

async def main() -> None:
    consumer = cqrs.CQRSConsumer(command_bus, event_bus)
    register_hexcore_procrastinate_tasks(procrastinate_app, consumer)

    await cqrs.run_procrastinate_worker(
        procrastinate_app,
        queues=["default", "reactive"],
        scheduler=cqrs.DynamicScheduler(repo, enqueuer, lock_provider=lock),
        on_startup=[lambda: cqrs.seed_cron_jobs(CRON_JOBS)],
    )
```

Si cualquiera de los dos bucles muere, el runner cancela el otro y el proceso sale con
`WorkerDied`, para que el orquestador lo reinicie completo: correr con un bucle caído
—encolar sin consumir, o al revés— es peor que caerse. `SIGTERM` se traduce a drenaje
ordenado.

### Fuera de un request

Los scopes de `hexcore.sql` sirven en workers, tasks, cron, scripts y seeds:

```python
import hexcore.sql as sql

async with sql.session_scope() as session:       # sesión pelada, sin construir el UoW
    ...

async with sql.uow_scope() as uow:               # UoW sin abrir: el use case hace su `async with`
    await CerrarTicketUseCase(uow).execute(request)
```

`session_scope` no construye el UoW a propósito: construirlo corre el auto-discovery e
instancia **todos** los repositorios de dominio, un coste absurdo para leer una tabla de
infraestructura.

### Nombres canónicos

Conviven dos nombres para varios conceptos por retrocompatibilidad
(`AbstractCommandBus`/`ICommandBus`, `AbstractSerializer`/`ISerializer`, etc.). Los
canónicos son los `Abstract*`, y son los únicos que exponen las fachadas y usa esta
documentación. Los alias `I*` siguen importables por su ruta de siempre.

---

## 1.1 Configuración v2: root-first y discovery explícito

En v2, HexCore no adivina rutas de repositorios. Debes declararlas en configuración.

### Config recomendada

Archivo `config.py` en la raíz:

```python
from hexcore.config import ServerConfig

config = ServerConfig(
    repository_discovery_paths={
        "myapp.features.users.infrastructure.repositories",
        "myapp.features.orders.infrastructure.repositories",
    }
)
```

### Prioridad de carga de configuración (`LazyConfig`)

1. `HEXCORE_CONFIG_MODULE`
2. `HEXCORE_CONFIG_MODULES`
3. `LazyConfig.set_config_modules(...)`
4. módulo `config` por defecto

### Comportamiento de UoW

- Si `repository_discovery_paths` está vacío o no contiene repositorios válidos, la inicialización de UoW falla con mensaje diagnóstico.
- El `event_dispatcher` se toma desde `LazyConfig.get_config().event_dispatcher` durante la construcción del UoW.

---

## 1.2 Templates de estructura vía CLI

El comando `hexcore init` soporta templates:

```sh
hexcore init mi_proyecto --template hexagonal
hexcore init mi_proyecto --template vertical-slice
```

### Template `hexagonal`

- `src/domain`
- `src/application`
- `src/infrastructure`
- `src/infrastructure/database/models`
- `src/infrastructure/database/documents`
- `tests/domain`

### Template `vertical-slice`

- `src/features`
- `src/shared/domain`
- `src/shared/application`
- `src/shared/infrastructure`
- `src/shared/infrastructure/database/models`
- `src/shared/infrastructure/database/documents`
- `tests/features`

Ambos templates crean `config.py` en raíz y configuran Alembic para migraciones.

---

## 2. Abstracciones de Entidades y Eventos

### BaseEntity

Clase base para entidades del dominio. Provee gestión de atributos comunes y manejo de eventos de dominio.

```python
from hexcore.domain.base import BaseEntity

class User(BaseEntity):
    id: UUID
    name: str
```

Las entidades heredan BaseEntity para activar el sistema de eventos y gestión de identidad.

---

### DomainEvent y eventos de entidad

Abstracciones para eventos del dominio y eventos de ciclo de vida de entidades.

```python
from hexcore.domain.events import DomainEvent, EntityCreatedEvent

class UserCreatedEvent(EntityCreatedEvent[User]):
    pass

user = User(...)
event = UserCreatedEvent(entity_id=user.id, payload={"name": user.name})
```

Estos eventos pueden ser disparados por entidades para notificar cambios significativos en el modelo de dominio.

---

## 3. Implementaciones de Repositorios: SQLAlchemy y Beanie ODM

### SQLAlchemyCommonImplementationsRepo

Repositorio genérico para modelos SQLAlchemy. Provee CRUD y requiere especificar la entidad, el modelo, la excepción "not found" y la unidad de trabajo.

```python name=hexcore/infrastructure/repositories/implementations.py
class SQLAlchemyCommonImplementationsRepo(BaseSQLAlchemyRepository[T], HasBasicArgs[T, M], t.Generic[T, M]):
    """
    Implementación común para repositorios SQL usando SQLAlchemy.
    Métodos principales:
      - get_by_id
      - list_all
      - save
      - delete
    """
    @property
    def model_cls(self) -> type[M]:
        raise NotImplementedError("Debes definir la clase modelo")

    async def get_by_id(self, entity_id: UUID) -> T:
        model = await sql_db_get(self.session, self.model_cls, entity_id, self.not_found_exception(entity_id))
        return await to_entity_from_model_or_document(model, self.entity_cls, self.fields_resolvers)

    async def list_all(self) -> t.List[T]:
        models = await sql_db_list(self.session, self.model_cls)
        return [await to_entity_from_model_or_document(model, self.entity_cls, self.fields_resolvers) for model in models]

    async def save(self, entity: T) -> T:
        saved = await sql_save_entity(self.session, entity, self.model_cls, fields_serializers=self.fields_serializers)
        return await to_entity_from_model_or_document(saved, self.entity_cls, self.fields_resolvers)

    async def delete(self, entity: T) -> None:
        await sql_logical_delete(self.session, entity, self.model_cls)
```

**Ejemplo de uso:**

```python
class UserRepository(SQLAlchemyCommonImplementationsRepo[UserEntity, UserModel]):
    def __init__(self, uow):
        super().__init__(
            entity_cls=UserEntity,
            model_cls=UserModel,
            not_found_exception=UserNotFoundException,
            fields_resolvers=None,
            fields_serializers=None,
            uow=uow
        )
```

---

### BeanieODMCommonImplementationsRepo

Repositorio genérico para documentos Beanie ODM (MongoDB). Provee CRUD y requiere la entidad, el documento, la excepción "not found", los resolvers y la unidad de trabajo.

```python name=hexcore/infrastructure/repositories/implementations.py
class BeanieODMCommonImplementationsRepo(IBaseRepository[T], HasBasicArgs[T, D], t.Generic[T, D]):
    """
    Implementación común para repositorios NoSQL usando Beanie ODM.
    Métodos principales:
      - get_by_id
      - list_all
      - save
      - delete
    """
    @property
    def document_cls(self) -> t.Type[D]:
        raise NotImplementedError("Debe implementar la propiedad document_cls")

    async def get_by_id(self, entity_id: UUID) -> T:
        document = await nosql_db_get(self.document_cls, entity_id)
        if not document:
            raise self.not_found_exception(entity_id)
        return await to_entity_from_model_or_document(document, self.entity_cls, self.fields_resolvers, is_nosql=True)

    async def list_all(self) -> t.List[T]:
        documents = await nosql_db_list(self.document_cls)
        return [await to_entity_from_model_or_document(doc, self.entity_cls, self.fields_resolvers, is_nosql=True) for doc in documents]

    @register_entity_on_uow
    async def save(self, entity: T) -> T:
        saved = await nosql_save_entity(entity, self.document_cls, self.fields_serializers)
        return await to_entity_from_model_or_document(saved, self.entity_cls, self.fields_resolvers, is_nosql=True)

    async def delete(self, entity: T) -> None:
        return await nosql_logical_delete(entity.id, self.document_cls)
```

**Ejemplo de uso:**

```python
class UserRepository(BeanieODMCommonImplementationsRepo[UserEntity, UserDocument]):
    def __init__(self, uow):
        super().__init__(
            entity_cls=UserEntity,
            document_cls=UserDocument,
            not_found_exception=UserNotFoundException,
            fields_resolvers=None,
            fields_serializers=None,
            uow=uow
        )
```

---

### Conversión entre modelos/documentos y entidades

Ambas implementaciones usan la función utilitaria `to_entity_from_model_or_document` para convertir instancias ORM/ODM en entidades de dominio, respetando la estructura de campos y aplicando resolvers para atributos complejos.

---

## 4. Inicialización y Descubrimiento de Documentos Beanie

Para inicializar y registrar todos los documentos Beanie automáticamente:

```python
from hexcore.infrastructure.repositories.orms.beanie.utils import init_beanie_documents

await init_beanie_documents()
```

Esto configura la conexión con MongoDB y registra todos los modelos Beanie implementados en el proyecto.

---

## 5. Darwin: referencia estructural

Darwin, el módulo de identidad nativo, sigue la misma separación hexagonal del resto del
framework: dominio puro sin dependencias, aplicación con puertos, e infraestructura con
adaptadores intercambiables.

### Puertos del dominio

Los puertos viven en `hexcore.darwin.domain.ports` y definen los contratos que los backends
implementan:

- `AbstractUserRepository` — CRUD de usuarios, búsqueda por mail, conteo de generación.
- `AbstractSessionRepository` — CRUD de sesiones, búsqueda por token, reuso, reaper.
- `AbstractAccountRepository` — cuentas de proveedores externos (OAuth, passkeys).
- `AbstractVerificationRepository` — tokens de un solo uso (verificación de mail, magic link,
  desafíos de 2FA).
- `AbstractRevocationList` — denylist de `sid` en caché para revocación inmediata.
- `AbstractAuditSink` — registro de auditoría, opcional.
- `AbstractClock` — reloj inyectable (producción: `SystemClock`; tests: `FixedClock`).
- `AbstractPasswordHasher` — hashing de contraseñas (producción: `Argon2PasswordHasher`).

Cada backend (`sqlalchemy`, `beanie`) expone repositorios con los mismos nombres
(`UserRepository`, `SessionRepository`, `AccountRepository`, `VerificationRepository`), así
que el núcleo y los servicios nunca nombran un backend concreto. Las implementaciones viven
en `hexcore.darwin.infrastructure.orms.{sqlalchemy,beanie}.repositories`.

### Resolución del backend

`resolve_storage_backend(preferido)` en `hexcore.darwin.infrastructure.orms.selection`
decide el backend. Si `IdentityConfig.storage` es `None`, detecta cuál extra está instalado.
Si están los dos, **falla**: es ambiguo y adivinar sería peor. Si no hay ninguno, el error
nombra los dos extras que se pueden instalar. Los cuatro caminos de error traen la remediación.

Los plugins resuelven su backend preguntándole **al contenedor**, no detectando por su cuenta.
Si cada uno detectara independientemente, un despliegue con los dos extras podría terminar con
el núcleo en un backend y un plugin en el otro — y el síntoma es que el login funciona y el
segundo factor no encuentra nada. `plugin_storage_backend()` y `plugin_repositories(plugin)`
en `hexcore.darwin.plugins.storage` implementan esta resolución.

### Contrato de nombre neutro de repositorios

Cada plugin con almacenamiento propio (`two_factor`, `oauth`, `passkey`, `organization`)
expone sus repositorios en:

```
hexcore.darwin.plugins.{plugin}.orms.{backend}.repository
```

El módulo se importa dinámicamente por `plugin_repositories(plugin)`, que resuelve el backend
desde el contenedor. Si el plugin no implementa ese backend, el error dice cuáles implementa
y sugiere pasar un repositorio propio.

Los esquemas SQL y Beanie de los plugins se exponen como `PLUGIN_MODELS` (para Alembic) y
`PLUGIN_DOCUMENTS` (para `init_beanie`) respectivamente. `installed_plugins()` descubre los
plugins presentes leyendo el sistema de archivos — no una lista declarada — para no
reintroducir el acoplamiento que la separación en extras sacó.

### Inicialización de documentos Beanie de identidad

`init_beanie` no acumula: la segunda llamada reemplaza el registro de la primera. Por eso los
documentos de identidad y los de los plugins tienen que entrar en **la misma llamada** que los
documentos de la aplicación. `init_identity_documents` es el helper que los junta:

```python
from hexcore.darwin.infrastructure.orms.beanie.schema import init_identity_documents

# Sin plugins — sólo los seis documentos del núcleo:
await init_identity_documents()

# Con plugins:
await init_identity_documents(plugins=["two_factor", "passkey"])
```

Si la aplicación tiene documentos propios, usá `identity_documents()` para obtener la lista
y combinala con los tuyos en una sola llamada a `init_beanie`:

```python
from beanie import init_beanie
from hexcore.darwin.infrastructure.orms.beanie.schema import identity_documents

await init_beanie(
    database=db,
    document_models=[*identity_documents(plugins=["two_factor"]), MiDocumento, OtroDocumento],
)
```

O declarativamente, con `hx.BeanieStep(documents=[...])` en el lifespan, pasándole la lista
completa.

### Esquema SQL y Alembic

`ensure_identity_schema_loaded(plugins=[...])` registra los modelos de identidad en el
`MetaData` de SQLAlchemy para que `alembic revision --autogenerate` los vea. Hay que llamarlo
desde el `env.py` de Alembic con la lista de plugins activos. Si un plugin falta, Alembic
genera `op.drop_table` sobre sus tablas. `IdentityStep` loguea al arrancar la lista exacta
que hay que copiar.

Los mixins (`UserMixin`, `SessionMixin`, `AccountMixin`, `VerificationMixin`, `AuditLogMixin`,
`JwksMixin`) permiten que la aplicación componga su propio modelo de usuario extendiendo los
campos que necesite:

```python
from hexcore.darwin import UserMixin, DEFAULT_USER_TABLE
from hexcore.sql import Base

class UserModel(UserMixin, Base):
    __tablename__ = DEFAULT_USER_TABLE

    # campos propios de tu app
    display_name: Mapped[str | None]
```

`validate_user_model(model_cls)` verifica en runtime que el modelo cumpla el contrato mínimo
(columnas requeridas, tipos, constraints). `IdentityStep` lo corre al arrancar.

---

## 6. Referencias

- [CONTRIBUTING.md](./CONTRIBUTING.md): Pautas de colaboración.
- [README.md](./README.md): Introducción arquitectónica.
- [CHANGELOG.md](./CHANGELOG.md): Historial de cambios.
- [docs/ARCHITECTURE_DARWIN.md](./docs/ARCHITECTURE_DARWIN.md): Arquitectura del módulo de identidad.
- [docs/ARCHITECTURE_TYPING.md](./docs/ARCHITECTURE_TYPING.md): Sistema de tipos y stubs.

---
