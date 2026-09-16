# HexCore — monorepo

[![PyPI](https://img.shields.io/pypi/v/hexcore?label=hexcore&color=blue)](https://pypi.org/project/hexcore/)
[![npm](https://img.shields.io/npm/v/@hexcore-js/darwin-client?label=%40hexcore-js%2Fdarwin-client&color=blue)](https://www.npmjs.com/package/@hexcore-js/darwin-client)
[![License](https://img.shields.io/github/license/Indroic/HexCore)](./LICENSE)

📖 **All documentation lives in [`docs/`](./docs/)**, in English and Spanish, for both packages.

## What this is

**HexCore is a Python library you install with `pip`.** It is not a project template, not a
code generator, and not a service you deploy — you `import` it from an application you own, and
you keep owning the application.

What it gives you is the layer most Python backends end up writing twice: the **abstractions**
of hexagonal architecture, DDD and CQRS (entities, repositories, unit of work, command and
query buses) *and* the **infrastructure** that actually makes them run — the SQL session layer,
the FastAPI application factory, the background worker runner, a cron you can edit without a
redeploy, authentication, and the testing doubles for all of it.

The design goal is that the happy path takes **zero configuration**: `create_app()` with no
arguments returns a usable app, `init_engine()` with no arguments returns a production-correct
engine. You pass arguments when you disagree with a default, not to get off the ground.

```python
# main.py — a complete HexCore app
from hexcore.fastapi import build_lifespan, create_app, SqlEngineStep

app = create_app(
    lifespan=build_lifespan(SqlEngineStep()),
    routers=[users_router, tickets_router],
)
```

Nothing heavy is pulled in by default. Everything beyond the core lives in **optional extras**
(`api`, `sql`, `redis`, `procrastinate`, `darwin-*`, …), and the modules that need one only
import it at the moment you use it — `import hexcore.cqrs` works with no extras installed at
all.

## The two packages

| | What it is | Documentation | Published on |
| :-- | :-- | :-- | :-- |
| 🐍 [`packages/hexcore/`](./packages/hexcore/) | The Python library described above. Python ≥ 3.12. | 🇬🇧 [EN](./docs/hexcore/en/) · 🇪🇸 [ES](./docs/hexcore/es/) | [PyPI: `hexcore`](https://pypi.org/project/hexcore/) |
| 🟦 [`packages/darwin-client/`](./packages/darwin-client/) | A TypeScript client for **Darwin**, the authentication module that ships inside the Python library. Node ≥ 20, zero runtime dependencies. | 🇬🇧 [EN](./docs/darwin-client/en/) · 🇪🇸 [ES](./docs/darwin-client/es/) | [npm: `@hexcore-js/darwin-client`](https://www.npmjs.com/package/@hexcore-js/darwin-client) |

### What Darwin is, precisely

Darwin is **not a separate product and not a separate service**. It is a module of the Python
library — `hexcore.darwin` — that you mount into your own FastAPI app to get registration,
email verification, sign-in, sessions with rotating refresh, revocation, audited impersonation,
and a plugin system for second factor, OAuth, magic links, passkeys and organizations.

It runs in your process, against your database, behind your domain. There is no HexCore server
anywhere.

### How the two packages relate

```
your FastAPI app  ──imports──▶  hexcore  (Python, PyPI)
      │                            └── hexcore.darwin  ──exposes──▶  HTTP routes under /auth
      │                                                                      ▲
      └── your frontend  ──imports──▶  @hexcore-js/darwin-client  ───────────┘
                                        (TypeScript, npm)
```

The client speaks Darwin's HTTP contract. It does **not** import anything from the Python
package, and the Python package does not know the client exists. They are coupled by the
contract in [`packages/darwin-client/openapi/`](./packages/darwin-client/openapi/), which is
dumped from the Python code and committed, so any drift between the two shows up in the diff of
a pull request.

**You do not need both.** Use the Python library on its own and authenticate however you like;
or, if you use Darwin and your frontend is TypeScript, the client saves you from hand-rolling
the rotating refresh, the double-submit CSRF and the transport choice — where a mistake in any
of the three is a security bug, not an inconvenience.

## Installation

The two packages are installed independently, from their own registries:

```sh
pip install hexcore                       # or: pip install "hexcore[api,sql,darwin-sqlalchemy]"
npm install @hexcore-js/darwin-client
```

Full extra-by-extra table in [installation](./docs/hexcore/en/installation.md) ·
[instalación](./docs/hexcore/es/instalacion.md).

## Documentation

All of the monorepo's documentation is consolidated in [`docs/`](./docs/):

```
docs/
├── README.md                  ← index: both packages, both languages
├── ARCHITECTURE_TYPING.md     ← the Python package's strict-typing contract
├── hexcore/
│   ├── en/   ·   es/          ← 20 guides per language, Darwin included
└── darwin-client/
    └── en/   ·   es/          ← 8 guides per language
```

Start at [installation](./docs/hexcore/en/installation.md) and
[quickstart](./docs/hexcore/en/quickstart.md) for the Python side, or at the
[Darwin Client quickstart](./docs/darwin-client/en/quickstart.md) for the frontend one.

**English is the reference version**; `es/` is its translation. If the two ever contradict each
other, English wins.

Each package's `README.md` stays complete and self-sufficient — they are what PyPI and npm
render — and links into `docs/` with absolute GitHub URLs, which are the only ones that resolve
outside the repository.

## Development

```bash
# Python
cd packages/hexcore && uv run pytest -q

# TypeScript
npm install
npm -w @hexcore-js/darwin-client run test
```

See [`CONTRIBUTING.md`](./CONTRIBUTING.md) for the full contribution flow, including the
`packages/` layout, the two independent release pipelines, and why.

## License

MIT © David Latosefki. See [`LICENSE`](./LICENSE).
