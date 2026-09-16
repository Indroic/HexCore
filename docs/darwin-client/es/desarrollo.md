# Desarrollo

El cliente es un workspace del [monorepo de HexCore](https://github.com/Indroic/HexCore). Todos
los comandos corren desde la raíz del repositorio.

```bash
npm install
npm -w @hexcore-js/darwin-client run gen        # regenera src/generated/ desde openapi/
npm -w @hexcore-js/darwin-client run typecheck
npm -w @hexcore-js/darwin-client run test
npm -w @hexcore-js/darwin-client run build
```

| Script | Hace |
| :-- | :-- |
| `gen` | `gen:schema` + `gen:errors` |
| `gen:schema` | `openapi-typescript openapi/darwin.openapi.json -o src/generated/schema.d.ts` |
| `gen:errors` | Arma `src/generated/error-codes.ts` desde `openapi/darwin.errors.json` |
| `gen:errors:check` | Lo mismo, en modo chequeo — es lo que corre la CI |
| `typecheck` | `tsc --noEmit`, strict, con `noUncheckedIndexedAccess` y `exactOptionalPropertyTypes` |
| `test` | `vitest run` |
| `build` | `tsup` — ESM + CJS dual con `.d.ts`, tres puntos de entrada |
| `lint` / `lint:fix` | Biome |

## De dónde sale `openapi/`

`openapi/` se vuelca desde el paquete Python con:

```bash
cd packages/hexcore
uv run python scripts/darwin_openapi.py --write
```

y se **versiona en git**. Ese es el punto: el drift entre el contrato y el cliente aparece en el
diff del pull request, en vez de en runtime en la app de alguien. Un cambio del servidor que
agrega un campo, renombra una ruta o introduce un código de error llega como un cambio visible en
`openapi/darwin.openapi.json` y en los archivos generados de `src/generated/`.

⚠️ **Nunca edites nada de `src/generated/` a mano.** Está excluido del lint y del formato de
Biome a propósito, y `gen:errors:check` rompe la CI cuando se desalineó del documento OpenAPI.
Cambiá el lado Python, volcá de nuevo, y regenerá.

## Releases

El cliente tiene su propio pipeline de release, independiente del paquete Python:

| | |
| :-- | :-- |
| Formato de tag | `darwin-client-v$version` |
| Config | `packages/darwin-client/.cz.toml` |
| Changelog | `packages/darwin-client/CHANGELOG.md` |
| Publica en | npm, vía `.github/workflows/publish-darwin-client-to-npm.yml` |

`commitizen` calcula la versión desde los commits convencionales en cada push a `master` y
regenera el changelog. **Ningún pull request edita `CHANGELOG.md`** — si un cambio merece prosa,
esa prosa va en el cuerpo del commit, que es lo que se publica.

Ninguna de las dos configs de `cz` filtra los commits por las rutas que tocaron, así que un
commit `feat:`/`fix:` que tocó un solo paquete cuenta igual para las dos versiones. Es una
limitación conocida y aceptada, no un bug a "arreglar" agregando scoping por ruta. Ver
[`CONTRIBUTING.md`](https://github.com/Indroic/HexCore/blob/master/CONTRIBUTING.md) para el
razonamiento completo.

## Layout del repositorio

```
packages/darwin-client/
├── src/
│   ├── core/          # cliente, fetcher, errores
│   ├── session/       # store, controlador de refresh
│   ├── transport/     # bearer, cookie, storages, cookie jars
│   ├── plugins/       # los seis plugins incluidos
│   ├── generated/     # ⚠️ generado — no editar
│   ├── index.ts       # el punto de entrada principal
│   ├── webauthn.ts    # el subpath /webauthn
│   └── store.ts       # el subpath /store
├── openapi/           # volcado desde el paquete Python, versionado
├── scripts/           # gen-error-codes.mjs
└── test/
```

---

Volver al **[índice](./README.md)**.
