# The six bundled plugins

Each one has its own extra. You install the ones you use:

```sh
pip install 'hexcore[darwin-two-factor]'
```

Every plugin extra pulls in `hexcore[darwin]`, so that command brings the core it needs. What it
does **not** bring is a storage backend: you choose that, and it is covered in
[Storage](./storage.md).

Four of the six add no dependencies at all — they run on stdlib plus the core — and still have
their own extra: it is the stable name where a future dependency lands without changing your
install command, and it is the only place anyone reads before installing.

---

## `magic_link` — single-use link login

```python
from hexcore.darwin.plugins.magic_link import MagicLinkPlugin

MagicLinkPlugin(ttl=timedelta(minutes=15), rate_limit=(3, 900), audit_hook=False)
```

| Route | What it does |
| :-- | :-- |
| `POST /request` | Issues the link |
| `POST /consume` | Redeems it for a session |

**It contributes no table**: it reuses the core's `verification` table, which already models
exactly a single-use token.

> The default `rate_limit` limits by IP. Without it, `POST /request` is a free email amplifier
> against third parties.

The link's TTL is 15 minutes — short on purpose, because it is a bearer credential that travels by
email and ends up in the client's history and in the provider's logs.

`POST /request` answers the same whether the email exists or not: otherwise it would be an
enumeration oracle on an unauthenticated route.

---

## `two_factor` — TOTP with backup codes

```python
from hexcore.darwin.plugins.two_factor import TwoFactorPlugin

TwoFactorPlugin(issuer="My Product", challenge_ttl=..., rate_limit=(5, 300))
```

| Route | What it does |
| :-- | :-- |
| `GET ""` | Second-factor status |
| `POST /enroll` | Starts enrollment |
| `POST /confirm` | Confirms it |
| `POST /disable` | Turns it off |
| `POST /challenge` | Redeems the second-step challenge |

`issuer` is the name authenticator apps display: put your product's there.

Sign-in is split in two steps: a hook on `user.sign_in.authenticated` runs with the password
already validated and the session not yet created, and raises `TwoFactorRequiredError` without
issuing anything.

> **Do not turn off the `rate_limit`.** A TOTP is six digits: without a limit, the challenge is
> brute-forced.

The per-row ceiling (`MAX_FAILED_ATTEMPTS`) only protects an enrolled user — the per-IP limit is
what stops someone rotating across accounts.

RFC 6238 on stdlib `hmac`, without `pyotp`: it is about thirty lines — an HMAC, a thirty-second
counter and a truncation — and they do not justify a dependency on the authentication path. The
secret's encryption reuses `joserfc`'s JWE.

---

## `oauth` — Authorization Code + PKCE

```python
from hexcore.darwin.plugins.oauth import OAuthPlugin

OAuthPlugin(
    providers=[...],
    allowed_redirect_uris=["https://my-app.com/callback"],
    link_policy=...,
)
```

| Route | What it does |
| :-- | :-- |
| `GET /providers` | The configured ones |
| `GET /{provider}/start` | Starts the flow |
| `GET /{provider}/callback` | Completes it |
| `GET /{provider}/link` | Links to an existing account |
| `GET /linked` | The linked ones |
| `DELETE /{provider}` | Unlinks |

PKCE is mandatory (`S256`, never `plain`). It reuses the core's `account` table and contributes
one table of its own only for the in-flight `state`.

> **`allowed_redirect_uris` must be declared in production.** Without the list nothing is
> validated, and a free `redirect_uri` lets an attacker walk off with the victim's code.

The default `link_policy` **does not link by matching email** (`LinkPolicy.NEVER`), and that is
the correct default: linking by matching email lets a provider that does not verify emails take
over an existing account. It is the most common OAuth account takeover.

The HTTP client sits behind a port, so the flow's tests run without the extra.

---

## `impersonate` — "sign in as", audited

```python
from hexcore.darwin.plugins.impersonate import ImpersonatePlugin

ImpersonatePlugin(policy=ScopeImpersonationPolicy(), rate_limit=...)
```

| Route | What it does |
| :-- | :-- |
| `POST /{user_id}` | Starts |
| `POST /stop` | Ends |
| `GET ""` | Current status |

It adds neither dependencies nor tables: impersonation is a session with two principals, and the
core has distinguished them since the schema was designed — `actor` and `subject` are columns on
`session` — because an unauditable impersonation should not have been constructible even before
this plugin existed.

The invariants:

- The impersonated session has a **60-minute non-renewable ceiling** (the core rejects the
  refresh).
- **No chains**: impersonating while impersonating is forbidden.
- `has_scope` consults the **actor**, never the subject: impersonating does not lend permissions.

The `rate_limit` exists **even though the route is authenticated**: if a support account is
compromised, the limit turns "impersonate the entire user base" into something slow and noticeable.

---

## `passkey` — WebAuthn

```python
from hexcore.darwin.plugins.passkey import PasskeyPlugin

PasskeyPlugin(
    rp_id="my-app.com",
    rp_name="My App",
    origins=["https://my-app.com"],
    require_user_verification=True,
)
```

| Route | What it does |
| :-- | :-- |
| `POST /register/options` | Registration options |
| `POST /register` | Registers |
| `POST /authenticate/options` | Login options |
| `POST /authenticate` | Logs in |
| `GET ""` | The registered ones |
| `DELETE /{passkey_id}` | Deletes one |

What is stored is the public key: a database dump is useless for authenticating either here or
elsewhere, and the origin is bound by the browser.

> **Changing `rp_id` invalidates every existing passkey.** It is the Relying Party's domain,
> without scheme or port, and it is part of the credential.

`origins` is mandatory unless you bring your own verifier. `require_user_verification=True` is
what enables passwordless login, requiring a PIN or biometrics.

The signature counter is used to detect cloned authenticators: a counter that stopped advancing
(but used to) rejects the authentication and cuts the session.

It adds `py_webauthn`, and that is justified: WebAuthn is not thirty lines like TOTP — there is
CBOR, COSE keys, attestation formats and a signature counter. Writing it by hand would be
home-made cryptography on the authentication path.

---

## `organization` — organizations, members and invitations

```python
from hexcore.darwin.plugins.organization import OrganizationPlugin

OrganizationPlugin(invitation_ttl=timedelta(days=7), max_members=None)
```

| Route | What it does |
| :-- | :-- |
| `POST ""` / `GET ""` | Create / list |
| `GET`·`PATCH`·`DELETE /{organization_id}` | One organization |
| `GET /{organization_id}/members` | Members |
| `PATCH`·`DELETE /{organization_id}/members/{user_id}` | One member |
| `POST`·`GET /{organization_id}/invitations` | Invitations |
| `DELETE /{organization_id}/invitations/{invitation_id}` | Revokes one |
| `POST /invitations/accept` | Accepts |

Three roles: `owner` > `admin` > `member`. The three invariants it upholds:

1. **An organization is never left without an `owner`** — counted in the database, not in memory,
   so it survives concurrent requests.
2. **Nobody promotes anyone above themselves**, nor acts on a peer or a superior.
3. **The invitation is bound to the invitee's verified email**: without that, forwarding the link
   grants the role to whoever receives it.

No dependencies: what it needs is not packages but **atomic operations**, and those come from the
backend — a correlated `EXISTS` in the `WHERE` for SQL, embedded members for Mongo. That is what
makes removing the last owner unable to win a race condition.

---

## Wiring them up

```python
from hexcore.darwin import IdentityConfig, configure_identity
from hexcore.darwin.plugins.magic_link import MagicLinkPlugin
from hexcore.darwin.plugins.two_factor import TwoFactorPlugin

configure_identity(
    IdentityConfig(),
    plugins=[MagicLinkPlugin(), TwoFactorPlugin(issuer="My Product")],
)
```

And to mount their routes:

```python
from hexcore.darwin import build_identity_router, get_identity_container

plugins = get_identity_container().plugins

app = create_app(
    features=AppFeatures(auth_context=True, csrf=True),
    routers=[build_identity_router(), *plugins.routers()],
)
```

All of them accept `include_router=False` if you would rather expose the flows through your own
routes and use only the commands.

**If they contribute tables, remember `env.py`**: see [Storage](./storage.md).

---

## See also

- [Writing your own plugin](./writing-plugins.md)
- [Storage, schema and migrations](./storage.md)
