# @hexcore/darwin-client

Cliente TypeScript agnóstico de framework de UI, de runtime y de backend para **Darwin**, el
módulo de identidad de [HexCore](../hexcore/). Cero dependencias de runtime.

## Por qué existe

Darwin expone su contrato HTTP sólo desde el código Python: doble transporte cookie/Bearer,
CSRF double-submit derivado por HMAC, y un `access_ttl` de dos minutos que exige refresh
rotativo con detección de reuso. Sin un cliente versionado junto al servidor, cualquier
frontend reimplementa esas cuatro cosas a mano — y equivocarse en cualquiera de ellas es un
bug de seguridad, no de comodidad.

## Instalación

```bash
npm install @hexcore/darwin-client
```

## Inicio rápido

```ts
import { createDarwinClient, BearerTransport, memoryStorage } from "@hexcore/darwin-client";

const client = createDarwinClient({
  baseUrl: "https://api.miapp.com",
  transport: new BearerTransport({ storage: memoryStorage() }),
});

const resultado = await client.signIn("ana@ejemplo.com", "supersecreta");

if (resultado.status === "two-factor-required") {
  // Ver la sección de plugins: `client.twoFactor.complete(resultado.challenge, codigo)`
} else {
  console.log(resultado.session); // { session_id, access_token, expires_in, ... }
}

client.session.getSnapshot();
// { status: "authenticated", me: { actor_id, subject_id, impersonating } }
```

`createDarwinClient()` ya trae, sin necesidad de nada más:

- **Refresh proactivo y reactivo, single-flight.** Antes de que el access venza, o al primer
  401 refrescable, un solo refresh se dispara aunque haya N requests en paralelo — el
  servidor rota el token una vez, no N veces.
- **Store de sesión** (`client.session`) con el mismo contrato que
  [`useSyncExternalStore`](https://react.dev/reference/react/useSyncExternalStore) de React.
- **Errores tipados** (`DarwinError`, con `code` generado desde el contrato Python — ver
  [Errores](#errores)).

## Transportes

| | `BearerTransport` | `CookieTransport` |
|---|---|---|
| Dónde vive el token | El `TokenStorage` que le pases (memoria por defecto) | `HttpOnly`, en el navegador |
| Quién puede leerlo | Tu JS | Nadie del lado del cliente — ni este paquete |
| Uso típico | Apps nativas, SSR, cualquier cosa que no sea un navegador con el backend en el mismo sitio | SPA servida por (o con CORS+credentials hacia) el mismo backend Darwin |

```ts
import { BearerTransport, memoryStorage, localStorageAdapter } from "@hexcore/darwin-client";

// Por defecto: sólo en memoria del proceso — se pierde al recargar la página.
new BearerTransport({ storage: memoryStorage() });

// Persistido entre recargas. Léase la advertencia de `memoryStorage()`: un XSS que
// exfiltra `localStorage` se lleva un refresh que vale semanas, no un access de dos minutos.
new BearerTransport({ storage: localStorageAdapter() });
```

```ts
import { CookieTransport } from "@hexcore/darwin-client";

// En un navegador de verdad: lee la cookie CSRF de `document.cookie` sola.
const client = createDarwinClient({
  baseUrl: "https://api.miapp.com",
  transport: new CookieTransport(),
});
```

`CookieTransport` exige `credentials: "include"` en cada request — el cliente ya se lo agrega
por su cuenta, no hay que configurarlo.

## Sesión y store

`client.session` expone `subscribe`, `getSnapshot` y `getServerSnapshot` — el contrato exacto
de `useSyncExternalStore`, así que React no necesita ningún adaptador:

```tsx
import { useSyncExternalStore } from "react";

function useSession() {
  return useSyncExternalStore(
    client.session.subscribe,
    client.session.getSnapshot,
    client.session.getServerSnapshot,
  );
}
```

Svelte espera algo distinto: `subscribe(run)` tiene que invocar `run` **inmediatamente** con
el valor actual, no sólo tras el próximo cambio. Para eso está el subpath `/store`:

```ts
import { toSvelteStore } from "@hexcore/darwin-client/store";

export const session = toSvelteStore(client.session);
// en un componente: `$session.status`
```

### SSR

```ts
const client = createDarwinClient({
  baseUrl,
  transport,
  hydrateOnCreate: false, // el estado inicial es "unauthenticated", no un "loading" eterno
});
```

Sin esto, el servidor nunca resuelve la hidratación (`GET /auth/me`) y el spinner de
`"loading"` queda renderizado en el HTML.

## Plugins

Registro **explícito** — nunca por descubrimiento. La lista que le pasás a
`createDarwinClient` es la lista completa; un `requires` mal apuntado o un ciclo entre
plugins falla en el arranque de la app, no en producción la primera vez que alguien lo usa.

```ts
import {
  createDarwinClient,
  twoFactor,
  magicLink,
  oauth,
  passkey,
  impersonate,
  organization,
} from "@hexcore/darwin-client";

const client = createDarwinClient({
  baseUrl,
  transport,
  plugins: [twoFactor(), magicLink(), oauth(), passkey(), impersonate(), organization()],
});
```

Cada plugin cuelga de su propia clave (`client.twoFactor`, `client.oauth`, ...), con
inferencia de tipos completa — agregar o sacar un plugin de la lista cambia el tipo de
`client` en el editor, no sólo en runtime.

### `twoFactor()`

TOTP. `complete()` canjea el `challenge` que trae un `signIn()` con `status:
"two-factor-required"` y termina el login.

```ts
const inscripcion = await client.twoFactor.enroll(); // { secret, uri, confirmed: false }
// ...el usuario escanea `uri` y confirma con un código...
await client.twoFactor.confirm(codigo);

const resultado = await client.signIn(email, password);
if (resultado.status === "two-factor-required") {
  await client.twoFactor.complete(resultado.challenge, codigo);
}
```

### `magicLink()`

Login sin contraseña. `request()` responde igual exista o no la cuenta — no lo uses para
inferir si un mail tiene cuenta.

```ts
await client.magicLink.request(email);
// ...el usuario hace click en el link del mail (trae `email` y `token`)...
await client.magicLink.consume(email, token);
```

### `oauth()`

Login/vinculación con un proveedor externo. El callback devuelve JSON, no un redirect — el
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

### `passkey()` y `@hexcore/darwin-client/webauthn`

`passkey` es transporte HTTP puro: las opciones de WebAuthn viajan como JSON crudo, sin tocar
`navigator.credentials`. Para el flujo completo en el navegador, el subpath `/webauthn`:

```ts
import { registerPasskey, authenticateWithPasskey } from "@hexcore/darwin-client/webauthn";

const resumen = await registerPasskey(client.passkey, "mi laptop");
const resultado = await authenticateWithPasskey(client.passkey, email);
```

`isWebAuthnSupported()` deja chequear soporte antes de mostrar el botón. Fuera de un
navegador (Node, React Native), `registerPasskey`/`authenticateWithPasskey` tiran
`WebAuthnUnavailableError` al invocarlos — importar el subpath nunca rompe un bundle que no
lo usa.

### `impersonate()`

Un operador actúa temporalmente como otro usuario, con motivo y vencimiento auditados del
lado del servidor.

```ts
await client.impersonate.start(userId, "verificar un bug reportado");
// ...el store de sesión pasa a reflejar al sujeto impersonado...
await client.impersonate.stop();
// no toca el store por su cuenta: con cookie hace falta un signIn() nuevo para volver a
// ser el operador; con bearer basta con los tokens que ya tenías guardados de antes.
```

### `organization()`

Multi-tenancy con roles (`owner` > `admin` > `member`) e invitaciones.

```ts
const org = await client.organization.create("Acme");
const emitida = await client.organization.invite(org.id, "nueva@acme.com", "admin");
// `emitida.token` es de un solo uso: en producción armá el link vos y no lo muestres.
await client.organization.acceptInvitation(token);
```

## Errores

Un único `DarwinError`, discriminado por `code` — no dieciocho subclases que sincronizar a
mano con el backend.

```ts
import { DarwinError, isRefreshable, isSessionDead, isTwoFactorRequired } from "@hexcore/darwin-client";

try {
  await client.signIn(email, password);
} catch (err) {
  if (err instanceof DarwinError) {
    console.log(err.code, err.status, err.detail);
  }
}
```

- `err.code` es un `DarwinCode`: los valores conocidos del contrato Python autocompletan en
  el editor, pero un código que el backend agregue y este cliente todavía no conozca se
  preserva igual — no rechaza en tiempo de compilación.
- `isRefreshable`/`isSessionDead`/`isTwoFactorRequired` son los predicados que ya usa el
  núcleo por dentro; están exportados porque una app también los necesita (por ejemplo, para
  no mostrar el mismo mensaje de error genérico ante un 2FA pendiente).
- `NonJsonResponse` y `NetworkError` son los dos códigos que sólo existen del lado del
  cliente: un proxy/balanceador que no habla el envelope de Darwin, y un `fetch` que rechazó
  (sin conexión, CORS).

## Desarrollo

```bash
npm install
npm -w @hexcore/darwin-client run gen        # regenera src/generated/ desde openapi/
npm -w @hexcore/darwin-client run typecheck
npm -w @hexcore/darwin-client run test
npm -w @hexcore/darwin-client run build
```

`openapi/` se vuelca desde el paquete Python con
`uv run python scripts/darwin_openapi.py --write` (ver `packages/hexcore/scripts/`) y se versiona
en git: el drift entre el contrato y el cliente aparece en el diff del PR.
