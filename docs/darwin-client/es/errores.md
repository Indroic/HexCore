# Errores

Un único `DarwinError`, discriminado por `code` — no dieciocho subclases que sincronizar a mano
con el backend.

```ts
import {
  DarwinError,
  isRefreshable,
  isSessionDead,
  isTwoFactorRequired,
} from "@hexcore-js/darwin-client";

try {
  await client.signIn(email, password);
} catch (err) {
  if (err instanceof DarwinError) {
    console.log(err.code, err.status, err.detail);
  }
}
```

## `err.code`

`err.code` es un `DarwinCode`: los valores conocidos del contrato Python autocompletan en el
editor, pero un código que el backend agregue y este cliente todavía no conozca **se preserva
igual** — no se rechaza en tiempo de compilación.

Esa es toda la razón por la que el tipo no es una unión cerrada. Una unión cerrada significaría
que cada código de error nuevo del servidor es un breaking change para todo frontend, y que la
respuesta más segura ante un código desconocido — mostrar el `detail` del servidor — sería
justamente la que el sistema de tipos prohíbe. La lista generada vive en
`src/generated/error-codes.ts`, exportada como `ERROR_CODES`, y se regenera desde
`openapi/darwin.errors.json`.

## Los predicados

```ts
isRefreshable(err);       // el access token venció; un refresh lo arregla
isSessionDead(err);       // la sesión no está más; sólo un sign-in nuevo lo arregla
isTwoFactorRequired(err); // hay un segundo factor pendiente
```

Son los mismos predicados que el núcleo usa por dentro. Están exportados porque una app también
los necesita — por ejemplo, para que un 2FA pendiente no reciba el mismo mensaje de error
genérico que una contraseña equivocada.

Rara vez necesitás `isRefreshable` vos: el cliente ya refresca al primer 401 refrescable,
single-flight. Está exportado para el caso en que estés manejando `$fetch` a mano contra una ruta
que este paquete no envuelve.

`isSessionDead` sí vale la pena cablearlo. Una vez que la sesión está muerta el cliente deja de
reintentar contra un refresh que ya se sabe caído, así que la app tiene que ir a la pantalla de
login por su cuenta — nadie lo va a hacer por vos.

## Los dos códigos del lado del cliente

`NonJsonResponse` y `NetworkError` son los dos únicos códigos que existen puramente del lado del
cliente:

| Código | Qué significa de verdad |
| :-- | :-- |
| `NonJsonResponse` | Algo contestó que no habla el envelope de Darwin — un proxy, un balanceador, una página de error en HTML |
| `NetworkError` | El `fetch` mismo rechazó: sin conexión, DNS, un preflight de CORS que falló |

⚠️ **Una mala configuración de CORS aparece como `NetworkError`, no como un 403.** El navegador
rechaza la respuesta antes de que tu código llegue a ver un status, así que no hay ningún error
de servidor que leer. Si el sign-in falla con `NetworkError` desde un navegador pero el mismo
request anda desde `curl`, mirá la configuración CORS de Darwin y `credentials` antes que
cualquier otra cosa.

## `parseWwwAuthenticate`

```ts
import { parseWwwAuthenticate } from "@hexcore-js/darwin-client";
```

Darwin informa *por qué* ocurrió un 401 en el header `WWW-Authenticate`. El cliente lo parsea
para decidir si un 401 es refrescable, y exporta el parser para las apps que quieran leer el
motivo por su cuenta.

## La forma

| Campo | Tipo | |
| :-- | :-- | :-- |
| `code` | `DarwinCode` | Lo que hay que ramificar |
| `status` | `number \| null` | El status HTTP — `null` en los dos códigos del lado del cliente |
| `detail` | `string` | El mensaje legible del servidor |
| `payload` | `Record<string, unknown>` | El envelope crudo, para los campos que lleve un código concreto |
| `wwwAuthenticate` | `ParsedWwwAuthenticate \| undefined` | El header parseado, cuando lo hubo |

Ramificá sobre `code`. `status` es para los logs y para el puñado de casos en que el mismo código
llega con distintos status; `detail` se puede mostrar al usuario, porque Darwin lo escribe para
eso, pero no es un string estable contra el que matchear.

---

Sigue: **[Desarrollo](./desarrollo.md)**.
