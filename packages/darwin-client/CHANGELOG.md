## darwin-client-v1.5.0 (2026-09-23)

### Feat

- **darwin-client**: refresco de sesión en segundo plano, sin matar la sesión por un fallo transitorio

### Fix

- **deprecations**: ejecutar en el código las remociones que 11.0 ya anunció
- **darwin**: un kid desconocido o retirado daba 500 en vez de 401
- **darwin**: los startup steps de los plugins rompían el arranque

## darwin-client-v1.4.0 (2026-09-18)

### Feat

- **darwin**: ronda de correcciones rbac/drbac para 10.4 (HC-8..HC-22)

## darwin-client-v1.3.0 (2026-09-18)

### Feat

- **darwin-client**: drbac — cliente TypeScript (Fase F6 de rbac/drbac)
- **darwin**: drbac — persistencia, PDP, PIP y router (Fase F5 de rbac/drbac)
- **darwin**: drbac — lenguaje de condiciones (Fase F4 de rbac/drbac)
- **darwin-client**: rbac — cliente TypeScript (Fase F3 de rbac/drbac)
- **darwin**: rbac — servicio, resolver, provider y router (Fase F2 de rbac/drbac)
- **darwin**: rbac — dominio y persistencia (Fase F1 de rbac/drbac)

### Fix

- **darwin**: drbac no confunde latencia de la base con budget de evaluacion
- **ci**: agregar darwin-rbac a la matriz de extras y a extra_smoke

## darwin-client-v1.2.0 (2026-09-17)

### Feat

- **darwin**: contrato de autorización en el núcleo (Fase F0 de rbac/drbac)

### Fix

- **darwin-client**: regenerar error-codes.ts con AccessDeniedError
- **darwin**: regenerar el contrato de errores con AccessDeniedError

## darwin-client-v1.1.0 (2026-09-17)

### Feat

- **darwin**: el estado de la cuenta lo aporta la app y viaja en el token

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
