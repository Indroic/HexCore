# Darwin — el módulo de identidad

Registro, verificación de mail, login, sesiones con refresh rotativo, revocación, impersonación
auditada, y un sistema de plugins que agrega segundo factor, OAuth, magic links, passkeys y
organizaciones **sin que el núcleo los conozca**.

Port de la arquitectura, el esquema y el sistema de plugins de
[Better Auth](https://github.com/better-auth/better-auth) a Python + CQRS.

```sh
pip install 'hexcore[darwin-sqlalchemy]'
```

---

## Arrancar

```python
from hexcore.darwin import (
    IdentityConfig,
    build_identity_router,
    configure_identity,
    identity_startup_steps,
)
from hexcore.fastapi import AppFeatures, SqlEngineStep, build_lifespan, create_app

configure_identity(IdentityConfig())          # una vez, al arrancar

app = create_app(
    features=AppFeatures(auth_context=True, csrf=True),
    lifespan=build_lifespan(SqlEngineStep(), *identity_startup_steps()),
    routers=[build_identity_router()],
)
```

Eso monta ocho rutas bajo `/auth`:

`POST /sign-up` · `POST /verify-email` · `POST /sign-in` · `POST /refresh` · `POST /sign-out` ·
`POST /sign-out-everywhere` · `GET /me` · `GET /sessions`

> ⚠️ **`POST /sign-up` es un oráculo de enumeración si lo dejás público tal cual**: responde 409
> cuando el mail ya existe. Sirve el caso administrativo; el público conviene escribirlo en tu
> app, donde la respuesta es siempre la misma y la diferencia va en el mail que se manda.

---

## Índice

| Documento | Qué cubre |
| :-- | :-- |
| [Almacenamiento](./almacenamiento.md) | Backends, esquema, Alembic, `init_beanie`, modelo de usuario propio |
| [Plugins incluidos](./plugins-incluidos.md) | Los seis, con sus rutas y advertencias |
| [Escribir un plugin propio](./plugins-propios.md) | Los puntos de extensión, hooks, y las trampas |

← Volver al [índice de la documentación](../).

---

## Las decisiones que conviene conocer

Lo que sigue no es trivia: son las cuatro cosas que cambian cómo integrás el módulo.

### Actor vs Subject

La sesión persiste `actor_user_id` **y** `subject_user_id`, no un solo `user_id`. `AuthContext`
expone `actor` (quién ejecuta) y `subject` (a quién afecta).

Es lo que hace auditable la impersonación: un contexto impersonado sin los dos principales **no
se puede construir**, porque la validación del modelo lo rechaza. Fuera de una impersonación,
actor y subject son el mismo usuario.

Si escribís lógica que pregunta "quién es el usuario", elegí a conciencia cuál de los dos
querés. Casi siempre es `subject` para permisos sobre datos y `actor` para auditoría.

### Revocación en tres capas, cero DB en el hot path

El access token es un JWT con `exp` corto. **No se toca la base para validarlo** — sólo firma,
`exp`, audience y transporte.

1. `exp` corto: si se lo roban, se lo roban por poco.
2. Denylist de `sid` en `ICache`: `SignOut` bloquea la sesión y un token vigente se rechaza sin
   esperar a que expire.
3. Contador de generación por usuario: `SignOutEverywhere` lo incrementa y todos los tokens de
   la generación anterior se rechazan sin enumerarlos. Lo verifica `GenerationGuard`, que
   cachea el contador 60 s para no consultar la base en cada petición; `revoke_all_for`
   descarta esa entrada en el mismo flujo, así que el corte es inmediato y no "dentro de un
   minuto".

El refresh token **sí** toca la base: rota la sesión atómicamente y detecta reuso. Un refresh
robado revoca la familia entera en el primer intento.

La denylist falla **cerrando** (`on_cache_error="deny"`), al revés que el `rate_limit` del
framework y a propósito: un cache caído no puede convertirse en "todo el mundo pasa".

### El algoritmo va pineado, nunca el `alg` del token

`joserfc` sobre `pyjwt` justamente porque su API **obliga** a pasar la lista de algoritmos
permitidos: el default seguro es estructural, no documental. La confusión de algoritmo es la
familia de bugs de JWT más repetida, y esta elección la hace imposible por construcción.

### El transporte va atado al token

Cookie y Bearer emiten tokens con `aud`/`tt` distinto, así que **una cookie no se puede
replayear como Bearer** esquivando CSRF y `SameSite`.

Un solo endpoint por operación sirve los dos transportes: el cliente web recibe `Set-Cookie` y
ningún token en el cuerpo; el nativo recibe los tokens en el cuerpo y ningún `Set-Cookie`.
Duplicar las rutas duplicaría también los chequeos de seguridad, y la copia que se olvida de uno
es la que se explota.

Cookies: `__Host-` + `HttpOnly` + `Secure` + `SameSite=Lax`, más chequeo anti-CSRF explícito.

---

## El actor cruza la cola

Cuando encolás un comando durante un request autenticado, el actor viaja en un **sobre firmado**
atado al mensaje (`cid`, `mt`). Sin ese binding, un grant capturado de un "borrar cuenta" se
re-adjunta a un "transferir fondos".

El worker **re-valida la fila de `session`** en vez de confiar en el `exp`: un token válido al
encolar puede estar revocado para cuando el worker lo procesa. `IdentityConfig.worker_context_ttl`
(24 h por default) acota la ventana.

---

## Configuración

```python
IdentityConfig(
    secret_key=...,                  # SecretStr | None
    tokens=TokenConfig(...),
    cookies=CookieConfig(...),
    passwords=PasswordPolicy(...),
    user_model=None,                 # tu clase, si componés UserMixin
    session_model=None,              # idem para las otras cinco tablas
    storage=None,                    # "sqlalchemy" | "beanie" | None (detecta)
    trusted_origins=(),
    worker_context_ttl=timedelta(hours=24),
    require_verified_email=True,
    max_verification_attempts=5,
)
```

| Campo | Default | Qué hace |
| :-- | :-- | :-- |
| `secret_key` | `None` | La clave de firma. **Sin default**: se lee de `HEXCORE_DARWIN_SECRET_KEY` |
| `user_model` | `None` | Tu clase, si componés `UserMixin`. Se valida al configurar |
| `session_model`, `account_model`, `verification_model`, `audit_model`, `jwks_model` | `None` | Idem para las otras cinco tablas. **Casi nunca hacen falta**: si declarás tus modelos, Darwin los encuentra solo (ver abajo) |
| `storage` | `None` | `"sqlalchemy"`, `"beanie"`, o detección |
| `trusted_origins` | `()` | Orígenes válidos para el chequeo anti-CSRF |
| `worker_context_ttl` | 24 h | Ventana en que el sobre del actor sigue siendo canjeable |
| `require_verified_email` | `True` | Si el sign-in exige mail verificado. **No aplica a una cuenta sin mail**: exigirlo ahí sería un 403 permanente |
| `usernames` | `None` | La `UsernamePolicy`, o `None` si esta app no usa nombres de usuario |
| `require_email` | `True` | Si el alta exige una dirección de correo |
| `sign_in_identifiers` | `("email",)` | Con qué se puede entrar: `"email"`, `"username"`, o los dos |
| `max_verification_attempts` | `5` | Intentos por token de verificación antes de invalidarlo |

`TokenConfig`:

| Campo | Default | Nota |
| :-- | :-- | :-- |
| `issuer` | `"hexcore"` | El `iss` del JWT |
| `access_ttl` | **2 minutos** | Corto a propósito: es lo que acota el robo de un access token |
| `refresh_ttl` | 30 días | El refresh rota en cada uso |
| `session_ttl` | 90 días | Techo absoluto de la sesión, rote lo que rote |
| `algorithm` | `"Ed25519"` | Fijado por allowlist, nunca por el `alg` del token |
| `leeway` | 30 s | Tolerancia de reloj entre nodos |

`CookieConfig`:

| Campo | Default | Nota |
| :-- | :-- | :-- |
| `access_name` / `refresh_name` / `csrf_name` | `session` / `refresh` / `csrf` | Reciben el prefijo `__Host-` |
| `secure` | `True` | — |
| `http_only` | `True` | El JS no ve el token |
| `same_site` | `"lax"` | — |
| `path` | `"/"` | `__Host-` exige exactamente esto |

`PasswordPolicy`:

| Campo | Default | Nota |
| :-- | :-- | :-- |
| `min_length` | `12` | Longitud sobre composición: es lo que recomienda el NIST |
| `max_length` | `1024` | Un techo existe porque hashear 10 MB es un DoS gratis |
| `denylist` | `frozenset()` | Contraseñas prohibidas, comparadas normalizadas |
| `acknowledge_weak_minimum` | `False` | Permite bajar `min_length` de 8. Es un flag aparte para que cueste escribirlo y quede en el diff |

`UsernamePolicy`:

| Campo | Default | Nota |
| :-- | :-- | :-- |
| `min_length` / `max_length` | `3` / `32` | — |
| `pattern` | `^[a-z0-9][a-z0-9_.-]*$` | Sobre el valor ya normalizado. **No admite `@`**, para que un username no pueda tener forma de mail |
| `case_sensitive` | `False` | Con `False`, `Ana` y `ana` son el mismo usuario |
| `reserved` | `frozenset()` | Nombres que nadie puede tomar, comparados normalizados |

---

## Entrar con nombre de usuario

```python
IdentityConfig(
    usernames=UsernamePolicy(min_length=4, reserved=frozenset({"admin", "api"})),
    require_email=False,
    sign_in_identifiers=("email", "username"),
)
```

Con eso, `POST /auth/sign-up` acepta `{"username": "indroic", "password": "..."}` sin mail, y
`POST /auth/sign-in` acepta cualquiera de los dos en el campo `identifier`.

**Tener usernames y aceptarlos para entrar son dos decisiones distintas**, y por eso son dos
campos: un foro puede mostrar `@pepe` en cada mensaje y aun así exigir el mail para el login.
Dejá `sign_in_identifiers=("email",)` y el username queda como identificador público nomás.

⚠️ **Una cuenta sin mail no puede recuperar la contraseña ni verificar nada**: los dos flujos se
apoyan en mandar un código a alguna parte. Si usás `require_email=False`, necesitás otro camino
de recuperación.

El cuerpo de `/auth/sign-in` acepta el identificador con **tres nombres** —`identifier`, `email`
y `username`— para que un front de 9.x que manda `{"email": ...}` siga funcionando. Mandá uno
solo: si vienen varios, se rechaza.

En producción **falla si no hay clave de firma**, y eso es deliberado. Para generar una:
`hexcore identity generate-secret`.

`configure_identity(config, **componentes)` acepta cualquier puerto a inyectar: `users=`,
`clock=`, `key_store=`, `principals=`, `plugins=`, … Es lo que usan los tests y lo que permite
enchufar los permisos de tu app.

---

## Tus modelos concretos

Si declarás tus propios modelos sobre las tablas de Darwin —el camino recomendado, y el único
que permite agregarles columnas— **no hace falta configurar nada**:

```python
from hexcore.darwin import UserMixin
from hexcore.sql import Base

class UserModel(UserMixin, Base):
    __tablename__ = "darwin_user"
    plan: Mapped[str] = mapped_column(String(32), default="free")
```

Darwin resuelve la clase concreta de cada tabla en tres pasos: lo que declaraste en
`IdentityConfig`, después la clase mapeada que compone el mixin correspondiente, y recién si no
hay ninguna, la de su `models.py`.

⚠️ **Ese orden es el que evita un arranque roto.** Importar `models.py` para obtener *una* clase
ejecuta el módulo entero, que declara **las seis** sobre `Base`. Con tu `UserModel` ya declarado
sobre `darwin_user`, eso son dos clases peleando por la misma tabla y SQLAlchemy corta con
`InvalidRequestError: Table 'darwin_user' is already defined for this MetaData instance` — al
primer uso de cualquier repositorio, así que el stack trace apunta a una consulta de sesiones y
no al import que la causó. Los `*_model` de `IdentityConfig` existen sólo para desempatar cuando
mapeás dos clases sobre el mismo mixin en tablas distintas.

---

## Roles y scopes

`Principal` lleva `roles` y `scopes`, y de dónde salen lo decide tu app con un puerto:

```python
from hexcore.darwin import (
    AbstractPrincipalResolver,
    ResolvedPrincipal,
    configure_identity,
)

class PrincipalesDeLaApp(AbstractPrincipalResolver):
    async def resolve(self, user):
        async with uow_scope() as uow:
            fila = await uow.membresias.get_by_user(user.id)
        return ResolvedPrincipal(
            roles=frozenset(fila.roles),
            scopes=frozenset(fila.permisos),
            status=fila.status,     # el estado de TU máquina de estados
        )

configure_identity(IdentityConfig(), principals=PrincipalesDeLaApp())
```

El default es `NullPrincipalResolver`, que devuelve dos conjuntos vacíos: una app que no declara
permisos no empieza a recibirlos porque actualizó.

**Se consulta en el sign-in y en cada rotación de refresh**, o sea cada `access_ttl` (2 minutos)
mientras la sesión esté viva. Re-resolver es lo que hace que quitarle un rol a alguien tenga
efecto sin esperar a que cierre sesión — el corte llega en la rotación siguiente. Si necesitás
que sea inmediato, la herramienta es `revoke_all_for`, que sube la generación y corta todos los
tokens del usuario de una.

Los tres viajan en el token, no en la base: `authenticate` es el camino caliente y no consulta.

### El estado de la cuenta

`status` es un `str` libre y **Darwin no lo interpreta**: no sabe si `pending` puede entrar ni
si `banned` puede leer. Sólo lo transporta hasta `auth.actor.status`, donde tu app lo lee sin
volver a consultar la base.

Existe porque Darwin sólo conoce `is_active` y `locked_until`, y sólo los mira **al rotar**. Una
app con su propia máquina de estados tenía dos opciones malas: consultar la base en cada request
para saber si el usuario sigue habilitado, o espejar su estado dentro de `is_active` /
`locked_until` en cada transición y mantener los dos sincronizados para siempre. Con `status` no
hace falta ninguna de las dos.

Para un estado que directamente no puede seguir, **lanzá desde el resolver**. Tiene que ser una
`IdentityError` —cualquier otra escapa al mapeo del módulo y sale como 500— y tené presente que
en una rotación eso corre *después* de consumir la fila de sesión, así que el usuario queda
deslogueado: que suele ser lo que se quiere para una cuenta suspendida.

⚠️ Cambiar el estado tarda hasta un `access_ttl` en tener efecto, igual que los roles. Si un
`banned` tiene que echar a alguien **ya**, el flujo que lo cambia llama además a
`SessionService.revoke_all_for`.

---

## Sin extras

`import hexcore.darwin` **no arrastra** joserfc, argon2 ni sqlalchemy: la fachada resuelve
perezosamente y sólo importa el submódulo del símbolo que pedís. Hay tests que lo verifican
bloqueando los paquetes en `sys.meta_path`.

---

## Ver también

- [`docs/ARCHITECTURE_TYPING.md`](../../../ARCHITECTURE_TYPING.md) — el sistema de tipos del framework
- [README del proyecto](../../../../packages/hexcore/README.md) — el resto de HexCore
