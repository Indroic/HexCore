## darwin-client-v0.1.2 (2026-09-16)

### Fix

- **darwin-client**: el default de `csrfCookieName` no coincidía con ningún nombre que el servidor emita

## darwin-client-v0.1.1 (2026-09-16)

### Fix

- **darwin-client**: los mensajes de error de runtime pasan a inglés
- **ci**: el bump no falla cuando no hay nada que publicar

## darwin-client-v0.1.0 (2026-09-16)

Primera versión: núcleo (`createDarwinClient`, transportes cookie/bearer, refresh
single-flight, store de sesión), los seis plugins (`twoFactor`, `magicLink`, `oauth`,
`passkey`, `impersonate`, `organization`) y los subpaths de navegador (`/webauthn`, `/store`).
