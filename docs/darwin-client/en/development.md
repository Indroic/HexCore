# Development

The client is a workspace of the [HexCore monorepo](https://github.com/Indroic/HexCore). All
commands run from the repository root.

```bash
npm install
npm -w @hexcore-js/darwin-client run gen        # regenerates src/generated/ from openapi/
npm -w @hexcore-js/darwin-client run typecheck
npm -w @hexcore-js/darwin-client run test
npm -w @hexcore-js/darwin-client run build
```

| Script | Does |
| :-- | :-- |
| `gen` | `gen:schema` + `gen:errors` |
| `gen:schema` | `openapi-typescript openapi/darwin.openapi.json -o src/generated/schema.d.ts` |
| `gen:errors` | Builds `src/generated/error-codes.ts` from `openapi/darwin.errors.json` |
| `gen:errors:check` | The same, in check mode — this is what CI runs |
| `typecheck` | `tsc --noEmit`, strict, including `noUncheckedIndexedAccess` and `exactOptionalPropertyTypes` |
| `test` | `vitest run` |
| `build` | `tsup` — dual ESM + CJS with `.d.ts`, three entry points |
| `lint` / `lint:fix` | Biome |

## Where `openapi/` comes from

`openapi/` is dumped from the Python package with:

```bash
cd packages/hexcore
uv run python scripts/darwin_openapi.py --write
```

and it is **committed to git**. That is the point: the drift between the contract and the client
shows up in the diff of the pull request, rather than at runtime in somebody's app. A server
change that adds a field, renames a route or introduces an error code arrives as a visible
change to `openapi/darwin.openapi.json` and to the generated files under `src/generated/`.

⚠️ **Never edit anything under `src/generated/` by hand.** It is excluded from Biome's lint and
format on purpose, and `gen:errors:check` fails CI when it has drifted from the OpenAPI
document. Change the Python side, re-dump, and regenerate.

## Releases

The client has its own release pipeline, independent from the Python package:

| | |
| :-- | :-- |
| Tag format | `darwin-client-v$version` |
| Config | `packages/darwin-client/.cz.toml` |
| Changelog | `packages/darwin-client/CHANGELOG.md` |
| Publishes to | npm, via `.github/workflows/publish-darwin-client-to-npm.yml` |

`commitizen` computes the version from the conventional commits on every push to `master` and
regenerates the changelog. **No pull request edits `CHANGELOG.md`** — if a change deserves
prose, that prose goes in the commit body, which is what gets published.

Neither `cz` config filters commits by the paths they touched, so a `feat:`/`fix:` commit that
only touched one package still counts toward both versions. This is a known and accepted
limitation, not a bug to fix by adding path scoping. See
[`CONTRIBUTING.md`](https://github.com/Indroic/HexCore/blob/master/CONTRIBUTING.md) for the full
reasoning.

## Repository layout

```
packages/darwin-client/
├── src/
│   ├── core/          # client, fetcher, errors
│   ├── session/       # store, refresh controller
│   ├── transport/     # bearer, cookie, storages, cookie jars
│   ├── plugins/       # the six bundled plugins
│   ├── generated/     # ⚠️ generated — do not edit
│   ├── index.ts       # the main entry point
│   ├── webauthn.ts    # the /webauthn subpath
│   └── store.ts       # the /store subpath
├── openapi/           # dumped from the Python package, committed
├── scripts/           # gen-error-codes.mjs
└── test/
```

---

Back to the **[index](./README.md)**.
