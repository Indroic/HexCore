# Installation

```bash
npm install @hexcore-js/darwin-client
```

The package has **zero runtime dependencies**. What it needs from the environment is `fetch`,
and nothing else.

## Requirements

| | |
| :-- | :-- |
| Node | `>= 20` |
| Module format | ESM and CJS, both shipped (`import` and `require` both work) |
| TypeScript | Types are bundled; there is no `@types/` package to install |

`fetch` is global from Node 20 onwards, so nothing extra is needed there. In any runtime
without a global `fetch` — or in a test where you want to intercept the calls — pass your own:

```ts
const client = createDarwinClient({ baseUrl, transport, fetch: myFetch });
```

The client stores the reference at construction time, so replacing the global afterwards does
not affect a client that is already running.

## Entry points

The package publishes three entry points, and they are separate on purpose:

```ts
import { createDarwinClient } from "@hexcore-js/darwin-client";
import { registerPasskey } from "@hexcore-js/darwin-client/webauthn";
import { toSvelteStore } from "@hexcore-js/darwin-client/store";
```

| Subpath | Contains | Needs a browser |
| :-- | :-- | :-- |
| `.` | The core, the transports, the six plugins, the error type | No |
| `./webauthn` | The `navigator.credentials` flow | Yes — see below |
| `./store` | The Svelte adapter | No |

The split is what keeps `navigator.credentials` out of a bundle that never asks for it. It is
also why **importing `/webauthn` outside a browser never breaks the bundle**: the module loads
fine, and it is `registerPasskey`/`authenticateWithPasskey` that throw `WebAuthnUnavailableError`
when you actually call them. A React Native app can import the module behind a feature flag
without a conditional import.

`isWebAuthnSupported()` is the check to run before showing the button, rather than catching the
error after the user has already clicked it.

## The package is `sideEffects: false`

Every export is tree-shakeable. A bundler that sees you import only `createDarwinClient` and
`BearerTransport` drops the cookie transport, the six plugins and the Svelte adapter from the
output. This is worth knowing when reading the bundle size: the published size of the package
is not the size of what reaches your users.

## Which version goes with which server

The client is versioned next to the Python package in the same repository, and the OpenAPI
document it generates its types from lives under `openapi/`, committed to git. There is no
version-matching table to consult: a server that adds an error code the client does not know
about yet keeps working, because `err.code` preserves unknown values instead of rejecting them
at compile time — see [Errors](./errors.md).

---

Next: **[Quickstart](./quickstart.md)**.
