# Darwin — the identity module

Registration, email verification, sign-in, sessions with rotating refresh, revocation, audited
impersonation, and a plugin system adding second factor, OAuth, magic links, passkeys and
organizations **without the core knowing about them**. A port of
[Better Auth](https://github.com/better-auth/better-auth)'s architecture, schema and plugin
system to Python + CQRS.

```bash
pip install 'hexcore[darwin-sqlalchemy]'
```

192 symbols. Use `python scripts/hexcore_surface.py --find <name>` rather than guessing; the
groups are `IdentityConfig`/container, commands, context, entities and value objects, events,
exceptions, permissions, ports, API, infrastructure, plugins, SQL storage.

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
   previous generation is rejected without enumerating them.

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
    storage=None,                    # "sqlalchemy" | "beanie" | None (detects)
    trusted_origins=(),
    worker_context_ttl=timedelta(hours=24),
    require_verified_email=True,
    max_verification_attempts=5,
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
(compared normalised).

`configure_identity(config, **components)` accepts any port to inject: `users=`, `clock=`,
`key_store=`, `plugins=`, … It is what the tests use and what lets you persist keys in
production.

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

## The six bundled plugins

| Extra | Plugin | What it adds |
| :-- | :-- | :-- |
| `[darwin-magic-link]` | `magic_link` | Single-use link login |
| `[darwin-two-factor]` | `two_factor` | TOTP (RFC 6238) with backup codes |
| `[darwin-oauth]` | `oauth` | Authorization Code + PKCE |
| `[darwin-impersonate]` | `impersonate` | "Sign in as", audited |
| `[darwin-passkey]` | `passkey` | WebAuthn |
| `[darwin-organization]` | `organization` | Organizations, members, invitations |

Every plugin extra pulls in `hexcore[darwin]`, so installing one brings the core it needs.
Four of the six add no new dependencies today and still earn their extra: it is the stable
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
