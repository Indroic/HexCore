# Los seis plugins incluidos

Cada uno tiene su propio extra. Instalás el que usás:

```sh
pip install 'hexcore[darwin-two-factor]'
```

Cada extra de plugin arrastra `hexcore[darwin]`, así que ese comando trae el núcleo que
necesita. Lo que **no** trae es un backend de almacenamiento: eso lo elegís vos, y está en
[Almacenamiento](./almacenamiento.md).

Cuatro de los seis no suman ninguna dependencia —corren con stdlib más el núcleo— y aun así
tienen extra propio: es el nombre estable donde una dependencia futura entra sin cambiarte el
comando de instalación, y es el único lugar que se lee antes de instalar.

---

## `magic_link` — login por link de un solo uso

```python
from hexcore.darwin.plugins.magic_link import MagicLinkPlugin

MagicLinkPlugin(ttl=timedelta(minutes=15), rate_limit=(3, 900), audit_hook=False)
```

| Ruta | Qué hace |
| :-- | :-- |
| `POST /request` | Emite el link |
| `POST /consume` | Lo canjea por una sesión |

**No aporta tabla**: reusa la `verification` del núcleo, que ya modela exactamente un token de
un solo uso.

> El `rate_limit` default limita por IP. Sin él, `POST /request` es un amplificador de mail
> gratuito contra terceros.

---

## `two_factor` — TOTP con códigos de respaldo

```python
from hexcore.darwin.plugins.two_factor import TwoFactorPlugin

TwoFactorPlugin(issuer="Mi Producto", challenge_ttl=..., rate_limit=(5, 300))
```

| Ruta | Qué hace |
| :-- | :-- |
| `GET ""` | Estado del segundo factor |
| `POST /enroll` | Empieza el alta |
| `POST /confirm` | La confirma |
| `POST /disable` | Lo desactiva |
| `POST /challenge` | Canjea el desafío del segundo paso |

`issuer` es el nombre que muestran las apps autenticadoras: poné el de tu producto.

> **No apagues el `rate_limit`.** Un TOTP es de seis dígitos: sin límite, el desafío se resuelve
> por fuerza bruta.

RFC 6238 sobre `hmac` de la stdlib, sin `pyotp`: son unas treinta líneas —un HMAC, un contador
de a treinta segundos y un truncado— y no justifican una dependencia en el camino de
autenticación.

---

## `oauth` — Authorization Code + PKCE

```python
from hexcore.darwin.plugins.oauth import OAuthPlugin

OAuthPlugin(
    providers=[...],
    allowed_redirect_uris=["https://mi-app.com/callback"],
    link_policy=...,
)
```

| Ruta | Qué hace |
| :-- | :-- |
| `GET /providers` | Los configurados |
| `GET /{provider}/start` | Arranca el flujo |
| `GET /{provider}/callback` | Lo cierra |
| `GET /{provider}/link` | Vincula a una cuenta existente |
| `GET /linked` | Las vinculadas |
| `DELETE /{provider}` | Desvincula |

> **`allowed_redirect_uris` hay que declararlo en producción.** Sin la lista, el `redirect_uri`
> queda abierto y el flujo se convierte en un open redirect.

El `link_policy` default **no vincula por mail**, y es el correcto: vincular por mail coincidente
deja que un proveedor que no verifica el mail se apodere de una cuenta existente.

El cliente HTTP está detrás de un puerto, así que los tests del flujo corren sin el extra.

---

## `impersonate` — "entrar como", auditado

```python
from hexcore.darwin.plugins.impersonate import ImpersonatePlugin

ImpersonatePlugin(policy=ScopeImpersonationPolicy(), rate_limit=...)
```

| Ruta | Qué hace |
| :-- | :-- |
| `POST /{user_id}` | Empieza |
| `POST /stop` | Termina |
| `GET ""` | Estado actual |

No aporta dependencias ni tablas: la impersonación es una sesión con dos principales, y el
núcleo los distingue desde el diseño del esquema —`actor` y `subject` son columnas de
`session`— porque una impersonación no auditable no tenía que poder construirse ni siquiera
antes de que este plugin existiera.

El `rate_limit` existe **aunque la ruta esté autenticada**: si una cuenta de soporte se ve
comprometida, sin límite el atacante enumera usuarios.

---

## `passkey` — WebAuthn

```python
from hexcore.darwin.plugins.passkey import PasskeyPlugin

PasskeyPlugin(
    rp_id="mi-app.com",
    rp_name="Mi App",
    origins=["https://mi-app.com"],
    require_user_verification=True,
)
```

| Ruta | Qué hace |
| :-- | :-- |
| `POST /register/options` | Opciones de alta |
| `POST /register` | Da de alta |
| `POST /authenticate/options` | Opciones de login |
| `POST /authenticate` | Login |
| `GET ""` | Las registradas |
| `DELETE /{passkey_id}` | Borra una |

> **Cambiar el `rp_id` invalida todas las passkeys existentes.** Es el dominio de la Relying
> Party, sin esquema ni puerto, y forma parte de la credencial.

`origins` es obligatorio si no traés tu propio verificador. `require_user_verification=True` es
lo que habilita login sin contraseña, exigiendo PIN o biometría.

Suma `py_webauthn`, y se justifica: WebAuthn no son treinta líneas como el TOTP —hay CBOR,
claves COSE, formatos de attestation y un contador de firmas—. Escribirlo a mano sería
criptografía propia en el camino de autenticación.

---

## `organization` — organizaciones, miembros e invitaciones

```python
from hexcore.darwin.plugins.organization import OrganizationPlugin

OrganizationPlugin(invitation_ttl=timedelta(days=7), max_members=None)
```

| Ruta | Qué hace |
| :-- | :-- |
| `POST ""` / `GET ""` | Crea / lista |
| `GET`·`PATCH`·`DELETE /{organization_id}` | Una organización |
| `GET /{organization_id}/members` | Miembros |
| `PATCH`·`DELETE /{organization_id}/members/{user_id}` | Un miembro |
| `POST`·`GET /{organization_id}/invitations` | Invitaciones |
| `DELETE /{organization_id}/invitations/{invitation_id}` | Revoca una |
| `POST /invitations/accept` | Acepta |

Sin dependencias: lo que necesita no son paquetes sino **operaciones atómicas**, y esas las da el
backend — `EXISTS` correlacionado en el `WHERE` para SQL, miembros embebidos para Mongo. Es lo
que hace que quitar al último owner no pueda ganarse una condición de carrera.

---

## Cablearlos

```python
from hexcore.darwin import IdentityConfig, configure_identity
from hexcore.darwin.plugins.magic_link import MagicLinkPlugin
from hexcore.darwin.plugins.two_factor import TwoFactorPlugin

configure_identity(
    IdentityConfig(),
    plugins=[MagicLinkPlugin(), TwoFactorPlugin(issuer="Mi Producto")],
)
```

Y para montar sus rutas:

```python
plugins = get_identity_container().plugins

app = create_app(
    features=AppFeatures(auth_context=True, csrf=True),
    routers=[build_identity_router(), *plugins.routers()],
)
```

Todos aceptan `include_router=False` si preferís exponer los flujos con tus propias rutas y
usar sólo los comandos.

**Si aportan tablas, acordate del `env.py`**: ver [Almacenamiento](./almacenamiento.md).

---

## Ver también

- [Escribir un plugin propio](./plugins-propios.md)
- [Almacenamiento, esquema y migraciones](./almacenamiento.md)
