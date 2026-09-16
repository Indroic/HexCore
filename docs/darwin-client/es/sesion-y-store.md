# Sesión y store

`client.session` es la única fuente de verdad sobre "quién está logueado". Es un store, no un
snapshot que haya que pollear: se actualiza en el sign-in, en el sign-out, en un refresh exitoso
y ante una sesión que el servidor mató.

## El estado

```ts
type SessionState =
  | { status: "loading" }
  | { status: "unauthenticated"; reason?: "signed-out" | "refresh-failed" | "revoked" }
  | { status: "authenticated"; me: MeResponse };
```

`reason` existe para que la UI pueda distinguir tres cosas distintas, y las tres merecen un
mensaje distinto:

| `reason` | Qué pasó | Qué dice una buena UI |
| :-- | :-- | :-- |
| `"signed-out"` | Un `signOut()` deliberado | Nada; a la pantalla de login |
| `"refresh-failed"` | El refresh falló — la sesión está muerta y marcada para no reintentar | "Tu sesión expiró" |
| `"revoked"` | Se detectó reuso de token del lado del servidor | "Cerramos tu sesión por seguridad" |

Sin `reason`, las tres colapsan en un logout mudo, y la tercera — la única que puede significar
que alguien más tiene el token del usuario — es justo la que no querés que sea muda.

## React

`client.session` expone `subscribe`, `getSnapshot` y `getServerSnapshot` — el contrato exacto de
`useSyncExternalStore`, así que React no necesita ningún adaptador:

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

`getSnapshot` devuelve una referencia estable entre cambios, que es lo que `useSyncExternalStore`
exige para no entrar en un loop infinito de renders. No lo envuelvas en algo que arme un objeto
nuevo en cada llamada — derivar estado va en un `useMemo` sobre el snapshot devuelto, no adentro
del snapshot.

## Svelte

Svelte espera algo distinto: `subscribe(run)` tiene que invocar `run` **inmediatamente** con el
valor actual, no sólo tras el próximo cambio. Para eso está el subpath `/store`:

```ts
import { toSvelteStore } from "@hexcore-js/darwin-client/store";

export const session = toSvelteStore(client.session);
// en un componente: `$session.status`
```

Es un subpath aparte y no parte del núcleo porque el núcleo se mantiene libre de las convenciones
de cualquier framework. El adaptador son unas pocas líneas; la alternativa — que el núcleo
adivine qué framework le está preguntando — no.

## Vue, Solid, Angular, cualquier otro

Cualquier framework que sepa consumir un par `subscribe`/`getSnapshot` puede consumir este store
directo. La forma es deliberadamente la más chica que funciona:

```ts
const desuscribir = client.session.subscribe(() => {
  hacerAlgoCon(client.session.getSnapshot());
});
```

`subscribe` devuelve la función de desuscripción. Llamala al desmontar; un cliente que sobrevive
al componente mantendría el callback vivo si no.

## SSR

```ts
const client = createDarwinClient({
  baseUrl,
  transport,
  hydrateOnCreate: false, // el estado inicial es "unauthenticated", no un "loading" eterno
});
```

Por defecto el cliente hidrata la sesión con un `GET /auth/me` apenas se construye, y el estado
inicial es `"loading"`. En un servidor eso está mal de una forma fácil de no ver: el servidor
renderiza y responde antes de que la hidratación resuelva, así que **el spinner de `"loading"`
queda horneado en el HTML** y el usuario lo ve hasta que arranca el bundle del cliente y lo
reemplaza.

`hydrateOnCreate: false` hace que el estado inicial sea `"unauthenticated"`, que es lo correcto
para renderizar en un request cuya sesión todavía no resolviste.

Si sí querés que el servidor renderice la vista autenticada, resolvela explícitamente — construí
el cliente con un [`memoryCookieJar()`](./transportes.md#cookie-jars) que lleve el header
`Cookie` del request entrante, hacé `await client.me()`, y renderizá a partir de eso.

---

Sigue: **[Plugins](./plugins.md)**.
