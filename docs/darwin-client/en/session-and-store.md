# Session and store

`client.session` is the single source of truth for "who is signed in". It is a store, not a
snapshot you have to poll: it updates on sign-in, on sign-out, on a successful refresh, and on
a session the server killed.

## The state

```ts
type SessionState =
  | { status: "loading" }
  | { status: "unauthenticated"; reason?: "signed-out" | "refresh-failed" | "revoked" }
  | { status: "authenticated"; me: MeResponse };
```

`reason` exists so the UI can tell three different things apart, and all three deserve a
different message:

| `reason` | What happened | What a good UI says |
| :-- | :-- | :-- |
| `"signed-out"` | A deliberate `signOut()` | Nothing; go to the login screen |
| `"refresh-failed"` | The refresh failed — the session is dead and marked so it is not retried | "Your session expired" |
| `"revoked"` | Token reuse was detected server-side | "We closed your session for security reasons" |

Without `reason`, all three collapse into a silent logout, and the third one — the only one
that might mean somebody else has the user's token — is exactly the one you do not want to be
silent.

## Background refresh

The client keeps the access token fresh **without needing any request from your app**: it
tracks the token's expiry and schedules a refresh a few seconds ahead of it, and it revalidates
on `visibilitychange`/`online` so a tab that was backgrounded (where browsers throttle timers)
catches up as soon as it becomes active again. A refresh that fails for a transient reason —
the network dropped, a proxy answered with garbage once — does not tear the session down; only
a real answer from the server (the refresh token is invalid, revoked, or the session reached
its ceiling) moves the store to `"unauthenticated"`.

Call `client.dispose()` when you are done with a client — unmounting the app, a hot reload, a
test's teardown — to stop that timer and remove the listeners.

## React

`client.session` exposes `subscribe`, `getSnapshot` and `getServerSnapshot` — the exact
contract of `useSyncExternalStore`, so React needs no adapter at all:

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

`getSnapshot` returns a stable reference between changes, which is what `useSyncExternalStore`
requires to avoid an infinite render loop. Do not wrap it in something that builds a new object
each call — deriving state belongs in a `useMemo` over the returned snapshot, not in the
snapshot itself.

## Svelte

Svelte expects something slightly different: `subscribe(run)` has to invoke `run`
**immediately** with the current value, not only on the next change. That is what the `/store`
subpath is for:

```ts
import { toSvelteStore } from "@hexcore-js/darwin-client/store";

export const session = toSvelteStore(client.session);
// in a component: `$session.status`
```

It is a separate subpath rather than part of the core because the core stays free of any
framework's conventions. The adapter is a few lines; the alternative — the core guessing which
framework is asking — is not.

## Vue, Solid, Angular, anything else

Any framework that can consume a `subscribe`/`getSnapshot` pair can consume this store
directly. The shape is deliberately the smallest one that works:

```ts
const unsubscribe = client.session.subscribe(() => {
  doSomethingWith(client.session.getSnapshot());
});
```

`subscribe` returns the unsubscribe function. Call it on teardown; a client that outlives the
component would otherwise keep the callback alive.

## SSR

```ts
const client = createDarwinClient({
  baseUrl,
  transport,
  hydrateOnCreate: false, // initial state is "unauthenticated", not an eternal "loading"
});
```

By default the client hydrates the session with a `GET /auth/me` as soon as it is constructed,
and the initial state is `"loading"`. On a server that is wrong in a way that is easy to miss:
the server renders and responds before the hydration resolves, so **the `"loading"` spinner
ends up baked into the HTML** and the user sees it until the client-side bundle boots and
replaces it.

`hydrateOnCreate: false` makes the initial state `"unauthenticated"` instead, which is the
correct thing to render for a request whose session you have not resolved yet.

If you do want the server to render the authenticated view, resolve it explicitly — construct
the client with a [`memoryCookieJar()`](./transports.md#cookie-jars) holding the incoming
request's `Cookie` header, `await client.me()`, and render from that.

---

Next: **[Plugins](./plugins.md)**.
