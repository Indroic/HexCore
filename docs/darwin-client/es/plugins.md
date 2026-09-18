# Plugins

El registro es **explícito** — nunca por descubrimiento. La lista que le pasás a
`createDarwinClient` es la lista completa; un `requires` mal apuntado o un ciclo entre plugins
falla en el arranque de la app, no en producción la primera vez que alguien lo usa.

```ts
import {
  createDarwinClient,
  twoFactor,
  magicLink,
  oauth,
  passkey,
  impersonate,
  organization,
  defineAccessControl,
  rbac,
  drbac,
} from "@hexcore-js/darwin-client";

const ac = defineAccessControl({
  resources: { invoice: ["read", "create", "approve"] },
  roles: ["viewer", "accountant", "admin"],
});

const client = createDarwinClient({
  baseUrl,
  transport,
  plugins: [
    twoFactor(), magicLink(), oauth(), passkey(), impersonate(), organization(),
    rbac({ ac }), drbac({ ac }),
  ],
});
```

Cada plugin cuelga de su propia clave (`client.twoFactor`, `client.oauth`, …), con inferencia de
tipos completa — agregar o sacar un plugin de la lista cambia el tipo de `client` en el editor,
no sólo en runtime. Pedir `client.oauth` sin haber registrado `oauth()` es un error de
compilación, no un `undefined` a las 3 de la mañana.

⚠️ **Cada plugin sigue necesitando su contraparte del lado del servidor.** Registrar `passkey()`
en el cliente no hace nada si el despliegue de Darwin no habilitó el plugin de passkey; las
llamadas devuelven 404. Ver
[los plugins incluidos de Darwin](../../hexcore/es/darwin/plugins-incluidos.md) para el lado del
servidor, y [escribir un plugin](../../hexcore/es/darwin/plugins-propios.md) si estás agregando
uno propio.

---

## `twoFactor()`

TOTP. `complete()` canjea el `challenge` que trae un `signIn()` con
`status: "two-factor-required"` y termina el login.

```ts
const inscripcion = await client.twoFactor.enroll(); // { secret, uri, confirmed: false }
// ...el usuario escanea `uri` y confirma con un código...
await client.twoFactor.confirm(codigo);

const resultado = await client.signIn(identifier, password);
if (resultado.status === "two-factor-required") {
  await client.twoFactor.complete(resultado.challenge, codigo);
}
```

| Método | Devuelve |
| :-- | :-- |
| `status()` | `{ enrolled, confirmed }` |
| `enroll()` | `{ secret, uri, confirmed }` — `uri` es el URI `otpauth://` para el QR |
| `confirm(code)` | `{ confirmed }` |
| `disable(code)` | `{ disabled }` — exige un código válido, así una sesión secuestrada no puede apagar el 2FA |
| `complete(challenge, code)` | Un `SignInResult` |

⚠️ **`enroll()` no alcanza.** Un factor inscripto pero nunca confirmado no protege nada;
`confirmed: false` está ahí para que la UI le diga al usuario que la configuración quedó a
medias, en vez de mostrar un tilde verde.

## `magicLink()`

Login sin contraseña. `request()` responde igual exista o no la cuenta — no lo uses para inferir
si un mail tiene cuenta.

```ts
await client.magicLink.request(email);
// ...el usuario hace click en el link del mail (trae `email` y `token`)...
await client.magicLink.consume(email, token);
```

`request()` devuelve `{ sent, token? }`. ⚠️ **`token` sólo viene poblado en un despliegue que no
manda mails de verdad** — existe para que un entorno local pueda completar el flujo sin un
servidor SMTP. En producción el campo vuelve vacío, y una app que dependa de él funciona en
desarrollo y se rompe al desplegar.

## `oauth()`

Login o vinculación con un proveedor externo. El callback devuelve JSON, no un redirect — el
handler de la ruta de retorno de tu app lo canjea con `handleCallback()`.

```ts
const { url } = await client.oauth.start("google", redirectUri);
window.location.assign(url);
// ...el proveedor vuelve a `redirectUri` con `?code=...&state=...`...
const { result, created } = await client.oauth.handleCallback("google", {
  code, state, redirectUri,
});
if (created) {
  // cuenta nueva: mandar a onboarding en vez de al home
}
```

Devolver JSON en vez de redirigir es lo que mantiene el flujo usable fuera de un navegador y deja
que tu propio router sea dueño de la navegación. `created` es la bandera que distingue un primer
login de un usuario que vuelve, cosa que la sesión sola no te dice.

| Método | Para qué |
| :-- | :-- |
| `providers()` | Qué proveedores configuró el despliegue |
| `start(provider, redirectUri)` | `{ url, state }` — mandá al usuario a `url` |
| `handleCallback(provider, params)` | `{ result, created }` |
| `link(provider, redirectUri)` | Igual que `start`, pero se adosa a la cuenta actual |
| `linked()` | Los proveedores ya vinculados |
| `unlink(provider)` | `{ unlinked }` |

⚠️ `unlink()` se niega a dejar la cuenta sin ningún método de acceso. Manejá ese rechazo — es un
[`DarwinError`](./errores.md), no un no-op silencioso.

## `passkey()` y `@hexcore-js/darwin-client/webauthn`

`passkey` es transporte HTTP puro: las opciones de WebAuthn viajan como JSON crudo, sin tocar
`navigator.credentials`. Para el flujo completo en el navegador, el subpath `/webauthn`:

```ts
import { registerPasskey, authenticateWithPasskey } from "@hexcore-js/darwin-client/webauthn";

const resumen = await registerPasskey(client.passkey, "mi laptop");
const resultado = await authenticateWithPasskey(client.passkey, email);
```

`isWebAuthnSupported()` deja chequear soporte antes de mostrar el botón. Fuera de un navegador
(Node, React Native), `registerPasskey`/`authenticateWithPasskey` tiran
`WebAuthnUnavailableError` al invocarlos — importar el subpath nunca rompe un bundle que no lo
usa.

La separación es deliberada: `passkey` solo es testeable y ejecutable en cualquier lado, y la
mitad que sólo puede existir en un navegador es la mitad a la que optás.

| Método de `client.passkey` | Para qué |
| :-- | :-- |
| `registerOptions()` / `register(credential, name?)` | Las dos mitades del registro |
| `authenticateOptions(email?)` / `authenticate(credential)` | Las dos mitades de la autenticación |
| `list()` | Las passkeys de la cuenta, con `name`, `backedUp`, `lastUsedAt` |
| `remove(passkeyId)` | `{ deleted }` |

## `impersonate()`

Un operador actúa temporalmente como otro usuario, con motivo y vencimiento auditados del lado
del servidor.

```ts
await client.impersonate.start(userId, "verificar un bug reportado");
// ...el store de sesión pasa a reflejar al sujeto impersonado...
await client.impersonate.stop();
// no toca el store por su cuenta: con cookie hace falta un signIn() nuevo para volver a
// ser el operador; con bearer basta con los tokens que ya tenías guardados de antes.
```

Esa asimetría no es un descuido. Con `CookieTransport` las cookies originales del operador
quedaron reemplazadas por las de la impersonación, y este paquete no puede leer cookies
`HttpOnly` para devolver el par viejo a su lugar. Con `BearerTransport` tu storage todavía las
tiene. `status()` devuelve `{ active, actorId, subjectId, reason, expiresAt }` para que la UI
pueda mantener un cartel inconfundible mientras dure.

## `organization()`

Multi-tenancy con roles (`owner` > `admin` > `member`) e invitaciones.

```ts
const org = await client.organization.create("Acme");
const emitida = await client.organization.invite(org.id, "nueva@acme.com", "admin");
// `emitida.token` es de un solo uso: en producción armá el link vos y no lo muestres.
await client.organization.acceptInvitation(token);
```

| Método | Para qué |
| :-- | :-- |
| `create(...)` / `get(id)` / `update(...)` / `remove(id)` | La organización en sí |
| `mine()` | Las membresías del actor actual |
| `members(id)` / `setRole(id, userId, role)` / `removeMember(id, userId)` | Membresía |
| `invite(...)` / `pendingInvitations(id)` / `revokeInvitation(...)` / `acceptInvitation(token)` | Invitaciones |

⚠️ **`invite()` devuelve el token para que vos armes el link**, no para que lo muestres.
Renderizarlo mete una credencial de un solo uso en la página, en los logs y en el historial del
navegador de quien ya está logueado — que no es la persona para la que se emitió.

## `rbac()`

Roles y permisos persistidos y asignables. Necesita un esquema —
[`defineAccessControl`](../../hexcore/es/darwin/autorizacion.md) declara los recursos, acciones
y roles una sola vez, y cada método de abajo queda tipado contra eso.

```ts
const ac = defineAccessControl({
  resources: { invoice: ["read", "approve"] },
  roles: ["viewer", "accountant"],
});
const client = createDarwinClient({ baseUrl, transport, plugins: [rbac({ ac })] });

client.rbac.can("invoice", "approve");             // boolean, síncrono, sin await
client.rbac.hasRole("accountant", { scope: "org:42" });
```

| Método | Para qué |
| :-- | :-- |
| `can(resource, action, opts?)` / `hasPermission(permission, opts?)` | Síncrono, contra el resumen ya cargado |
| `hasRole(role, opts?)` | Igual, para membresía de rol |
| `snapshot(opts?)` | El estado completo — para una lista de roles, no sólo un booleano |
| `subscribe(listener)` | Se dispara con cualquier cambio, en cualquier scope cargado |
| `revalidate(opts?)` | Awaitea un fetch nuevo de un scope |
| `notifyAccessDenied(opts?)` | Avisale al store que atrapaste un 403 del servidor para este scope, para que revalide |
| `dehydrate()` / `hydrate(data)` | Llevar el estado entre un render de SSR y el del cliente — nunca en `localStorage` por default |

⚠️ **Todo esto es optimista para la UI.** La autoridad siempre es el `AuthorizationEngine.decide()`
del servidor, corriendo en cada acción real — `can()` sólo evita mostrar u ocultar un botón
mientras esa decisión real todavía no se pidió. Falla **cerrando**: nunca adivina hacia `true`
sin datos, y un error de red conserva el último resumen conocido en vez de vaciarlo.

## `drbac()`

Políticas condicionales y bindings de rol contextuales sobre `rbac`. `requires: ["rbac"]` — los
dos plugins no se importan entre sí, pero `drbac()` necesita `rbac()` registrado al lado.

```ts
const client = createDarwinClient({ baseUrl, transport, plugins: [rbac({ ac }), drbac({ ac })] });

client.drbac.evaluate("invoice", "approve", { attributes: { owner_id } });
// "allow" | "deny" | "unknown" — síncrono, nunca espera nada

await client.drbac.check("invoice", "approve", { resourceId: invoiceId });
// boolean — autoritativo, contra el servidor
```

| Método | Para qué |
| :-- | :-- |
| `evaluate(resource, action, opts?)` | Síncrono y optimista, contra las reglas `client_evaluable` de `GET /me/snapshot` |
| `check(resource, action, opts?)` | `POST /check` autoritativo, batcheado con cualquier otro `check()` del mismo microtask |
| `checkMany(items)` | Igual, para varios ítems en un solo request explícito |
| `snapshot(opts?)` / `subscribe(listener)` / `revalidate(opts?)` | Misma forma que las de `rbac()` |

Dos caminos deliberadamente distintos, la misma separación que los docstrings del lado del
servidor: `evaluate()` nunca toca la red y contesta `"unknown"` —nunca `"allow"`— para lo que no
puede resolver con los datos ya cargados; `check()`/`checkMany()` son el
`AuthorizationEngine.decide()` de verdad, y son lo que tiene que proteger una mutación real.

⚠️ **`evaluate()` sólo puede ver reglas `client_evaluable`.** Una regla cuya condición use un
`Predicate` nunca se manda al cliente —el servidor la fuerza afuera, porque el navegador no
tiene forma de correr el mismo Python—, así que `evaluate()` contestando `"unknown"` para algo
que el servidor sí resolvería es lo esperado, no un bug. La acción real la protegés siempre con
`check()`.

---

Sigue: **[Errores](./errores.md)**.
