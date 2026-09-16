# Errors

A single `DarwinError`, discriminated by `code` — not eighteen subclasses to keep in sync with
the backend by hand.

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

`err.code` is a `DarwinCode`: the known values of the Python contract autocomplete in the
editor, but a code the backend adds and this client does not know about yet is **preserved
anyway** — it is not rejected at compile time.

That is the whole reason the type is not a closed union. A closed union would mean that every
new server-side error code is a breaking change for every frontend, and that the safest
response to an unrecognised code — showing the server's `detail` — would be the one the type
system forbids. The generated list is in `src/generated/error-codes.ts`, exported as
`ERROR_CODES`, and it is regenerated from `openapi/darwin.errors.json`.

## The predicates

```ts
isRefreshable(err);       // the access token expired; a refresh will fix it
isSessionDead(err);       // the session is gone; only a new sign-in will fix it
isTwoFactorRequired(err); // a second factor is pending
```

These are the same predicates the core uses internally. They are exported because an app needs
them too — for instance, so that a pending 2FA does not get the same generic error message as a
wrong password.

You rarely need `isRefreshable` yourself: the client already refreshes on the first refreshable
401, single-flight. It is exported for the case where you are driving `$fetch` by hand against a
route this package does not wrap.

`isSessionDead` is the one worth wiring up. Once the session is dead the client stops retrying
against a refresh that is already known to be down, so the app has to route to the login screen
itself — nothing will do it for you.

## The two client-side codes

`NonJsonResponse` and `NetworkError` are the only two codes that exist purely on the client
side:

| Code | What it actually means |
| :-- | :-- |
| `NonJsonResponse` | Something answered that does not speak Darwin's envelope — a proxy, a load balancer, an HTML error page |
| `NetworkError` | `fetch` itself rejected: no connection, DNS, a CORS preflight that failed |

⚠️ **A CORS misconfiguration surfaces as `NetworkError`, not as a 403.** The browser refuses the
response before your code ever sees a status, so there is no server error to read. If sign-in
fails with `NetworkError` from a browser but the same request works from `curl`, look at the
Darwin CORS configuration and at `credentials` before looking at anything else.

## `parseWwwAuthenticate`

```ts
import { parseWwwAuthenticate } from "@hexcore-js/darwin-client";
```

Darwin reports *why* a 401 happened in the `WWW-Authenticate` header. The client parses it to
decide whether a 401 is refreshable, and exports the parser for apps that want to read the
reason themselves.

## The shape

| Field | Type | |
| :-- | :-- | :-- |
| `code` | `DarwinCode` | The thing to branch on |
| `status` | `number \| null` | The HTTP status — `null` for the two client-side codes |
| `detail` | `string` | The server's human-readable message |
| `payload` | `Record<string, unknown>` | The raw envelope, for the fields a specific code carries |
| `wwwAuthenticate` | `ParsedWwwAuthenticate \| undefined` | The parsed header, when there was one |

Branch on `code`. `status` is for logging and for the handful of cases where the same code can
arrive with different statuses; `detail` is safe to show to a user, because Darwin writes it for
that, but it is not a stable string to match against.

---

Next: **[Development](./development.md)**.
