# @hexcore/darwin-client

Cliente TypeScript agnóstico de framework de UI, de runtime y de backend para **Darwin**, el
módulo de identidad de [HexCore](../hexcore/). Sin dependencias de runtime.

> **Estado**: esqueleto. Este paquete todavía no expone `createDarwinClient()` — sólo la capa
> de transporte (`CookieTransport`/`BearerTransport`) y los tipos/códigos de error generados
> desde el contrato Python (`openapi/`). El fetcher con refresh single-flight, el store de
> sesión y los plugins llegan en las próximas sub-fases.

## Por qué existe

Darwin expone su contrato HTTP sólo desde el código Python: doble transporte cookie/Bearer,
CSRF double-submit derivado por HMAC, y un `access_ttl` de dos minutos que exige refresh
rotativo con detección de reuso. Sin un cliente versionado junto al servidor, cualquier
frontend reimplementa esas cuatro cosas a mano — y equivocarse en cualquiera de ellas es un
bug de seguridad, no de comodidad.

## Desarrollo

```bash
npm install
npm -w @hexcore/darwin-client run gen        # regenera src/generated/ desde openapi/
npm -w @hexcore/darwin-client run typecheck
npm -w @hexcore/darwin-client run test
npm -w @hexcore/darwin-client run build
```

`openapi/` se vuelca desde el paquete Python con
`uv run python scripts/darwin_openapi.py --write` (ver `packages/hexcore/scripts/`) y se versiona
en git: el drift entre el contrato y el cliente aparece en el diff del PR.
