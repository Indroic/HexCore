# HexCore — monorepo

Este repositorio es un monorepo con dos paquetes:

- **[`packages/hexcore/`](./packages/hexcore/)** — la librería Python de arquitectura
  hexagonal + DDD + CQRS, publicada en PyPI como `hexcore`. Es el paquete original de este
  repo; toda su documentación vive en [`packages/hexcore/README.md`](./packages/hexcore/README.md)
  y en [`packages/hexcore/docs/`](./packages/hexcore/docs/).
- **`packages/darwin-client/`** — cliente TypeScript agnóstico de framework, runtime y backend
  para Darwin (el módulo de identidad de HexCore), publicado en npm como
  `@hexcore/darwin-client`.

## Por qué un monorepo

Darwin expone su contrato HTTP sólo desde el código Python: sin un cliente TypeScript
versionado junto al servidor, cualquier frontend reimplementa a mano el refresh rotativo, el
CSRF double-submit y la elección de transporte — y equivocarse en cualquiera de los tres es un
bug de seguridad, no de comodidad. Tener los dos paquetes en el mismo repo permite un gate de
CI que falla cuando el cliente y el servidor divergen.

## Desarrollo

```bash
cd packages/hexcore && uv run pytest -q
```

Ver `CONTRIBUTING.md` para el flujo completo de contribución, incluido el layout de
`packages/` y por qué.
