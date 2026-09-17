## darwin-client-v1.0.0 (2026-09-17)

### BREAKING CHANGE

- `User.email` pasa de `str` a `str | None`, y el parámetro `email` de
`IdentityService.sign_in()` se llama ahora `identifier`. El nombre viejo sigue resolviendo
con un `DeprecationWarning` y se elimina en 11.0; el cuerpo HTTP de `/auth/sign-in` acepta
`identifier`, `email` y `username`, así que un front de 9.x no necesita cambios.

### Feat

- **darwin**: login por username, email opcional, y las sesiones dejan de perder los permisos

### Fix

- **test**: el warning se busca, no se asume que sea el primero capturado
- **deps**: sube SQLAlchemy a 2.0.54 para que Python 3.14 tenga greenlet
- **deps**: sube pydantic en el lock para que 3.14 tenga wheels
- **packaging**: el paquete no declaraba ningún classifier, y el badge de Python decía "missing"
- **skill**: el frontmatter de SKILL.md no parseaba como YAML

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
