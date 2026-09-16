# @hexcore-js/darwin-client

[![npm](https://img.shields.io/npm/v/@hexcore-js/darwin-client?color=blue)](https://www.npmjs.com/package/@hexcore-js/darwin-client)
[![Bundle size](https://img.shields.io/bundlephobia/minzip/@hexcore-js/darwin-client)](https://bundlephobia.com/package/@hexcore-js/darwin-client)
[![Node](https://img.shields.io/node/v/@hexcore-js/darwin-client)](https://www.npmjs.com/package/@hexcore-js/darwin-client)
[![License](https://img.shields.io/npm/l/@hexcore-js/darwin-client)](https://github.com/Indroic/HexCore/blob/master/LICENSE)

📖 **[Full documentation](https://github.com/Indroic/HexCore/tree/master/docs/darwin-client/en/)** ·
🇪🇸 **[Documentación en español](https://github.com/Indroic/HexCore/tree/master/docs/darwin-client/es/)** ·
🐙 **[Repository](https://github.com/Indroic/HexCore)**

A UI-framework-, runtime- and backend-agnostic TypeScript client for **Darwin**, the identity
module of [HexCore](https://github.com/Indroic/HexCore/tree/master/packages/hexcore). Zero
runtime dependencies.

## Why it exists

Darwin only publishes its HTTP contract from the Python code: dual cookie/Bearer transport,
HMAC-derived double-submit CSRF, and a two-minute `access_ttl` that requires rotating refresh
with reuse detection. Without a client versioned next to the server, every frontend
reimplements those four things by hand — and getting any of them wrong is a security bug, not
an inconvenience.

## Installation

```bash
npm install @hexcore-js/darwin-client
```

## Quickstart

```ts
import { createDarwinClient, BearerTransport, memoryStorage } from "@hexcore-js/darwin-client";

const client = createDarwinClient({
  baseUrl: "https://api.example.com",
  transport: new BearerTransport({ storage: memoryStorage() }),
});

const result = await client.signIn("ana@example.com", "correct-horse-battery");

if (result.status === "two-factor-required") {
  // See the plugins section: `client.twoFactor.complete(result.challenge, code)`
} else {
  console.log(result.session); // { session_id, access_token, expires_in, ... }
}

client.session.getSnapshot();
// { status: "authenticated", me: { actor_id, subject_id, impersonating } }
```

`createDarwinClient()` already ships, with nothing else required:

- **Proactive and reactive refresh, single-flight.** Before the access token expires, or on the
  first refreshable 401, a single refresh fires even with N requests in parallel — the server
  rotates the token once, not N times.
- **A session store** (`client.session`) with the same contract as React's
  [`useSyncExternalStore`](https://react.dev/reference/react/useSyncExternalStore).
- **Typed errors** (`DarwinError`, with a `code` generated from the Python contract — see
  [Errors](#errors)).

## Transports

| | `BearerTransport` | `CookieTransport` |
|---|---|---|
| Where the token lives | The `TokenStorage` you pass in (memory by default) | `HttpOnly`, in the browser |
| Who can read it | Your JS | Nobody on the client side — not even this package |
| Typical use | Native apps, SSR, anything that is not a browser sharing a site with the backend | A SPA served by (or with CORS + credentials towards) the same Darwin backend |

```ts
import { BearerTransport, memoryStorage, localStorageAdapter } from "@hexcore-js/darwin-client";

// The default: process memory only — lost on reload.
new BearerTransport({ storage: memoryStorage() });

// Persisted across reloads. Read the warning on `memoryStorage()`: an XSS that exfiltrates
// `localStorage` walks away with a refresh token good for weeks, not a two-minute access token.
new BearerTransport({ storage: localStorageAdapter() });
```

```ts
import { CookieTransport } from "@hexcore-js/darwin-client";

// In a real browser: it reads the CSRF cookie from `document.cookie` by itself.
const client = createDarwinClient({
  baseUrl: "https://api.example.com",
  transport: new CookieTransport(),
});
```

`CookieTransport` requires `credentials: "include"` on every request — the client adds that on
its own, there is nothing to configure.

## Session and store

`client.session` exposes `subscribe`, `getSnapshot` and `getServerSnapshot` — the exact
contract of `useSyncExternalStore`, so React needs no adapter:

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

Svelte expects something different: `subscribe(run)` has to invoke `run` **immediately** with
the current value, not only on the next change. That is what the `/store` subpath is for:

```ts
import { toSvelteStore } from "@hexcore-js/darwin-client/store";

export const session = toSvelteStore(client.session);
// in a component: `$session.status`
```

### SSR

```ts
const client = createDarwinClient({
  baseUrl,
  transport,
  hydrateOnCreate: false, // the initial state is "unauthenticated", not an eternal "loading"
});
```

Without this, the server never resolves the hydration (`GET /auth/me`) and the `"loading"`
spinner ends up rendered into the HTML.

## Plugins

Registration is **explicit** — never by discovery. The list you hand to `createDarwinClient` is
the complete list; a misdirected `requires` or a cycle between plugins fails at app startup, not
in production the first time somebody uses it.

```ts
import {
  createDarwinClient,
  twoFactor,
  magicLink,
  oauth,
  passkey,
  impersonate,
  organization,
} from "@hexcore-js/darwin-client";

const client = createDarwinClient({
  baseUrl,
  transport,
  plugins: [twoFactor(), magicLink(), oauth(), passkey(), impersonate(), organization()],
});
```

Each plugin hangs off its own key (`client.twoFactor`, `client.oauth`, ...) with full type
inference — adding or removing a plugin from the list changes the type of `client` in the
editor, not just at runtime.

### `twoFactor()`

TOTP. `complete()` exchanges the `challenge` carried by a `signIn()` that came back with
`status: "two-factor-required"` and finishes the login.

```ts
const enrollment = await client.twoFactor.enroll(); // { secret, uri, confirmed: false }
// ...the user scans `uri` and confirms with a code...
await client.twoFactor.confirm(code);

const result = await client.signIn(email, password);
if (result.status === "two-factor-required") {
  await client.twoFactor.complete(result.challenge, code);
}
```

### `magicLink()`

Passwordless login. `request()` answers the same way whether or not the account exists — do not
use it to infer whether an email has an account.

```ts
await client.magicLink.request(email);
// ...the user clicks the link in the email (it carries `email` and `token`)...
await client.magicLink.consume(email, token);
```

### `oauth()`

Login or linking with an external provider. The callback returns JSON, not a redirect — your
app's return route handler exchanges it with `handleCallback()`.

```ts
const { url } = await client.oauth.start("google", redirectUri);
window.location.assign(url);
// ...the provider comes back to `redirectUri` with `?code=...&state=...`...
const { result, created } = await client.oauth.handleCallback("google", {
  code, state, redirectUri,
});
if (created) {
  // new account: send to onboarding rather than to the home screen
}
```

### `passkey()` and `@hexcore-js/darwin-client/webauthn`

`passkey` is pure HTTP transport: the WebAuthn options travel as raw JSON without touching
`navigator.credentials`. For the complete browser flow, use the `/webauthn` subpath:

```ts
import { registerPasskey, authenticateWithPasskey } from "@hexcore-js/darwin-client/webauthn";

const summary = await registerPasskey(client.passkey, "my laptop");
const result = await authenticateWithPasskey(client.passkey, email);
```

`isWebAuthnSupported()` lets you check support before showing the button. Outside a browser
(Node, React Native), `registerPasskey`/`authenticateWithPasskey` throw
`WebAuthnUnavailableError` when invoked — importing the subpath never breaks a bundle that does
not use it.

### `impersonate()`

An operator temporarily acts as another user, with a reason and an expiry audited server-side.

```ts
await client.impersonate.start(userId, "investigating a reported bug");
// ...the session store now reflects the impersonated subject...
await client.impersonate.stop();
// it does not touch the store on its own: with cookies you need a fresh signIn() to be the
// operator again; with bearer the tokens you already had stored are enough.
```

### `organization()`

Multi-tenancy with roles (`owner` > `admin` > `member`) and invitations.

```ts
const org = await client.organization.create("Acme");
const issued = await client.organization.invite(org.id, "new@acme.com", "admin");
// `issued.token` is single-use: in production build the link yourself and do not display it.
await client.organization.acceptInvitation(token);
```

## Errors

A single `DarwinError`, discriminated by `code` — not eighteen subclasses to keep in sync with
the backend by hand.

```ts
import { DarwinError, isRefreshable, isSessionDead, isTwoFactorRequired } from "@hexcore-js/darwin-client";

try {
  await client.signIn(email, password);
} catch (err) {
  if (err instanceof DarwinError) {
    console.log(err.code, err.status, err.detail);
  }
}
```

- `err.code` is a `DarwinCode`: the known values of the Python contract autocomplete in the
  editor, but a code the backend adds and this client does not know about yet is preserved
  anyway — it is not rejected at compile time.
- `isRefreshable`/`isSessionDead`/`isTwoFactorRequired` are the same predicates the core uses
  internally; they are exported because an app needs them too (for instance, so that a pending
  2FA does not get the same generic error message as a wrong password).
- `NonJsonResponse` and `NetworkError` are the only two codes that exist purely on the client
  side: a proxy or load balancer that does not speak Darwin's envelope, and a `fetch` that
  rejected (no connection, CORS).

## Development

```bash
npm install
npm -w @hexcore-js/darwin-client run gen        # regenerates src/generated/ from openapi/
npm -w @hexcore-js/darwin-client run typecheck
npm -w @hexcore-js/darwin-client run test
npm -w @hexcore-js/darwin-client run build
```

`openapi/` is dumped from the Python package with
`uv run python scripts/darwin_openapi.py --write` (see `packages/hexcore/scripts/`) and is
committed to git: the drift between the contract and the client shows up in the diff of the
pull request.

See [`CONTRIBUTING.md`](https://github.com/Indroic/HexCore/blob/master/CONTRIBUTING.md) for the
full contribution flow.

## License

MIT © David Latosefki. See [`LICENSE`](https://github.com/Indroic/HexCore/blob/master/LICENSE).
