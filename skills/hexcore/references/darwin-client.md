# `@hexcore-js/darwin-client` — consuming Darwin from a client

The TypeScript half of Darwin, published to npm from the same repository as the Python package
and versioned separately (tags `darwin-client-v*`, against `hexcore-v*`). Agnostic of UI
framework, of runtime and of backend; **zero runtime dependencies**; ESM and CJS both shipped;
Node ≥ 20; `sideEffects: false`, so a bundler drops what you do not import.

```bash
npm install @hexcore-js/darwin-client
```

Everything it needs from the environment is `fetch`. Pass your own with
`createDarwinClient({ fetch })` in a runtime that has none, or in a test that intercepts calls —
the reference is captured at construction, so replacing the global afterwards does not reach a
client that is already running.

Do not recall its exports either:

```bash
python $SKILL/scripts/hexcore_surface.py --darwin-client     # every symbol, per subpath
python $SKILL/scripts/hexcore_surface.py --find twoFactor    # searches both packages
```

Three entry points, separate on purpose:

| Subpath | Contains | Needs a browser |
| :-- | :-- | :-- |
| `.` | Core, the two transports, the six plugins, the error type | No |
| `./webauthn` | The `navigator.credentials` flow | Yes, **when called** |
| `./store` | The Svelte adapter | No |

Importing `/webauthn` outside a browser never breaks a bundle: the module loads, and it is
`registerPasskey` / `authenticateWithPasskey` that throw `WebAuthnUnavailableError` when
actually invoked. That is what lets a React Native app import it behind a feature flag with no
conditional import.

---

## Getting started

```ts
import { BearerTransport, createDarwinClient, memoryStorage } from "@hexcore-js/darwin-client";

const client = createDarwinClient({
  baseUrl: "https://api.example.com",
  transport: new BearerTransport({ storage: memoryStorage() }),
});

const result = await client.signIn("ana@example.com", "correct-horse-battery");
```

`baseUrl` is the root of the deployment **without** `/auth` — the client appends that itself.
`transport` has **no default**; see below, it is the one security decision here.

The rest of the core surface: `client.signOut()`, `client.refresh()`, `client.me()`, the session
store `client.session`, and `client.$fetch<T>(path, init)` — the same primitive the plugins are
built on, for a Darwin route this package does not wrap yet. Reach for `$fetch` rather than a
second hand-rolled `fetch`, which would skip both the token handling and the error mapping.

### ⚠️ `signIn()` returns a result, it does not throw

```ts
import type { SignInResult } from "@hexcore-js/darwin-client";
// { status: "signed-in"; session: SessionResponse }
// | { status: "two-factor-required"; challenge: string }
```

A configured second factor is the happy path of a correct account, not a failure. Bad
credentials still throw a `DarwinError`; a pending second factor does not. Code that wraps
`signIn()` in a `try/catch` and treats everything it catches as "wrong password" will tell a 2FA
user their password is wrong.

The `challenge` is what `twoFactor().complete()` exchanges to finish the login.

---

## The contract with the deployment — four clauses, all of them quiet

Neither side validates the other, and every one of the four fails somewhere other than where
the mistake is.

### 1. The CSRF cookie name

`csrfCookieName` defaults to `"csrf"`, matching the server's `CookieConfig.csrf_name`. It is
the **base** name: the transport tries `__Host-<name>` first and falls back to `<name>`, which
are the two values `CookieConfig.name_for("csrf")` can return.

```ts
import { CookieTransport } from "@hexcore-js/darwin-client";

new CookieTransport();                                // both, in that order
new CookieTransport({ csrfCookieName: "mi_csrf" });   // custom CookieConfig.csrf_name
```

The fallback exists because the server's name *changes with `secure`* — `__Host-csrf` over
HTTPS, plain `csrf` over HTTP — so a single hardcoded value is right in production and wrong in
development, or the reverse.

⚠️ **If the deployment sets a custom `csrf_name`, you have to pass it**, and a mismatch is
silent in the worst way: with no cookie found the client sends no `X-CSRF-Token` at all, and the
server answers **403 `CsrfValidationError` on every state-changing request while every `GET`
keeps working**. That reads like a permissions bug and is not one. Read the value off the
deployment rather than off memory:

```python
from hexcore.darwin import CookieConfig

CookieConfig().name_for("csrf")   # -> "__Host-csrf" with the default secure=True
```

### 2. CORS and `trusted_origins`

The server checks the request's `Origin` against `IdentityConfig.trusted_origins` and **fails
closed**: with neither `Origin`/`Referer` nor declared origins, it rejects. Separately, a cookie
transport needs `Access-Control-Allow-Credentials` from the CORS middleware, and
`AppFeatures(csrf=True)` on the app.

⚠️ A CORS mistake reaches the client as `NetworkError`, **not** as a 403 — the browser refuses
the response before any code sees a status. If sign-in fails with `NetworkError` from a browser
and the same request works from `curl`, look at CORS and at credentials before anything else.

⚠️ `"*"` with `allow_credentials=True` is never valid (non-negotiable 12 in `SKILL.md`).

### 3. Which plugins the server has enabled

Registering `passkey()` in the client does **nothing** if the deployment has not enabled the
server-side plugin: the calls answer 404.

| Python extra | Server plugin | Client plugin | Extra subpath |
| :-- | :-- | :-- | :-- |
| `[darwin-two-factor]` | `two_factor` | `twoFactor()` | — |
| `[darwin-magic-link]` | `magic_link` | `magicLink()` | — |
| `[darwin-oauth]` | `oauth` | `oauth()` | — |
| `[darwin-passkey]` | `passkey` | `passkey()` | `./webauthn` |
| `[darwin-impersonate]` | `impersonate` | `impersonate()` | — |
| `[darwin-organization]` | `organization` | `organization()` | — |

`hexcore identity plugins <module>` prints what the server side actually contributes. That is
the list the client's plugin array has to be a subset of.

### 4. The access token's TTL

`TokenConfig.access_ttl` is **2 minutes**. The client refreshes before expiry and on the first
refreshable 401, under a single-flight lock. See below for why replacing that with your own
refresh is how sessions start dying under load.

---

## Transports — the one decision that is a security decision

There is no default, deliberately.

| | `BearerTransport` | `CookieTransport` |
| :-- | :-- | :-- |
| Where the refresh token lives | The `TokenStorage` you pass | `HttpOnly`, in the browser |
| Who can read it | Your JS | Nobody client-side, not even this package |
| Use for | Native apps, CLIs, SSR, server-to-server | A SPA on the same site as Darwin |

```ts
import {
  BearerTransport,
  fromAsyncStorage,
  localStorageAdapter,
  memoryStorage,
} from "@hexcore-js/darwin-client";

new BearerTransport({ storage: memoryStorage() });        // default: lost on reload
new BearerTransport({ storage: localStorageAdapter() });  // persisted
new BearerTransport({ storage: fromAsyncStorage(asyncStorage) }); // React Native
```

⚠️ **`localStorageAdapter()` is not a free upgrade over `memoryStorage()`.** An XSS that
exfiltrates `localStorage` walks away with a *refresh* token good for 30 days, not with a
two-minute access token. `memoryStorage()` is the default because losing the session on reload
is a visible, harmless failure and the alternative fails invisibly and expensively. In a public
web app the answer is `CookieTransport`, not persistence.

### Cookie jars

```ts
import { CookieTransport, cookieStoreJar, memoryCookieJar } from "@hexcore-js/darwin-client";

new CookieTransport({ cookieJar: memoryCookieJar(incomingCookieHeader) }); // SSR, tests
new CookieTransport({ cookieJar: cookieStoreJar() });                      // Cookie Store API
```

In a browser it reads `document.cookie` with no setup (`documentCookieJar()`).

⚠️ **Constructing a `CookieTransport` outside a browser without `cookieJar` throws**, on
purpose. The jar exists for SSR (forwarding the incoming request's `Cookie` header) and for
tests, **not as a production mode**. Outside a browser the answer is `BearerTransport`.

---

## Refresh is single-flight, and that is not an optimisation

Darwin's refresh is **rotating with reuse detection**: a second refresh carrying the token that
was just rotated is indistinguishable from a stolen token being replayed, and the server kills
the whole session family. See the three-layer revocation section of `darwin.md`.

So a naive client that refreshes per request logs its users out under load, and the symptom is a
session that dies only when the app is busy. The single-flight lock in this package is what
prevents it: with N requests in parallel, exactly one refresh is issued.

⚠️ This is the reason not to hand-roll refresh on top of `$fetch`, and the reason two client
instances against the same cookies are a mistake: two clients are two locks.

---

## The session store

```ts
import type { SessionState } from "@hexcore-js/darwin-client";
// { status: "loading" }
// | { status: "unauthenticated"; reason?: "signed-out" | "refresh-failed" | "revoked" }
// | { status: "authenticated"; me: MeResponse }
```

`client.session` exposes `subscribe` / `getSnapshot` / `getServerSnapshot` — the exact contract
of React's `useSyncExternalStore`, so React needs no adapter:

```ts
useSyncExternalStore(
  client.session.subscribe,
  client.session.getSnapshot,
  client.session.getServerSnapshot,
);
```

`getSnapshot` returns a stable reference between changes. ⚠️ Wrapping it in something that
builds a new object per call is an infinite render loop; derive in a `useMemo` over the
snapshot.

Svelte needs `subscribe(run)` to invoke `run` immediately, which the core does not do:

```ts
import { toSvelteStore } from "@hexcore-js/darwin-client/store";
```

Anything else consumes `subscribe`/`getSnapshot` directly; `subscribe` returns the unsubscribe,
and a client that outlives the component keeps the callback alive if you do not call it.

**`reason` is not decoration.** `"signed-out"` is a deliberate `signOut()`; `"refresh-failed"`
is an expired session; `"revoked"` means the server **detected token reuse**. Collapsing the
three into a silent logout hides the only one that might mean somebody else has the user's
token.

### SSR

```ts
const client = createDarwinClient({ baseUrl, transport, hydrateOnCreate: false });
```

⚠️ By default the client hydrates with a `GET /auth/me` at construction and starts in
`"loading"`. On a server that renders and responds before the hydration resolves, **the spinner
gets baked into the HTML** and stays until the bundle boots. `hydrateOnCreate: false` starts at
`"unauthenticated"`, which is the correct thing to render for an unresolved session. To render
the authenticated view, use `memoryCookieJar()` with the incoming `Cookie` header and `await`
`client.me()` explicitly.

---

## Errors

```ts
import {
  DarwinError,
  isRefreshable,
  isSessionDead,
  isTwoFactorRequired,
} from "@hexcore-js/darwin-client";
```

One class, discriminated by `code`. Fields: `code`, `status` (`null` for the two client-side
codes), `detail` (safe to show; written for that, but not a stable string to match on),
`payload`, `wwwAuthenticate`.

`DarwinCode` is **not a closed union** on purpose: a code the server adds and this version does
not know is preserved rather than rejected at compile time. A closed union would make every new
server error code a breaking change for every frontend, and would forbid the safest response to
an unknown code — showing `detail`. The known list is `ERROR_CODES`, generated from
`openapi/darwin.errors.json`.

Two codes exist only client-side: `NonJsonResponse` (something answered that does not speak
Darwin's envelope — a proxy, a load balancer, an HTML error page) and `NetworkError` (`fetch`
itself rejected: no connection, DNS, a failed CORS preflight).

⚠️ **`isSessionDead` is the predicate to wire up.** Once the session is dead the client stops
retrying, and nothing routes to the login screen for you. `isRefreshable` you rarely need — the
client already handles it — except when driving `$fetch` by hand.

---

## Plugins

Registration is explicit, never by discovery:

```ts
import {
  createDarwinClient,
  impersonate,
  magicLink,
  oauth,
  organization,
  passkey,
  twoFactor,
} from "@hexcore-js/darwin-client";

const client = createDarwinClient({
  baseUrl,
  transport,
  plugins: [twoFactor(), magicLink(), oauth(), passkey(), impersonate(), organization()],
});
```

Each hangs off its own key (`client.twoFactor`, …) with full inference: asking for
`client.oauth` without registering `oauth()` is a compile error, not an `undefined` at 3am.
Duplicate ids, a `requires` pointing at an unregistered plugin, and cycles are all rejected **at
construction**, naming the offending plugin — not on the first call in production.

Third-party plugins go through `definePlugin`, or TypeScript cannot infer `Id` and `Api` and
`client.myPlugin` lands as `any`.

### The trap in each bundled plugin

- **`twoFactor`** — ⚠️ `enroll()` is not enough. A factor enrolled but never confirmed protects
  nothing; `confirmed: false` exists so the UI can say the setup is half done instead of showing
  a green check. `disable(code)` requires a valid code, so a hijacked session cannot turn 2FA
  off.
- **`magicLink`** — `request()` answers the same whether or not the account exists; do not use
  it to probe for an email. ⚠️ Its `token` field is only populated in a deployment that does not
  actually send mail. An app that depends on it works in development and breaks on deploy.
- **`oauth`** — the callback returns JSON, not a redirect, so your own router owns the
  navigation. `handleCallback()` returns `{ result, created }`; `created` is the only way to
  tell a first sign-in from a returning user. ⚠️ `unlink()` refuses to leave an account with no
  way in at all — that rejection is a `DarwinError` to handle, not a silent no-op.
- **`passkey`** — pure HTTP transport, testable anywhere; the browser half is the `/webauthn`
  subpath. Call `isWebAuthnSupported()` before showing the button rather than catching the error
  after the click.
- **`impersonate`** — ⚠️ `stop()` does not restore the operator under `CookieTransport`: the
  operator's cookies were replaced and this package cannot read `HttpOnly` ones to put them
  back, so a fresh `signIn()` is needed. Under `BearerTransport` the stored tokens suffice.
  `status()` returns `{ active, actorId, subjectId, reason, expiresAt }` — keep an unmistakable
  banner up for as long as it lasts.
- **`organization`** — roles `owner` > `admin` > `member`. ⚠️ `invite()` returns the token so
  you can **build** the link, not so you can render it: displaying it puts a single-use
  credential into the page, the logs and the browser history of whoever is already signed in,
  who is not the person it was issued for.

```ts
import {
  authenticateWithPasskey,
  isWebAuthnSupported,
  registerPasskey,
} from "@hexcore-js/darwin-client/webauthn";
```

---

## Where the long version is

`docs/darwin-client/en/` in this repository — `installation`, `quickstart`, `transports`,
`session-and-store`, `plugins`, `errors`, `development` — with a Spanish mirror in `es/`. The
server side is `references/darwin.md` and `docs/hexcore/en/darwin/`.
