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
   la generación anterior se rechazan sin enumerarlos.

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
    storage=None,                    # "sqlalchemy" | "beanie" | None (detecta)
    trusted_origins=(),
    worker_context_ttl=timedelta(hours=24),
    require_verified_email=True,
    max_verification_attempts=5,
)
```

En producción **falla si no hay clave de firma**, y eso es deliberado.

`configure_identity(config, **componentes)` acepta cualquier puerto a inyectar: `users=`,
`clock=`, `key_store=`, `plugins=`, … Es lo que usan los tests y lo que permite persistir las
claves en producción.

---

## Sin extras

`import hexcore.darwin` **no arrastra** joserfc, argon2 ni sqlalchemy: la fachada resuelve
perezosamente y sólo importa el submódulo del símbolo que pedís. Hay tests que lo verifican
bloqueando los paquetes en `sys.meta_path`.

---

## Ver también

- [`docs/ARCHITECTURE_TYPING.md`](../ARCHITECTURE_TYPING.md) — el sistema de tipos del framework
- [README del proyecto](../../README.md) — el resto de HexCore
