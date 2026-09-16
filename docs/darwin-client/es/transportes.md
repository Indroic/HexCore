# Transportes

Un transporte decide **dónde viven los tokens y quién puede leerlos**. Es la única decisión de
este cliente que es una decisión de seguridad y no una preferencia, y es obligatoria — no hay
default.

| | `BearerTransport` | `CookieTransport` |
| :-- | :-- | :-- |
| Dónde vive el token | El `TokenStorage` que le pases (memoria por defecto) | `HttpOnly`, en el navegador |
| Quién puede leerlo | Tu JS | Nadie del lado del cliente — ni este paquete |
| Uso típico | Apps nativas, SSR, cualquier cosa que no sea un navegador con el backend en el mismo sitio | SPA servida por (o con CORS+credentials hacia) el mismo backend Darwin |

## `BearerTransport`

```ts
import { BearerTransport, memoryStorage, localStorageAdapter } from "@hexcore-js/darwin-client";

// Por defecto: sólo en memoria del proceso — se pierde al recargar la página.
new BearerTransport({ storage: memoryStorage() });

// Persistido entre recargas.
new BearerTransport({ storage: localStorageAdapter() });
```

⚠️ **`localStorageAdapter()` no es una mejora gratis sobre `memoryStorage()`.** Un XSS que
exfiltra `localStorage` se lleva un token de *refresh* que vale semanas, no un access de dos
minutos. En un navegador, eso es precisamente para lo que existe `CookieTransport`. Persistir en
`localStorage` es un canje razonable en un entorno donde el XSS no está en el modelo de
amenazas, y uno malo en una web pública.

`memoryStorage()` es el default porque perder la sesión al recargar es una falla visible e
inofensiva, y la alternativa falla de forma invisible y cara.

### React Native y otros storages asíncronos

```ts
import { BearerTransport, fromAsyncStorage } from "@hexcore-js/darwin-client";

new BearerTransport({ storage: fromAsyncStorage(AsyncStorage) });
```

`fromAsyncStorage` adapta cualquier cosa con la forma `getItem`/`setItem`/`removeItem` que
devuelve promesas, que es lo que exponen el `AsyncStorage` de React Native y casi todos los
wrappers de almacenamiento seguro.

## `CookieTransport`

```ts
import { createDarwinClient, CookieTransport } from "@hexcore-js/darwin-client";

// En un navegador de verdad: lee la cookie CSRF de `document.cookie` sola.
const client = createDarwinClient({
  baseUrl: "https://api.ejemplo.com",
  transport: new CookieTransport(),
});
```

El refresh token lo setea el servidor como `HttpOnly`, así que este paquete nunca lo ve. Lo que
el transporte **sí** maneja es la otra mitad del contrato: Darwin usa **CSRF double-submit
derivado por HMAC**, o sea que todo request que muta estado tiene que devolver un valor que el
servidor puso en una cookie legible. `CookieTransport` lo lee y lo devuelve por vos.

`CookieTransport` exige `credentials: "include"` en cada request — el cliente ya se lo agrega
por su cuenta, no hay que configurarlo.

### Cookie jars

En un navegador el transporte lee `document.cookie` sin ninguna configuración. Fuera de uno —
SSR, tests, un runtime con la Cookie Store API — se le pasa un jar explícito:

```ts
import { CookieTransport, memoryCookieJar, cookieStoreJar } from "@hexcore-js/darwin-client";

new CookieTransport({ cookieJar: memoryCookieJar(cookieHeaderEntrante) }); // SSR / tests
new CookieTransport({ cookieJar: cookieStoreJar() });                      // Cookie Store API
```

| Jar | Lee de |
| :-- | :-- |
| `documentCookieJar()` | `document.cookie` — el default cuando existe `document` |
| `memoryCookieJar(header)` | Un string de header `Cookie:` que vos le pasás |
| `cookieStoreJar()` | La Cookie Store API asincrónica |

⚠️ **Construir un `CookieTransport` fuera de un navegador sin `cookieJar` tira.** Es
deliberado, y el error dice qué hacer: el jar existe para SSR (reenviar la cookie del request
entrante) y para tests, **no como modo de producción**. Fuera del navegador la recomendación es
`BearerTransport`.

### `csrfCookieName`

```ts
new CookieTransport({ csrfCookieName: "mi_csrf" });
```

Por defecto `"csrf"`, que es el default de `CookieConfig.csrf_name` del lado del servidor. Es el
nombre **base**, sin el prefijo `__Host-`: el transporte busca `__Host-<nombre>` y cae a
`<nombre>`, que son exactamente los dos valores que `CookieConfig.name_for("csrf")` puede
devolver según `secure`.

Ese fallback es el punto. El servidor emite `__Host-csrf` sobre HTTPS y `csrf` pelado sobre
HTTP, así que un único nombre hardcodeado acierta en producción y falla en desarrollo, o al
revés — y falla **en silencio**: sin la cookie el cliente no manda el header, y todo request que
muta estado vuelve como un 403 `CsrfValidationError` mientras los `GET` siguen andando. El
chequeo double-submit sólo cubre los métodos que mutan, así que el síntoma es "las lecturas van,
todas las escrituras fallan", que se lee como un bug de permisos y no lo es.

Pasar un nombre ya prefijado también funciona, y entonces se usa exactamente ese y nada más.
⚠️ Igual hay que cambiarlo si el despliegue configuró un `CookieConfig.csrf_name` propio.

⚠️ **Un transporte de cookie en un despliegue cross-site también necesita el servidor
configurado en consecuencia.** `SameSite`, `Secure` y el header CORS
`Access-Control-Allow-Credentials` se setean del lado de Darwin; equivocarse aparece como una
sesión que nunca autentica en silencio, no como un error. Ver la
[guía de configuración de Darwin](../../hexcore/es/configuracion.md#seguridad-cors).

## Cuál elegir

Usá `CookieTransport` cuando el frontend es una app de navegador y el backend es Darwin en el
mismo sitio (o en un sitio que controlás, con CORS y credentials). Es la única opción donde un
XSS exitoso no puede robarse un refresh token de larga vida.

Usá `BearerTransport` en todo lo demás: apps nativas, CLIs, servidor a servidor, y SSR, donde no
hay `document.cookie` del que hacer double-submit ni modelo de origen del navegador en el que
apoyarse.

Si estás haciendo SSR para una app de navegador, probablemente uses los dos — `memoryCookieJar()`
con el header `Cookie` del request entrante en el servidor, y el jar de `document.cookie` por
defecto una vez hidratado. Ver [Sesión y store → SSR](./sesion-y-store.md#ssr).

---

Sigue: **[Sesión y store](./sesion-y-store.md)**.
