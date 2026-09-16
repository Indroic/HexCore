# Darwin Client Documentation

`@hexcore-js/darwin-client` is a TypeScript client for **Darwin**, the identity module of
[HexCore](../../hexcore/en/darwin/). It is agnostic of UI framework, of runtime and of backend,
and it has **zero runtime dependencies**.

The design goal is that **the correct path is the default one**: `createDarwinClient()` with a
base URL and a transport already gives you single-flight token refresh, a session store and
typed errors. Nothing here has to be switched on.

> 🇪🇸 [Versión en español](../es/) — a complete translation of this documentation.

---

## Getting started

| # | Document | Covers |
| :-- | :-- | :-- |
| 1 | **[Installation](./installation.md)** | The package, the subpath exports, and what each runtime needs |
| 2 | **[Quickstart](./quickstart.md)** | A working sign-in in one screen, and what you get for free |

## The client

| # | Document | Covers |
| :-- | :-- | :-- |
| 3 | **[Transports](./transports.md)** | `BearerTransport` vs `CookieTransport`, token storage, cookie jars |
| 4 | **[Session and store](./session-and-store.md)** | The `useSyncExternalStore` contract, Svelte, SSR |
| 5 | **[Plugins](./plugins.md)** | Explicit registration and all six bundled plugins |
| 6 | **[Errors](./errors.md)** | The single `DarwinError`, its codes and the predicates |

## Reference

| # | Document | Covers |
| :-- | :-- | :-- |
| 7 | **[Development](./development.md)** | Workspace commands, and how `openapi/` is dumped from the Python package |

---

## Why this package exists

Darwin only publishes its HTTP contract from the Python code: dual cookie/Bearer transport,
HMAC-derived double-submit CSRF, and a two-minute `access_ttl` that requires rotating refresh
with reuse detection. Without a client versioned next to the server, every frontend
reimplements those four things by hand — and getting any of them wrong is a security bug, not
an inconvenience.

That is also why the two packages live in the same repository: a CI gate can fail when the
client and the server drift apart. The OpenAPI document under `openapi/` is dumped from the
Python package and committed, so the drift shows up in the diff of the pull request.

---

## The three entry points

| Import | What it gives you | When you need it |
| :-- | :-- | :-- |
| `@hexcore-js/darwin-client` | The core: `createDarwinClient`, transports, plugins, errors | Always |
| `@hexcore-js/darwin-client/webauthn` | `registerPasskey`, `authenticateWithPasskey`, `isWebAuthnSupported` | Passkeys in a browser |
| `@hexcore-js/darwin-client/store` | `toSvelteStore` | Svelte |

The subpaths are separate entry points and not part of the main bundle on purpose: importing
`/webauthn` is what pulls in the `navigator.credentials` code path, so a React Native app that
never touches passkeys never ships it.

---

## What you get, at a glance

| You need | API |
| :-- | :-- |
| A client | `createDarwinClient({ baseUrl, transport })` |
| Email + password sign-in | `client.signIn(email, password)` |
| Sign out | `client.signOut()` |
| Who am I | `client.me()`, `client.session.getSnapshot()` |
| A raw authenticated call | `client.$fetch<T>(path, init)` |
| Tokens your JS can read | `new BearerTransport({ storage })` |
| `HttpOnly` cookies | `new CookieTransport()` |
| React binding | `useSyncExternalStore(client.session.subscribe, …)` |
| Svelte binding | `toSvelteStore(client.session)` |
| TOTP, magic links, OAuth, passkeys, impersonation, organizations | The six [plugins](./plugins.md) |
| Discriminating one failure from another | `err.code`, `isRefreshable`, `isSessionDead`, `isTwoFactorRequired` |

---

## Conventions used here

- **English is the reference version.** [`../es/`](../es/) is its translation; if the two ever
  contradict each other, English wins.
- **⚠️ warnings are real failure modes**, not style notes. Most of them describe something that
  raises no exception and surfaces far away from its cause.
- Examples use `https://api.example.com` as the Darwin base URL. It never includes `/auth` —
  the client appends that itself.
