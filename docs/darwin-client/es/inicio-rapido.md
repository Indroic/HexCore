# Inicio rápido

Un sign-in completo, en una pantalla.

```ts
import { createDarwinClient, BearerTransport, memoryStorage } from "@hexcore-js/darwin-client";

const client = createDarwinClient({
  baseUrl: "https://api.ejemplo.com",
  transport: new BearerTransport({ storage: memoryStorage() }),
});

const resultado = await client.signIn("ana@ejemplo.com", "supersecreta");

if (resultado.status === "two-factor-required") {
  // Ver la guía de plugins: `client.twoFactor.complete(resultado.challenge, codigo)`
} else {
  console.log(resultado.session); // { session_id, access_token, expires_in, ... }
}

client.session.getSnapshot();
// { status: "authenticated", me: { actor_id, subject_id, impersonating } }
```

`baseUrl` es la raíz del despliegue de Darwin **sin** `/auth` — eso lo agrega el cliente.

## `signIn()` devuelve un resultado, no tira

Que el segundo factor esté activado no es un error de programación ni una falla: es el camino
feliz de una cuenta bien configurada. Por eso `signIn()` devuelve una unión discriminada en vez
de levantar una excepción:

```ts
type SignInResult =
  | { status: "signed-in"; session: SessionResponse }
  | { status: "two-factor-required"; challenge: string };
```

Modelar el segundo caso como excepción obligaría a que toda app envuelva su login en un
`try/catch` y distinga a mano entre "credenciales malas" (que sí es un error) y "falta el
segundo paso" (que es progreso). Las credenciales malas siguen tirando un
[`DarwinError`](./errores.md); un segundo factor pendiente no.

El `challenge` es lo que canjea
[`twoFactor().complete()`](./plugins.md#twofactor) para terminar el login.

## Qué ya tenés, sin configurar nada

### Refresh proactivo y reactivo, single-flight

El access token de Darwin vive dos minutos. El cliente lo refresca **antes** de que venza, y
también al primer 401 refrescable — y lo hace bajo un lock single-flight: con N requests en
paralelo se dispara un solo refresh, así que el servidor rota el token una vez y no N veces.

Esto importa porque el refresh de Darwin es *rotativo con detección de reuso*: un segundo
refresh que lleva el token que se acaba de rotar se ve exactamente igual que un token robado
siendo reproducido, y el servidor mata la sesión. Un cliente ingenuo que refresca por request
desloguea al usuario bajo carga. Es la falla que este lock existe para evitar.

### Un store de sesión

`client.session` es un store con el mismo contrato que el `useSyncExternalStore` de React, así
que React no necesita ningún adaptador y Svelte necesita uno de tres líneas. Ver
[Sesión y store](./sesion-y-store.md).

```ts
client.session.getSnapshot();
// { status: "loading" }
// | { status: "unauthenticated", reason?: "signed-out" | "refresh-failed" | "revoked" }
// | { status: "authenticated", me: { ... } }
```

`reason` es lo que te deja decir *"tu sesión se cerró por seguridad"* en vez de desloguear en
silencio: `"revoked"` significa que el servidor detectó reuso de token.

### Errores tipados

Un único `DarwinError` discriminado por `code`, generado desde el contrato Python. Ver
[Errores](./errores.md).

## El resto de la superficie del núcleo

```ts
await client.signOut();          // cierra la sesión del lado del servidor y limpia el transporte
await client.refresh();          // fuerza una rotación; rara vez hace falta llamarlo a mano
const me = await client.me();    // GET /auth/me

// Cualquier ruta de Darwin, autenticada, con el refresh y el mapeo de errores aplicados:
const data = await client.$fetch<MiTipo>("/auth/alguna/ruta", { method: "POST", body });
```

`$fetch` es la misma primitiva sobre la que están construidos los plugins. Es lo que hay que
usar cuando el servidor expone algo que este cliente todavía no envuelve — seguís teniendo el
manejo de tokens y los errores tipados, y no te tienta armar un segundo `fetch` a mano que se
saltea los dos.

## Elegir un transporte

El ejemplo de arriba usa `BearerTransport` con storage en memoria, que es el default correcto
para una app nativa o para SSR. Una SPA servida por el mismo backend normalmente quiere
`CookieTransport`, así el refresh token es `HttpOnly` y ningún JavaScript — incluido este
paquete — puede leerlo.

Esa elección tiene consecuencias de seguridad reales en las dos direcciones. Tiene su propia
guía: **[Transportes](./transportes.md)**.

---

Sigue: **[Transportes](./transportes.md)** · **[Sesión y store](./sesion-y-store.md)** ·
**[Plugins](./plugins.md)**
