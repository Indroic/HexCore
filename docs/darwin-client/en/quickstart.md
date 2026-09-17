# Quickstart

A complete sign-in, in one screen.

```ts
import { createDarwinClient, BearerTransport, memoryStorage } from "@hexcore-js/darwin-client";

const client = createDarwinClient({
  baseUrl: "https://api.example.com",
  transport: new BearerTransport({ storage: memoryStorage() }),
});

const result = await client.signIn("ana@example.com", "correct-horse-battery");
// …or the username, if the backend enables it:
// const result = await client.signIn("indroic", "correct-horse-battery");

if (result.status === "two-factor-required") {
  // See the plugins guide: `client.twoFactor.complete(result.challenge, code)`
} else {
  console.log(result.session); // { session_id, access_token, expires_in, ... }
}

client.session.getSnapshot();
// { status: "authenticated", me: { actor_id, subject_id, impersonating } }
```

`baseUrl` is the root of the Darwin deployment **without** `/auth` — the client appends that
itself.

## `signIn()` returns a result, it does not throw

Two-factor authentication being enabled is not a programming error and not a failure: it is the
happy path of a correctly configured account. So `signIn()` returns a discriminated union
rather than raising:

```ts
type SignInResult =
  | { status: "signed-in"; session: SessionResponse }
  | { status: "two-factor-required"; challenge: string };
```

Modelling the second case as an exception would force every app to wrap its login in a
`try/catch` and then tell apart, by hand, "bad credentials" (which really is an error) from
"the second step is missing" (which is progress). Bad credentials still throw a
[`DarwinError`](./errors.md); a pending second factor does not.

The `challenge` is what [`twoFactor().complete()`](./plugins.md#twofactor) exchanges to finish
the login.

## What you already have, without configuring anything

### Proactive and reactive refresh, single-flight

Darwin's access token lives for two minutes. The client refreshes it **before** it expires, and
also on the first refreshable 401 — and it does so under a single-flight lock: with N requests
in parallel only one refresh is issued, so the server rotates the token once and not N times.

This matters because Darwin's refresh is *rotating with reuse detection*: a second refresh
carrying the token that was just rotated looks exactly like a stolen token being replayed, and
the server kills the session. A naive client that refreshes per-request logs the user out under
load. That is the failure this lock exists to prevent.

### A session store

`client.session` is a store with the same contract as React's `useSyncExternalStore`, so React
needs no adapter and Svelte needs a three-line one. See
[Session and store](./session-and-store.md).

```ts
client.session.getSnapshot();
// { status: "loading" }
// | { status: "unauthenticated", reason?: "signed-out" | "refresh-failed" | "revoked" }
// | { status: "authenticated", me: { ... } }
```

`reason` is what lets you say *"your session was closed for security reasons"* instead of
logging the user out silently: `"revoked"` means the server detected token reuse.

### Typed errors

One `DarwinError` discriminated by `code`, generated from the Python contract. See
[Errors](./errors.md).

## The rest of the core surface

```ts
await client.signOut();          // closes the session server-side and clears the transport
await client.refresh();          // forces a rotation; you rarely need to call this yourself
const me = await client.me();    // GET /auth/me

// Any Darwin route, authenticated, with refresh and error mapping applied:
const data = await client.$fetch<MyType>("/auth/some/route", { method: "POST", body });
```

`$fetch` is the same primitive the plugins are built on. Reach for it when the server exposes
something this client does not wrap yet — you still get the token handling and the typed errors,
and you are not tempted to hand-roll a second `fetch` that skips both.

## Choosing a transport

The example above uses `BearerTransport` with in-memory storage, which is the right default for
a native app or for SSR. A single-page app served by the same backend usually wants
`CookieTransport` instead, so the refresh token is `HttpOnly` and no JavaScript — including this
package — can read it.

That choice has real security consequences in both directions. It has its own guide:
**[Transports](./transports.md)**.

---

Next: **[Transports](./transports.md)** · **[Session and store](./session-and-store.md)** ·
**[Plugins](./plugins.md)**
