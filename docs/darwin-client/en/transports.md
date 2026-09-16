# Transports

A transport decides **where the tokens live and who can read them**. It is the one decision in
this client that is a security decision rather than a preference, and it is required — there is
no default.

| | `BearerTransport` | `CookieTransport` |
| :-- | :-- | :-- |
| Where the token lives | The `TokenStorage` you pass in (memory by default) | `HttpOnly`, in the browser |
| Who can read it | Your JS | Nobody on the client side — not even this package |
| Typical use | Native apps, SSR, anything that is not a browser sharing a site with the backend | A SPA served by (or with CORS + credentials towards) the same Darwin backend |

## `BearerTransport`

```ts
import { BearerTransport, memoryStorage, localStorageAdapter } from "@hexcore-js/darwin-client";

// The default: process memory only — lost on reload.
new BearerTransport({ storage: memoryStorage() });

// Persisted across reloads.
new BearerTransport({ storage: localStorageAdapter() });
```

⚠️ **`localStorageAdapter()` is not a free upgrade over `memoryStorage()`.** An XSS that
exfiltrates `localStorage` walks away with a *refresh* token that is good for weeks, not with a
two-minute access token. In a browser, that is precisely the reason `CookieTransport` exists.
Persisting to `localStorage` is a reasonable trade in an environment where XSS is not part of
the threat model, and a bad one in a public web app.

`memoryStorage()` is the default because losing the session on reload is a visible, harmless
failure, and the alternative fails invisibly and expensively.

### React Native and other async storages

```ts
import { BearerTransport, fromAsyncStorage } from "@hexcore-js/darwin-client";

new BearerTransport({ storage: fromAsyncStorage(AsyncStorage) });
```

`fromAsyncStorage` adapts anything with the `getItem`/`setItem`/`removeItem` promise-returning
shape, which is what React Native's `AsyncStorage` and most secure-storage wrappers expose.

## `CookieTransport`

```ts
import { createDarwinClient, CookieTransport } from "@hexcore-js/darwin-client";

// In a real browser: it reads the CSRF cookie from `document.cookie` by itself.
const client = createDarwinClient({
  baseUrl: "https://api.example.com",
  transport: new CookieTransport(),
});
```

The refresh token is set by the server as `HttpOnly`, so this package never sees it. What the
transport *does* handle is the other half of the contract: Darwin uses **HMAC-derived
double-submit CSRF**, which means every state-changing request has to echo a value that the
server put in a readable cookie. `CookieTransport` reads it and echoes it for you.

`CookieTransport` requires `credentials: "include"` on every request — the client adds that on
its own, there is nothing to configure.

### Cookie jars

In a browser the transport reads `document.cookie` with no further setup. Outside one — SSR,
tests, a runtime with the Cookie Store API — you pass a jar explicitly:

```ts
import { CookieTransport, memoryCookieJar, cookieStoreJar } from "@hexcore-js/darwin-client";

new CookieTransport({ cookieJar: memoryCookieJar(incomingCookieHeader) }); // SSR / tests
new CookieTransport({ cookieJar: cookieStoreJar() });                      // Cookie Store API
```

| Jar | Reads from |
| :-- | :-- |
| `documentCookieJar()` | `document.cookie` — the default when `document` exists |
| `memoryCookieJar(header)` | A `Cookie:` header string you hand it |
| `cookieStoreJar()` | The asynchronous Cookie Store API |

⚠️ **Constructing a `CookieTransport` outside a browser without `cookieJar` throws.** That is
deliberate, and the error says what to do: the jar exists for SSR (forwarding the incoming
request's cookie) and for tests, **not as a production mode**. Outside a browser the
recommendation is `BearerTransport`.

### `csrfCookieName`

```ts
new CookieTransport({ csrfCookieName: "my_csrf" });
```

Defaults to `"csrf"`, which is the default of `CookieConfig.csrf_name` server-side. It is the
**base** name, without the `__Host-` prefix: the transport looks for `__Host-<name>` first and
falls back to `<name>`, which are exactly the two values `CookieConfig.name_for("csrf")` can
return depending on `secure`.

That fallback is the point. The server emits `__Host-csrf` over HTTPS and plain `csrf` over
HTTP, so a single hardcoded name is right in production and wrong in development, or the other
way round — and it is wrong **silently**: with no cookie the client sends no header, and every
state-changing request comes back as a 403 `CsrfValidationError` while `GET`s keep working. The
double-submit check only covers the methods that mutate, so the symptom is "reads fine, writes
all fail", which looks like a permissions bug and is not one.

Passing an already-prefixed name works too, and then that exact name is the only one tried.
⚠️ You still have to change this if the deployment set a custom `CookieConfig.csrf_name`.

⚠️ **A cookie transport in a cross-site deployment also needs the server configured to match.**
`SameSite`, `Secure` and the CORS `Access-Control-Allow-Credentials` header are set on the
Darwin side; getting them wrong shows up as a session that silently never authenticates rather
than as an error. See
[Darwin's configuration guide](../../hexcore/en/configuration.md#security-cors).

## Which one to pick

Use `CookieTransport` when the frontend is a browser app and the backend is Darwin on the same
site (or on a site you control with CORS and credentials). It is the only option where a
successful XSS cannot steal a long-lived refresh token.

Use `BearerTransport` everywhere else: native apps, CLIs, server-to-server, and SSR, where
there is no `document.cookie` to double-submit from and no browser origin model to lean on.

If you are doing SSR for a browser app, you will likely use both — `memoryCookieJar()` with the
incoming request's `Cookie` header on the server, and the default `document.cookie` jar once
hydrated. See [Session and store → SSR](./session-and-store.md#ssr).

---

Next: **[Session and store](./session-and-store.md)**.
