# Darwin — the identity module

Registration, email verification, sign-in, sessions with rotating refresh, revocation, audited
impersonation, and a plugin system adding second factor, OAuth, magic links, passkeys and
organizations **without the core knowing about them**. A port of
[Better Auth](https://github.com/better-auth/better-auth)'s architecture, schema and plugin
system to Python + CQRS.

```bash
pip install 'hexcore[darwin-sqlalchemy]'
```

214 symbols. Use `python scripts/hexcore_surface.py --find <name>` rather than guessing; the
groups are `IdentityConfig`/container, commands, context, entities and value objects, events,
exceptions, permissions, ports, API, infrastructure, plugins, SQL storage. `rbac`/`drbac`'s own
symbols are **not** in this facade — like every other plugin, they live under
`hexcore.darwin.plugins.rbac`/`.drbac` and are imported directly.

---

## Getting started

```python
from hexcore.darwin import (
    IdentityConfig,
    build_identity_router,
    configure_identity,
    identity_startup_steps,
)
from hexcore.fastapi import AppFeatures, SqlEngineStep, build_lifespan, create_app

configure_identity(IdentityConfig())          # once, at startup

app = create_app(
    features=AppFeatures(auth_context=True, csrf=True),
    lifespan=build_lifespan(SqlEngineStep(), *identity_startup_steps()),
    routers=[build_identity_router()],
)
```

That mounts eight routes under `/auth`: `POST /sign-up`, `/verify-email`, `/sign-in`,
`/refresh`, `/sign-out`, `/sign-out-everywhere`, and `GET /me`, `/sessions`.

`identity_startup_steps()` returns `IdentityStep` — validates configuration, resolves the
storage backend, brings up the signing keys — and `SessionReaperStep`, which purges expired
sessions in the background.

> ⚠️ **`POST /sign-up` is an enumeration oracle if exposed publicly as-is**: it answers 409
> when the email already exists. It serves the administrative case; the public one is better
> written in your app, where the response is always the same and the difference goes into the
> email that gets sent.

---

## The four decisions that change how you integrate it

### Actor vs subject

The session persists `actor_user_id` **and** `subject_user_id`, not a single `user_id`.
`AuthContext` exposes `actor` (who is executing) and `subject` (who is affected). That is what
makes impersonation auditable: an impersonated context without both principals **cannot be
built** — model validation rejects it. Outside impersonation they are the same user.

If you write logic asking "who is the user", choose deliberately. It is almost always
`subject` for data permissions and `actor` for auditing.

### Three-layer revocation, zero DB in the hot path

The access token is a JWT with a short `exp`, and **the database is not touched to validate
it** — only signature, `exp`, audience and transport.

1. Short `exp`: stolen means stolen for a short time.
2. A `sid` denylist in `ICache`: `SignOut` blocks the session, and a still-valid token is
   rejected without waiting for expiry.
3. A per-user generation counter: `SignOutEverywhere` increments it and every token from the
   previous generation is rejected without enumerating them. `GenerationGuard` enforces it and
   caches the counter for 60 s; `revoke_all_for` drops that entry in the same flow, so the cut
   is immediate.

The refresh token **does** hit the database: it rotates the session atomically and detects
reuse. A stolen refresh token revokes the entire family on the first attempt.

The denylist fails **closed** (`on_cache_error="deny"`), the opposite of the framework's
`rate_limit` and deliberately so: a downed cache cannot become "everybody gets in".

### The algorithm is pinned, never the token's `alg`

`joserfc` over `pyjwt` precisely because its API **forces** you to pass the list of allowed
algorithms: the safe default is structural, not documentary. Algorithm confusion is the most
repeated family of JWT bugs, and this makes it impossible by construction.

### The transport is bound to the token

Cookie and Bearer issue tokens with different `aud`/`tt`, so **a cookie cannot be replayed as
a Bearer token** to bypass CSRF and `SameSite`. One endpoint per operation serves both: the
web client receives `Set-Cookie` and no tokens in the body, the native client the reverse.
Duplicating the routes would duplicate the security checks, and the copy that forgets one is
the one that gets exploited.

Cookies: `__Host-` + `HttpOnly` + `Secure` + `SameSite=Lax`, plus an explicit anti-CSRF check.

---

## The actor crosses the queue

When you enqueue a command during an authenticated request, the actor travels in a **signed
envelope bound to the message** (`cid`, `mt`). Without that binding, a grant captured from a
"delete account" could be re-attached to a "transfer funds".

The worker **re-validates the `session` row** instead of trusting the `exp`: a token valid at
enqueue time may be revoked by the time the worker processes it.
`IdentityConfig.worker_context_ttl` (24 h by default) bounds the window.

---

## Configuration

```python
IdentityConfig(
    secret_key=...,                  # SecretStr | None
    tokens=TokenConfig(...),
    cookies=CookieConfig(...),
    passwords=PasswordPolicy(...),
    user_model=None,                 # your class, if you compose UserMixin
    session_model=None,              # …and the other four, only to break a tie
    storage=None,                    # "sqlalchemy" | "beanie" | None (detects)
    trusted_origins=(),
    worker_context_ttl=timedelta(hours=24),
    require_verified_email=True,
    max_verification_attempts=5,
    usernames=None,                  # UsernamePolicy(), to enable usernames
    require_email=True,
    sign_in_identifiers=("email",),  # or ("email", "username")
)
```

⚠️ **The signing key does not live in `ServerConfig`.** Every `ServerConfig` field has a
default, and a signing secret with a default is the worst thing an auth library can ship —
half the deployments would sign with the same example value. It is `IdentityConfig.secret_key`,
a `SecretStr` with **no default**, read from `HEXCORE_DARWIN_SECRET_KEY`. In production it
**fails if there is no key**.

```bash
export HEXCORE_DARWIN_SECRET_KEY="$(hexcore identity generate-secret)"
```

`TokenConfig`: `issuer` (`"hexcore"`), `access_ttl` (**2 minutes** — short on purpose, it is
what bounds a stolen access token), `refresh_ttl` (30 days, rotates on every use),
`session_ttl` (90 days absolute ceiling), `algorithm` (`"Ed25519"`), `leeway` (30 s).

`CookieConfig`: `access_name`/`refresh_name`/`csrf_name` (`session`/`refresh`/`csrf`, with the
`__Host-` prefix), `secure`, `http_only`, `same_site` (`"lax"`), `path` (`"/"` — `__Host-`
requires exactly this).

`PasswordPolicy`: `min_length` 12 (length over composition, what NIST recommends),
`max_length` 1024 (a ceiling exists because hashing 10 MB is free DoS), `denylist`
(compared normalised), `acknowledge_weak_minimum` (required to go below 8).

`UsernamePolicy`: `min_length` 3, `max_length` 32, `pattern` (applied to the normalised value;
**rejects `@`**, so a username cannot look like an email), `case_sensitive` False, `reserved`.

**Username sign-in, and the email as an optional identifier (10.0).** `User.email` is
`str | None`. Three settings control it: `usernames` enables the field, `require_email` decides
whether sign-up demands an address, and `sign_in_identifiers` decides what you can log in with —
having usernames and accepting them as a credential are separate decisions. `IdentityConfig`
refuses to build on any combination where nobody could sign in.

`sign_in()` takes `identifier=` (`email=` still resolves and warns, removed in 11.0), and the
HTTP body accepts `identifier`, `email` or `username` — one of them — so a 9.x front end keeps
working. `require_verified_email` is a **no-op for an account with no email**; without that, a
username-only account could never sign in.

⚠️ An account with no email cannot recover its password or verify anything. Both flows send a
code somewhere.

`configure_identity(config, **components)` accepts any port to inject: `users=`, `clock=`,
`key_store=`, `principals=`, `plugins=`, … It is what the tests use and what lets you plug in
your application's permissions.

**Roles, scopes and account status come from `AbstractPrincipalResolver`**, which returns a
`ResolvedPrincipal(roles, scopes, status)`. The default returns an empty one. It is consulted on
sign-in and on every refresh rotation, and all three travel inside the token, so `authenticate`
stays DB-free. A change lands on the next rotation (one `access_ttl`); for an immediate cut use
`revoke_all_for`. This is the flat, code-owned path. For roles and permissions an admin can
create and assign through an API — with anti-escalation, per-tenant scoping and conditional
rules — see **"rbac and drbac"** below.

`status` is a free-form `str` that **Darwin never interprets** — it only carries it to
`auth.actor.status`. It exists because Darwin knows only `is_active`/`locked_until` and checks
them only on rotation, which left apps with their own state machine either querying the database
per request or mirroring their status into those two fields forever. To reject a status outright,
raise an `IdentityError` from `resolve()`; on a rotation that runs after the session row is
consumed, so the user ends up signed out.

**Concrete models resolve themselves.** Declare `class UserModel(UserMixin, Base)` with
`__tablename__ = "darwin_user"` and Darwin finds it — it never imports its own `models.py` when
one of yours already occupies the table. That import declares all six defaults at once, and the
collision surfaces as `InvalidRequestError: Table 'darwin_user' is already defined`, thrown at
the first repository call rather than at the declaration. The `*_model` config fields only break
ties.

**The key store is persisted.** `KeyStore` reads `darwin_jwks` per backend; seed it with
`hexcore identity generate-keys --persist`. `IdentityStep` refuses to start with `debug=False`
when the key store is ephemeral or the cache is the default `MemoryCache` — both are per-process
and break silently as soon as there is a second worker.

---

## Storage and Alembic {#alembic}

Backends: `"sqlalchemy"`, `"beanie"`, or detection from what is installed. A deployment picks
**one**, which is why they are separate extras — whoever picks Mongo has no reason to install
SQLAlchemy, Alembic and asyncpg.

⚠️ **This is the module's most important warning.** `env.py` needs a third call, alongside the
two from `core.md`:

```python
ensure_framework_models_loaded()      # the framework's tables

DARWIN_PLUGINS: list[str] = []        # fill this in with the plugins you use
ensure_identity_schema_loaded(plugins=DARWIN_PLUGINS)

import_all_models(models)             # yours, recursively
```

A table that exists in the database and is missing from `Base.metadata` gets an
`op.drop_table` in the next autogenerated migration, in a migration that generates cleanly.
**With Darwin, the table that gets dropped is the entire credential store.**

The safety net, for a pre-commit hook or CI:

```bash
hexcore identity check-schema     # exits 1 if any identity table is missing from Base.metadata
```

Development shortcuts — `hexcore identity create-tables` is idempotent but versions nothing,
so a later schema change has nowhere to migrate from. Use Alembic in production.

On Mongo the hole is the same with a different symptom: every document must go in **one**
`init_beanie` call (see `core.md`).

Your own user model: compose `UserMixin`, pass it as `IdentityConfig.user_model`, and it is
validated at configure time. `validate_user_model` is the check.

---

## The CLI

```bash
hexcore identity generate-secret
hexcore identity generate-keys --algorithm Ed25519 --kid 2026-01
hexcore identity create-tables
hexcore identity check-schema
hexcore identity plugins myapp.identity
```

`generate-keys` emits the signing key pair as JWK on stdout, ready to redirect into a secret
manager. ⚠️ **The private key comes out in the clear** — the warning goes to **stderr**
precisely so it does not pollute what you redirect. Both JWKs are emitted parsed, not as the
string `SigningKey` stores: a secret manager receiving JSON with a JSON string inside forces a
double parse, and that is the step somebody works around by pasting the key into a file.

`plugins` reads a module exposing `plugins: PluginRegistry` or `PLUGINS: list[DarwinPlugin]`
and lists what each contributes — routes, commands, hooks, tables. It is how you get the exact
list that belongs in `DARWIN_PLUGINS`.

---

## The eight bundled plugins

| Extra | Plugin | What it adds |
| :-- | :-- | :-- |
| `[darwin-magic-link]` | `magic_link` | Single-use link login |
| `[darwin-two-factor]` | `two_factor` | TOTP (RFC 6238) with backup codes |
| `[darwin-oauth]` | `oauth` | Authorization Code + PKCE |
| `[darwin-impersonate]` | `impersonate` | "Sign in as", audited |
| `[darwin-passkey]` | `passkey` | WebAuthn |
| `[darwin-organization]` | `organization` | Organizations, members, invitations |
| `[darwin-rbac]` | `rbac` | Persisted, assignable roles and permissions |
| `[darwin-drbac]` | `drbac` | Conditional policies + contextual role bindings, on top of `rbac` |

Every plugin extra pulls in `hexcore[darwin]`, so installing one brings the core it needs.
Six of the eight add no new dependencies today and still earn their extra: it is the stable
name where a future dependency lands (`[darwin-passkey]` did not have `webauthn` until it
did), it makes the install command work, and it documents the surface where consumers look.

Storage is deliberately **not** required by a plugin extra: "one of two" cannot be expressed
in packaging metadata, and an extra with both would install SQLAlchemy for the person who
chose Mongo. The choice is resolved at runtime, with an error naming the missing extra.

---

## Writing a plugin

The extension points are routes, commands, hooks and tables. Hooks are the one you will
actually use: they bind to action names, support wildcards and ordering, and can
**short-circuit** — answering without running the handler — via `ShortCircuit`.

⚠️ **The trap: your exception has to be an `IdentityError`.** Anything else escapes the
module's exception mapping and surfaces as a 500 instead of the status you meant.

⚠️ **If your plugin stores things**, declare them in `tables()` / `contributed_tables`, and
put the plugin in the `DARWIN_PLUGINS` of your `env.py`. Same `op.drop_table` failure mode as
everything else in this file.

`hexcore identity plugins <module>` prints what a registry contributes, which is the fastest
way to check a plugin is wired as you think.

---

## `AuthorizationEngine`, `rbac` and `drbac`

Answers "can this actor do X to this resource", not just "does this actor have this scope".
`AuthorizationProvider.decide()` returns `allow`/`deny`/`not_applicable`; `AuthorizationEngine`
combines every registered one with **deny-overrides + default-deny**: any `deny` wins, else the
first `allow` wins, else (including zero providers) it denies. A provider that raises is treated
as `deny` and logged.

```python
from hexcore.darwin.infrastructure.api.authorization import require_permission

@router.post("/invoices/{id}/approve",
             dependencies=[Depends(require_permission("invoice.approve", resource=load_ref))])
```

`authorize_command(action, resource_from=...)` for CQRS; `await authorize(action, resource)` for
imperative code inside a handler, using `require_auth()`'s actor. `AccessDeniedError` (403)
carries only `required` — never the reason a policy lost.

**`rbac`** — persisted, assignable roles. `RoleRegistry` code roles seed as `is_system=True`,
not editable by API. Every table has `scope_key: str`, never `NULL` (`""` = global);
`RbacAuthorizationProvider` does **not** walk scope hierarchy — that is `drbac`'s job.
Anti-escalation everywhere: nobody grants more than they hold effectively in that scope, except
`assign_role(actor_id=None, ...)`, the sanctioned bootstrap bypass, always audited as
`granted_by=None`. Every mutation bumps `darwin_authz_version` for its scope; the permission
cache's key carries the version, so revocation needs no cache invalidation — the old key is
simply never asked for again.

**`drbac`** — conditional policies + contextual role bindings **on top of** `rbac`
(`requires = ("rbac",)`), but **no module imports anything from `rbac`** — `requires` only
orders registration, validated by name. A `RoleBinding.role_name` is a bare string, not a FK;
the one bridge is a `role_permissions` callable you wire yourself, same pattern as `rbac`'s own
`organization` integration. Resolves the full ancestor chain of a `scope_path`
(`"org:1"` covers `"org:1/proj:2"`). Conditions are a declarative AST (`Eq`, `And`,
`WithinScope`, ...) — no `eval`, `Var` reads only `subject.*`/`resource.*`/`env.*`, checked when
built. Evaluation is three-valued (Kleene logic, like SQL `NULL`): `true`/`false` decide, an
unresolved `Var` or an unregistered `Predicate` is indeterminate, never guessed toward `allow`.
Decision order: clear `deny` > clear `allow` > any indeterminate (fail-closed deny) >
`not_applicable`. `POST /check` is the real decision (batched, ≤ 50, deduplicated);
`GET /me/snapshot` only ever returns `client_evaluable` rules — never one with a `Predicate` in
it, forced out server-side regardless of what was declared.

⚠️ **`WithinScope` and scope-chain resolution compare by path segment, never by raw string
prefix.** `"org:4"` is not an ancestor of `"org:42/..."` just because the string is a prefix of
it — that mistake is a cross-tenant privilege escalation, not a cosmetic bug.

⚠️ **`ConditionTooComplexError`/`InvalidVarPathError` (422) are enforced when a policy is
saved**, not only when it is evaluated — a policy is written once and evaluated on every
matching request.

Client side: `client.rbac.can(resource, action, opts?)` / `hasRole(...)` are synchronous and
optimistic against a cached snapshot; `client.drbac.evaluate(...)` is the same idea, returning
`"allow" | "deny" | "unknown"`. `client.drbac.check(...)`/`checkMany(...)` are the authoritative,
batched calls against `POST /check`. Full detail: `references/darwin-client.md`.

Full guide with the threat model: `docs/hexcore/en/darwin/authorization.md`.

---

## Testing identity

```python
from hexcore.darwin.testing import (
    FakeSessionRepository,
    FakeUserRepository,
    PlainTextHasher,
    authenticated_context,
    configure_test_identity,
    create_test_user,
)
```

See `testing.md`.

---

## The client side

Darwin ships an official TypeScript client in this same repository,
`@hexcore-js/darwin-client`, for browsers, SSR and native apps. It is a separate package with
its own release train, and **neither side validates the other** -- three things have to agree
across them, and none of the three raises where the mistake is:

1. **The CSRF cookie name.** `CookieConfig.csrf_name` here against `csrfCookieName` there.
   The defaults agree, and the client tries the `__Host-` prefixed form first so `secure`
   does not have to be mirrored — but a custom `csrf_name` does, and a mismatch answers 403
   on every write while every read keeps working.
2. **CORS and `trusted_origins`.** A mistake here reaches the browser as a network error, not
   as a 403 -- there is no status for the client to read.
3. **Which plugins are enabled.** A plugin registered only on the client answers 404.

Full detail, with the client's own traps: `references/darwin-client.md`.
