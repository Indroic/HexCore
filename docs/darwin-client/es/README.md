# Documentación del cliente de Darwin

`@hexcore-js/darwin-client` es un cliente TypeScript para **Darwin**, el módulo de identidad de
[HexCore](../../hexcore/es/darwin/). Es agnóstico de framework de UI, de runtime y de backend, y
tiene **cero dependencias de runtime**.

El objetivo de diseño es que **el camino correcto sea el default**: `createDarwinClient()` con
una URL base y un transporte ya te da refresh single-flight, un store de sesión y errores
tipados. Nada de esto hay que encenderlo.

> 🇬🇧 [English version](../en/) — la versión de referencia. Se escribe ahí primero.

---

## Empezar

| # | Documento | Cubre |
| :-- | :-- | :-- |
| 1 | **[Instalación](./instalacion.md)** | El paquete, los subpaths y qué necesita cada runtime |
| 2 | **[Inicio rápido](./inicio-rapido.md)** | Un sign-in funcionando en una pantalla, y qué viene de fábrica |

## El cliente

| # | Documento | Cubre |
| :-- | :-- | :-- |
| 3 | **[Transportes](./transportes.md)** | `BearerTransport` vs `CookieTransport`, storages, cookie jars |
| 4 | **[Sesión y store](./sesion-y-store.md)** | El contrato de `useSyncExternalStore`, Svelte, SSR |
| 5 | **[Plugins](./plugins.md)** | Registro explícito y los seis plugins incluidos |
| 6 | **[Errores](./errores.md)** | El único `DarwinError`, sus códigos y los predicados |

## Referencia

| # | Documento | Cubre |
| :-- | :-- | :-- |
| 7 | **[Desarrollo](./desarrollo.md)** | Comandos del workspace, y cómo se vuelca `openapi/` desde el paquete Python |

---

## Por qué existe este paquete

Darwin expone su contrato HTTP sólo desde el código Python: doble transporte cookie/Bearer,
CSRF double-submit derivado por HMAC, y un `access_ttl` de dos minutos que exige refresh
rotativo con detección de reuso. Sin un cliente versionado junto al servidor, cualquier frontend
reimplementa esas cuatro cosas a mano — y equivocarse en cualquiera de ellas es un bug de
seguridad, no de comodidad.

Es también la razón por la que los dos paquetes viven en el mismo repositorio: un gate de CI
puede fallar cuando el cliente y el servidor divergen. El documento OpenAPI de `openapi/` se
vuelca desde el paquete Python y se versiona en git, así que el drift aparece en el diff del
pull request.

---

## Los tres puntos de entrada

| Import | Qué te da | Cuándo lo necesitás |
| :-- | :-- | :-- |
| `@hexcore-js/darwin-client` | El núcleo: `createDarwinClient`, transportes, plugins, errores | Siempre |
| `@hexcore-js/darwin-client/webauthn` | `registerPasskey`, `authenticateWithPasskey`, `isWebAuthnSupported` | Passkeys en un navegador |
| `@hexcore-js/darwin-client/store` | `toSvelteStore` | Svelte |

Los subpaths son puntos de entrada separados y no parte del bundle principal a propósito:
importar `/webauthn` es lo que arrastra el camino de código de `navigator.credentials`, así que
una app de React Native que nunca toca passkeys nunca lo empaqueta.

---

## Qué tenés, de un vistazo

| Necesitás | API |
| :-- | :-- |
| Un cliente | `createDarwinClient({ baseUrl, transport })` |
| Login con email y contraseña | `client.signIn(email, password)` |
| Cerrar sesión | `client.signOut()` |
| Quién soy | `client.me()`, `client.session.getSnapshot()` |
| Una llamada autenticada cruda | `client.$fetch<T>(path, init)` |
| Tokens que tu JS pueda leer | `new BearerTransport({ storage })` |
| Cookies `HttpOnly` | `new CookieTransport()` |
| Binding de React | `useSyncExternalStore(client.session.subscribe, …)` |
| Binding de Svelte | `toSvelteStore(client.session)` |
| TOTP, magic links, OAuth, passkeys, impersonación, organizaciones | Los seis [plugins](./plugins.md) |
| Distinguir un fallo de otro | `err.code`, `isRefreshable`, `isSessionDead`, `isTwoFactorRequired` |

---

## Convenciones de esta documentación

- **El inglés es la versión de referencia.** [`../en/`](../en/) es el original; si las dos se
  contradicen, gana el inglés.
- **Los ⚠️ son modos de falla reales**, no notas de estilo. Casi todos describen algo que no
  levanta ninguna excepción y aparece lejos de su causa.
- Los ejemplos usan `https://api.ejemplo.com` como URL base de Darwin. Nunca incluye `/auth` —
  eso lo agrega el cliente.
