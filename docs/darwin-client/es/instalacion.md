# Instalación

```bash
npm install @hexcore-js/darwin-client
```

El paquete tiene **cero dependencias de runtime**. Lo único que necesita del entorno es `fetch`,
y nada más.

## Requisitos

| | |
| :-- | :-- |
| Node | `>= 20` |
| Formato de módulo | ESM y CJS, los dos publicados (`import` y `require` funcionan) |
| TypeScript | Los tipos vienen incluidos; no hay ningún paquete `@types/` que instalar |

`fetch` es global desde Node 20, así que ahí no hace falta nada extra. En cualquier runtime sin
un `fetch` global — o en un test donde quieras interceptar las llamadas — pasá el tuyo:

```ts
const client = createDarwinClient({ baseUrl, transport, fetch: miFetch });
```

El cliente guarda la referencia al construirse, así que reemplazar el global después no afecta a
un cliente que ya está andando.

## Puntos de entrada

El paquete publica tres puntos de entrada, y están separados a propósito:

```ts
import { createDarwinClient } from "@hexcore-js/darwin-client";
import { registerPasskey } from "@hexcore-js/darwin-client/webauthn";
import { toSvelteStore } from "@hexcore-js/darwin-client/store";
```

| Subpath | Contiene | Necesita un navegador |
| :-- | :-- | :-- |
| `.` | El núcleo, los transportes, los seis plugins, el tipo de error | No |
| `./webauthn` | El flujo de `navigator.credentials` | Sí — ver abajo |
| `./store` | El adaptador de Svelte | No |

La separación es lo que mantiene `navigator.credentials` fuera de un bundle que no lo pide. Es
también la razón por la que **importar `/webauthn` fuera de un navegador nunca rompe el
bundle**: el módulo carga bien, y son `registerPasskey`/`authenticateWithPasskey` los que tiran
`WebAuthnUnavailableError` cuando efectivamente los llamás. Una app de React Native puede
importar el módulo detrás de un feature flag sin un import condicional.

`isWebAuthnSupported()` es el chequeo que hay que correr antes de mostrar el botón, en vez de
atrapar el error después de que el usuario ya hizo click.

## El paquete es `sideEffects: false`

Todos los exports son tree-shakeables. Un bundler que ve que sólo importás `createDarwinClient`
y `BearerTransport` deja afuera el transporte de cookie, los seis plugins y el adaptador de
Svelte. Vale la pena saberlo al mirar el tamaño del bundle: el tamaño publicado del paquete no
es el tamaño de lo que llega a tus usuarios.

## Qué versión va con qué servidor

El cliente se versiona junto al paquete Python en el mismo repositorio, y el documento OpenAPI
del que genera sus tipos vive en `openapi/`, versionado en git. No hay ninguna tabla de
compatibilidad que consultar: un servidor que agrega un código de error que el cliente todavía
no conoce sigue funcionando, porque `err.code` preserva los valores desconocidos en vez de
rechazarlos en tiempo de compilación — ver [Errores](./errores.md).

---

Sigue: **[Inicio rápido](./inicio-rapido.md)**.
