# Plugins

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
  defineAccessControl,
  rbac,
  drbac,
} from "@hexcore-js/darwin-client";

const ac = defineAccessControl({
  resources: { invoice: ["read", "create", "approve"] },
  roles: ["viewer", "accountant", "admin"],
});

const client = createDarwinClient({
  baseUrl,
  transport,
  plugins: [
    twoFactor(), magicLink(), oauth(), passkey(), impersonate(), organization(),
    rbac({ ac }), drbac({ ac }),
  ],
});
```

Each plugin hangs off its own key (`client.twoFactor`, `client.oauth`, …) with full type
inference — adding or removing a plugin from the list changes the type of `client` in the
editor, not just at runtime. Asking for `client.oauth` without having registered `oauth()` is a
compile error, not an `undefined` at 3am.

⚠️ **Every plugin still requires its server-side counterpart.** Registering `passkey()` in the
client does nothing if the Darwin deployment has not enabled the passkey plugin; the calls
return 404. See
[Darwin's bundled plugins](../../hexcore/en/darwin/bundled-plugins.md) for the server side, and
[writing a plugin](../../hexcore/en/darwin/writing-plugins.md) if you are adding your own.

---

## `twoFactor()`

TOTP. `complete()` exchanges the `challenge` carried by a `signIn()` that came back with
`status: "two-factor-required"` and finishes the login.

```ts
const enrollment = await client.twoFactor.enroll(); // { secret, uri, confirmed: false }
// ...the user scans `uri` and confirms with a code...
await client.twoFactor.confirm(code);

const result = await client.signIn(identifier, password);
if (result.status === "two-factor-required") {
  await client.twoFactor.complete(result.challenge, code);
}
```

| Method | Returns |
| :-- | :-- |
| `status()` | `{ enrolled, confirmed }` |
| `enroll()` | `{ secret, uri, confirmed }` — `uri` is the `otpauth://` URI for the QR code |
| `confirm(code)` | `{ confirmed }` |
| `disable(code)` | `{ disabled }` — requires a valid code, so a hijacked session cannot turn 2FA off |
| `complete(challenge, code)` | A `SignInResult` |

⚠️ **`enroll()` is not enough.** A factor that was enrolled but never confirmed does not protect
anything; `confirmed: false` is there so the UI can tell the user the setup is half done rather
than showing a green check.

## `magicLink()`

Passwordless login. `request()` answers the same way whether or not the account exists — do not
use it to infer whether an email has an account.

```ts
await client.magicLink.request(email);
// ...the user clicks the link in the email (it carries `email` and `token`)...
await client.magicLink.consume(email, token);
```

`request()` returns `{ sent, token? }`. ⚠️ **`token` is only populated in a deployment that does
not actually send mail** — it exists so a local environment can complete the flow without an
SMTP server. In production the field comes back empty, and an app that depends on it works in
development and breaks on deploy.

## `oauth()`

Login or account linking with an external provider. The callback returns JSON, not a redirect —
your app's return route handler exchanges it with `handleCallback()`.

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

Returning JSON instead of redirecting is what keeps the flow usable outside a browser and lets
your own router own the navigation. `created` is the flag that tells a first sign-in apart from
a returning user, which the session alone cannot tell you.

| Method | Purpose |
| :-- | :-- |
| `providers()` | Which providers the deployment has configured |
| `start(provider, redirectUri)` | `{ url, state }` — send the user to `url` |
| `handleCallback(provider, params)` | `{ result, created }` |
| `link(provider, redirectUri)` | Same as `start`, but attaches to the current account |
| `linked()` | The providers already attached |
| `unlink(provider)` | `{ unlinked }` |

⚠️ `unlink()` refuses to leave the account with no way in at all. Handle that rejection — it is
a [`DarwinError`](./errors.md), not a silent no-op.

## `passkey()` and `@hexcore-js/darwin-client/webauthn`

`passkey` is pure HTTP transport: the WebAuthn options travel as raw JSON without touching
`navigator.credentials`. For the complete browser flow, use the `/webauthn` subpath:

```ts
import { registerPasskey, authenticateWithPasskey } from "@hexcore-js/darwin-client/webauthn";

const summary = await registerPasskey(client.passkey, "my laptop");
const result = await authenticateWithPasskey(client.passkey, email);
```

`isWebAuthnSupported()` lets you check support before showing the button. Outside a browser
(Node, React Native) `registerPasskey`/`authenticateWithPasskey` throw
`WebAuthnUnavailableError` when invoked — importing the subpath never breaks a bundle that does
not use it.

The split is deliberate: `passkey` alone is testable and runnable anywhere, and the half that
can only exist in a browser is the half you opt into.

| `client.passkey` method | Purpose |
| :-- | :-- |
| `registerOptions()` / `register(credential, name?)` | The two halves of registration |
| `authenticateOptions(email?)` / `authenticate(credential)` | The two halves of authentication |
| `list()` | The account's passkeys, with `name`, `backedUp`, `lastUsedAt` |
| `remove(passkeyId)` | `{ deleted }` |

## `impersonate()`

An operator temporarily acts as another user, with a reason and an expiry audited server-side.

```ts
await client.impersonate.start(userId, "investigating a reported bug");
// ...the session store now reflects the impersonated subject...
await client.impersonate.stop();
// it does not touch the store on its own: with cookies you need a fresh signIn() to be the
// operator again; with bearer the tokens you already had stored are enough.
```

That asymmetry is not an oversight. With `CookieTransport` the operator's original cookies were
replaced by the impersonated ones, and this package cannot read `HttpOnly` cookies to put the
old pair back. With `BearerTransport` your storage still holds them. `status()` returns
`{ active, actorId, subjectId, reason, expiresAt }` so the UI can keep an unmistakable banner up
for as long as it lasts.

## `organization()`

Multi-tenancy with roles (`owner` > `admin` > `member`) and invitations.

```ts
const org = await client.organization.create("Acme");
const issued = await client.organization.invite(org.id, "new@acme.com", "admin");
// `issued.token` is single-use: in production build the link yourself and do not display it.
await client.organization.acceptInvitation(token);
```

| Method | Purpose |
| :-- | :-- |
| `create(...)` / `get(id)` / `update(...)` / `remove(id)` | The organization itself |
| `mine()` | The memberships of the current actor |
| `members(id)` / `setRole(id, userId, role)` / `removeMember(id, userId)` | Membership |
| `invite(...)` / `pendingInvitations(id)` / `revokeInvitation(...)` / `acceptInvitation(token)` | Invitations |

⚠️ **`invite()` returns the token so that you can build the link**, not so that you can show it.
Rendering it puts a single-use credential into the page, the logs and the browser history of
whoever is already signed in — which is not the person it was issued for.

## `rbac()`

Persisted, assignable roles and permissions. Needs a schema —
[`defineAccessControl`](../../hexcore/en/darwin/authorization.md) declares the resources,
actions and roles once, and every method below is typed against it.

```ts
const ac = defineAccessControl({
  resources: { invoice: ["read", "approve"] },
  roles: ["viewer", "accountant"],
});
const client = createDarwinClient({ baseUrl, transport, plugins: [rbac({ ac })] });

client.rbac.can("invoice", "approve");             // boolean, synchronous, no await
client.rbac.hasRole("accountant", { scope: "org:42" });
```

| Method | Purpose |
| :-- | :-- |
| `can(resource, action, opts?)` / `hasPermission(permission, opts?)` | Synchronous, against the loaded snapshot |
| `hasRole(role, opts?)` | Same, for role membership |
| `snapshot(opts?)` | The full state — for a list of roles, not just a boolean |
| `subscribe(listener)` | Fires on any change, in any loaded scope |
| `revalidate(opts?)` | Awaits a fresh fetch of a scope |
| `notifyAccessDenied(opts?)` | Tell the store you caught a server 403 for this scope, so it revalidates |
| `dehydrate()` / `hydrate(data)` | Carry state across an SSR render — never persisted to `localStorage` by default |

⚠️ **Everything here is optimistic for the UI.** The authority is always the server's
`AuthorizationEngine.decide()`, run on every real action — `can()` only avoids showing or hiding
a button while that real decision has not been asked yet. It fails **closed**: it never guesses
toward `true` without data, and a network error keeps the last known snapshot rather than
wiping it to empty.

## `drbac()`

Conditional policies and contextual role bindings on top of `rbac`. `requires: ["rbac"]` — the
two plugins do not import from each other, but `drbac()` needs `rbac()` registered alongside it.

```ts
const client = createDarwinClient({ baseUrl, transport, plugins: [rbac({ ac }), drbac({ ac })] });

client.drbac.evaluate("invoice", "approve", { attributes: { owner_id } });
// "allow" | "deny" | "unknown" — synchronous, never awaits anything

await client.drbac.check("invoice", "approve", { resourceId: invoiceId });
// boolean — authoritative, against the server
```

| Method | Purpose |
| :-- | :-- |
| `evaluate(resource, action, opts?)` | Synchronous and optimistic, against `GET /me/snapshot`'s `client_evaluable` rules |
| `check(resource, action, opts?)` | Authoritative `POST /check`, batched with any other `check()` from the same microtask |
| `checkMany(items)` | Same, for several items in one explicit request |
| `snapshot(opts?)` / `subscribe(listener)` / `revalidate(opts?)` | Same shape as `rbac()`'s |

Two deliberately different paths, same split as the server-side docstrings: `evaluate()` never
touches the network and answers `"unknown"` — never `"allow"` — for anything it cannot resolve
with the data already loaded; `check()`/`checkMany()` are the real
`AuthorizationEngine.decide()`, and are what should gate an actual mutation.

⚠️ **`evaluate()` can only see `client_evaluable` rules.** A rule whose condition uses a
`Predicate` is never sent to the client — the server forces it out, because the browser has no
way to run the same Python — so `evaluate()` answering `"unknown"` for something the server
would resolve is expected, not a bug. Gate the real action on `check()`, always.

---

Next: **[Errors](./errors.md)**.
