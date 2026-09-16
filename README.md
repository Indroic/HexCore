# HexCore — monorepo

[![PyPI](https://img.shields.io/pypi/v/hexcore?label=hexcore&color=blue)](https://pypi.org/project/hexcore/)
[![npm](https://img.shields.io/npm/v/@hexcore-js/darwin-client?label=%40hexcore-js%2Fdarwin-client&color=blue)](https://www.npmjs.com/package/@hexcore-js/darwin-client)
[![License](https://img.shields.io/github/license/Indroic/HexCore)](./LICENSE)

📖 **Toda la documentación vive en [`docs/`](./docs/)**, en inglés y en español, para los dos
paquetes.

Este repositorio es un monorepo con dos paquetes:

| | Qué es | Documentación | Publicado en |
| :-- | :-- | :-- | :-- |
| 🐍 [`packages/hexcore/`](./packages/hexcore/) | El meta-framework Python de arquitectura hexagonal + DDD + CQRS | 🇬🇧 [EN](./docs/hexcore/en/) · 🇪🇸 [ES](./docs/hexcore/es/) | [PyPI: `hexcore`](https://pypi.org/project/hexcore/) |
| 🟦 [`packages/darwin-client/`](./packages/darwin-client/) | El cliente TypeScript agnóstico de framework, runtime y backend para Darwin, el módulo de identidad de HexCore | 🇬🇧 [EN](./docs/darwin-client/en/) · 🇪🇸 [ES](./docs/darwin-client/es/) | [npm: `@hexcore-js/darwin-client`](https://www.npmjs.com/package/@hexcore-js/darwin-client) |

## Por qué un monorepo

Darwin expone su contrato HTTP sólo desde el código Python: sin un cliente TypeScript
versionado junto al servidor, cualquier frontend reimplementa a mano el refresh rotativo, el
CSRF double-submit y la elección de transporte — y equivocarse en cualquiera de los tres es un
bug de seguridad, no de comodidad. Tener los dos paquetes en el mismo repo permite un gate de
CI que falla cuando el cliente y el servidor divergen.

## Documentación

Toda la documentación del monorepo está consolidada en [`docs/`](./docs/):

```
docs/
├── README.md                  ← índice: los dos paquetes, los dos idiomas
├── ARCHITECTURE_TYPING.md     ← el contrato de tipado estricto del paquete Python
├── hexcore/
│   ├── en/   ·   es/          ← 20 guías por idioma, incluida Darwin
└── darwin-client/
    └── en/   ·   es/          ← 8 guías por idioma
```

**El inglés es la versión de referencia**; `es/` es su traducción. Si las dos se contradicen,
gana el inglés.

Los `README.md` de cada paquete siguen siendo completos y autosuficientes — son lo que
renderizan PyPI y npm — y enlazan a `docs/` con URLs absolutas de GitHub, que son las únicas
que resuelven fuera del repositorio.

## Desarrollo

```bash
# Python
cd packages/hexcore && uv run pytest -q

# TypeScript
npm install
npm -w @hexcore-js/darwin-client run test
```

Ver [`CONTRIBUTING.md`](./CONTRIBUTING.md) para el flujo completo de contribución, incluido el
layout de `packages/`, las dos pipelines de release independientes y por qué.
