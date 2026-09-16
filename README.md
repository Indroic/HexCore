# HexCore — monorepo

[![PyPI](https://img.shields.io/pypi/v/hexcore?label=hexcore&color=blue)](https://pypi.org/project/hexcore/)
[![npm](https://img.shields.io/npm/v/@hexcore-js/darwin-client?label=%40hexcore-js%2Fdarwin-client&color=blue)](https://www.npmjs.com/package/@hexcore-js/darwin-client)
[![License](https://img.shields.io/github/license/Indroic/HexCore)](./LICENSE)

📖 **All documentation lives in [`docs/`](./docs/)**, in English and Spanish, for both packages.

This repository is a monorepo with two packages:

| | What it is | Documentation | Published on |
| :-- | :-- | :-- | :-- |
| 🐍 [`packages/hexcore/`](./packages/hexcore/) | The Python meta-framework built on hexagonal architecture + DDD + CQRS | 🇬🇧 [EN](./docs/hexcore/en/) · 🇪🇸 [ES](./docs/hexcore/es/) | [PyPI: `hexcore`](https://pypi.org/project/hexcore/) |
| 🟦 [`packages/darwin-client/`](./packages/darwin-client/) | The framework-, runtime- and backend-agnostic TypeScript client for Darwin, the identity module of HexCore | 🇬🇧 [EN](./docs/darwin-client/en/) · 🇪🇸 [ES](./docs/darwin-client/es/) | [npm: `@hexcore-js/darwin-client`](https://www.npmjs.com/package/@hexcore-js/darwin-client) |

## Why a monorepo

Darwin only publishes its HTTP contract from the Python code: without a TypeScript client
versioned next to the server, every frontend hand-rolls the rotating refresh, the double-submit
CSRF and the choice of transport — and getting any of the three wrong is a security bug, not an
inconvenience. Keeping both packages in the same repository is what makes a CI gate possible
that fails when the client and the server drift apart.

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
