# Darwin — the identity module

Registration, email verification, sign-in, sessions with rotating refresh, revocation, audited
impersonation, and a plugin system that adds second factor, OAuth, magic links, passkeys and
organizations **without the core knowing about them**.

A port of the architecture, schema and plugin system of
[Better Auth](https://github.com/better-auth/better-auth) to Python + CQRS.

```sh
pip install 'hexcore[darwin-sqlalchemy]'
```

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

That mounts eight routes under `/auth`:

`POST /sign-up` · `POST /verify-email` · `POST /sign-in` · `POST /refresh` · `POST /sign-out` ·
`POST /sign-out-everywhere` · `GET /me` · `GET /sessions`

`identity_startup_steps()` returns the steps to unpack into `build_lifespan`: `IdentityStep` —
which validates configuration, resolves the storage backend and brings up the signing keys — and
`SessionReaperStep`, which purges expired sessions in the background.

> ⚠️ **`POST /sign-up` is an enumeration oracle if you expose it publicly as-is**: it answers 409
> when the email already exists. It serves the administrative case; the public one is better
> written in your app, where the response is always the same and the difference goes into the
> email that gets sent.

---

## Index

| Document | Covers |
| :-- | :-- |
| [Storage](./storage.md) | Backends, schema, Alembic, `init_beanie`, your own user model |
| [Bundled plugins](./bundled-plugins.md) | All six, with their routes and warnings |
| [Writing your own plugin](./writing-plugins.md) | The extension points, hooks, and the traps |

← Back to the [documentation index](../).

---

## The decisions worth knowing

What follows is not trivia: these are the four things that change how you integrate the module.

### Actor vs subject

The session persists `actor_user_id` **and** `subject_user_id`, not a single `user_id`.
`AuthContext` exposes `actor` (who is executing) and `subject` (who is affected).

That is what makes impersonation auditable: an impersonated context without both principals
**cannot be built**, because model validation rejects it. Outside an impersonation, actor and
subject are the same user.

If you write logic that asks "who is the user", choose deliberately which of the two you mean. It
is almost always `subject` for data permissions and `actor` for auditing.

### Three-layer revocation, zero DB in the hot path

The access token is a JWT with a short `exp`. **The database is not touched to validate it** —
only signature, `exp`, audience and transport.

1. Short `exp`: if it is stolen, it is stolen for a short time.
2. A `sid` denylist in `ICache`: `SignOut` blocks the session and a still-valid token is rejected
   without waiting for expiry.
3. A per-user generation counter: `SignOutEverywhere` increments it and every token from the
   previous generation is rejected without enumerating them. `GenerationGuard` checks it,
   caching the counter for 60 s so the hot path does not query the database on every request;
   `revoke_all_for` drops that entry in the same flow, so the cut is immediate rather than
   "within a minute".

The refresh token **does** hit the database: it rotates the session atomically and detects reuse.
A stolen refresh token revokes the entire family on the first attempt.

The denylist fails **closed** (`on_cache_error="deny"`), the opposite of the framework's
`rate_limit` and deliberately so: a downed cache cannot be allowed to become "everybody gets
in".

### The algorithm is pinned, never the token's `alg`

`joserfc` over `pyjwt` precisely because its API **forces** you to pass the list of allowed
algorithms: the safe default is structural, not documentary. Algorithm confusion is the most
repeated family of JWT bugs, and this choice makes it impossible by construction.

### The transport is bound to the token

Cookie and Bearer issue tokens with different `aud`/`tt`, so **a cookie cannot be replayed as a
Bearer token** to bypass CSRF and `SameSite`.

A single endpoint per operation serves both transports: the web client receives `Set-Cookie` and
no tokens in the body; the native client receives the tokens in the body and no `Set-Cookie`.
Duplicating the routes would duplicate the security checks too, and the copy that forgets one is
the one that gets exploited.

Cookies: `__Host-` + `HttpOnly` + `Secure` + `SameSite=Lax`, plus an explicit anti-CSRF check.

The `sign_in_rate_limit` default uses `on_backend_error="deny"` — the opposite of the framework's
`rate_limit` default, and on purpose: a downed Redis should not turn into unlimited credential
stuffing.

---

## The actor crosses the queue

When you enqueue a command during an authenticated request, the actor travels in a **signed
envelope** bound to the message (`cid`, `mt`). Without that binding, a grant captured from a
"delete account" could be re-attached to a "transfer funds".

The worker **re-validates the `session` row** instead of trusting the `exp`: a token that was
valid at enqueue time may be revoked by the time the worker processes it.
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
    session_model=None,              # same for the other five tables
    storage=None,                    # "sqlalchemy" | "beanie" | None (detects)
    trusted_origins=(),
    worker_context_ttl=timedelta(hours=24),
    require_verified_email=True,
    max_verification_attempts=5,
)
```

| Field | Default | What it does |
| :-- | :-- | :-- |
| `secret_key` | `None` | The signing key. **No default**: read from `HEXCORE_DARWIN_SECRET_KEY` |
| `user_model` | `None` | Your class, if you compose `UserMixin`. Validated at configure time |
| `session_model`, `account_model`, `verification_model`, `audit_model`, `jwks_model` | `None` | Same for the other five tables. **Rarely needed**: if you declare your models, Darwin finds them (see below) |
| `storage` | `None` | `"sqlalchemy"`, `"beanie"`, or detection |
| `trusted_origins` | `()` | Valid origins for the anti-CSRF check |
| `worker_context_ttl` | 24 h | Window in which the actor envelope remains redeemable |
| `require_verified_email` | `True` | Whether sign-in requires a verified email. **Does not apply to an account with no email**: requiring it there would be a permanent 403 |
| `usernames` | `None` | The `UsernamePolicy`, or `None` if this app has no usernames |
| `require_email` | `True` | Whether sign-up requires an email address |
| `sign_in_identifiers` | `("email",)` | What you can sign in with: `"email"`, `"username"`, or both |
| `max_verification_attempts` | `5` | Attempts per verification token before invalidating it |

`TokenConfig`:

| Field | Default | Note |
| :-- | :-- | :-- |
| `issuer` | `"hexcore"` | The JWT's `iss` |
| `access_ttl` | **2 minutes** | Short on purpose: it is what bounds a stolen access token |
| `refresh_ttl` | 30 days | The refresh token rotates on every use |
| `session_ttl` | 90 days | Absolute session ceiling, however much it rotates |
| `algorithm` | `"Ed25519"` | Pinned by allowlist, never by the token's `alg` |
| `leeway` | 30 s | Clock tolerance between nodes |

`CookieConfig`:

| Field | Default | Note |
| :-- | :-- | :-- |
| `access_name` / `refresh_name` / `csrf_name` | `session` / `refresh` / `csrf` | They get the `__Host-` prefix |
| `secure` | `True` | — |
| `http_only` | `True` | JS cannot read the token |
| `same_site` | `"lax"` | — |
| `path` | `"/"` | `__Host-` requires exactly this |

`PasswordPolicy`:

| Field | Default | Note |
| :-- | :-- | :-- |
| `min_length` | `12` | Length over composition: what NIST recommends |
| `max_length` | `1024` | A ceiling exists because hashing 10 MB is free DoS |
| `denylist` | `frozenset()` | Forbidden passwords, compared normalized |

In production it **fails if there is no signing key**, and that is deliberate. To generate one:
`hexcore identity generate-secret`.

`configure_identity(config, **components)` accepts any port to inject: `users=`, `clock=`,
`key_store=`, `principals=`, `plugins=`, … It is what the tests use and what lets you plug in
your application's permissions.

---

## Your own concrete models

If you declare your own models on Darwin's tables — the recommended path, and the only one that
lets you add columns to them — **no configuration is needed**:

```python
from hexcore.darwin import UserMixin
from hexcore.sql import Base

class UserModel(UserMixin, Base):
    __tablename__ = "darwin_user"
    plan: Mapped[str] = mapped_column(String(32), default="free")
```

Darwin resolves each table's concrete class in three steps: what you declared in
`IdentityConfig`, then the mapped class composing the matching mixin, and only if there is none,
the one from its own `models.py`.

⚠️ **That order is what prevents a broken startup.** Importing `models.py` to obtain *one* class
runs the whole module, which declares **all six** on `Base`. With your `UserModel` already
declared on `darwin_user`, that is two classes fighting over the same table, and SQLAlchemy
fails with `InvalidRequestError: Table 'darwin_user' is already defined for this MetaData
instance` — on the first use of any repository, so the traceback points at a session query
rather than at the import that caused it. The `*_model` fields on `IdentityConfig` exist only to
break a tie when you map two classes onto the same mixin in different tables.

---

## Roles and scopes

`Principal` carries `roles` and `scopes`, and where they come from is your application's
decision, expressed as a port:

```python
from hexcore.darwin import AbstractPrincipalResolver, configure_identity

class AppRoles(AbstractPrincipalResolver):
    async def resolve(self, user):
        async with uow_scope() as uow:
            row = await uow.memberships.get_by_user(user.id)
        return frozenset(row.roles), frozenset(row.permissions)

configure_identity(IdentityConfig(), principals=AppRoles())
```

The default is `NullPrincipalResolver`, which returns two empty sets: an application that
declares no permissions does not start receiving them because it upgraded.

**It is consulted on sign-in and on every refresh rotation**, that is every `access_ttl`
(2 minutes) while the session is alive. Re-resolving is what makes revoking someone's role take
effect without waiting for them to sign out — the cut lands on the next rotation. If you need it
immediate, the tool is `revoke_all_for`, which bumps the generation and kills every token for
that user at once.

Both travel inside the token, not in the database: `authenticate` is the hot path and does not
query.

---

## Without extras

`import hexcore.darwin` **does not pull in** joserfc, argon2 or sqlalchemy: the facade resolves
lazily and only imports the submodule of the symbol you ask for. There are tests that verify this
by blocking the packages in `sys.meta_path`.

---

## See also

- [`docs/ARCHITECTURE_TYPING.md`](../../../ARCHITECTURE_TYPING.md) — the framework's type system
- [Project README](../../../../packages/hexcore/README.md) — the rest of HexCore
