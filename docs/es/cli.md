# CLI

El paquete instala un ejecutable `hexcore` (y los proyectos generados traen un `manage.py`
equivalente). Está escrito con Typer, así que `hexcore --help` y `hexcore <comando> --help`
funcionan en todos los niveles.

```sh
hexcore --help
```

---

## `hexcore init` — crear un proyecto

```sh
hexcore init mi_proyecto --template hexagonal
hexcore init mi_proyecto -t vertical-slice
```

Falla si la carpeta ya existe, y si el template no es uno de los dos.

### Template `hexagonal`

Las capas primero, el dominio adentro:

```
mi_proyecto/
├─ src/
│  ├─ domain/
│  ├─ application/
│  └─ infrastructure/
│     └─ database/
│        ├─ models/          # SQLAlchemy
│        ├─ documents/       # Beanie
│        └─ migrations/versions/
├─ tests/domain/
├─ alembic/env.py
├─ alembic.ini
├─ config.py
└─ manage.py
```

`repository_discovery_paths` queda sembrado con `src.infrastructure.repositories` y
`src.infrastructure.repositories.implementations`.

### Template `vertical-slice`

La feature primero, las capas adentro de cada una:

```
mi_proyecto/
├─ src/
│  ├─ features/
│  └─ shared/
│     ├─ domain/
│     ├─ application/
│     └─ infrastructure/
│        └─ database/{models,documents,migrations}
├─ tests/features/
├─ config.py
└─ manage.py
```

`repository_discovery_paths` queda sembrado con `src.features` y
`src.shared.infrastructure.repositories`.

### Lo que hacen los dos

1. Crean los paquetes con sus `__init__.py`.
2. Escriben `README.md`, `.gitignore` y `manage.py`.
3. Escriben `config.py` en la raíz **si no existe** — un `config.py` previo se preserva.
4. Corren `alembic init`, apuntan `version_locations` al directorio de migraciones del template,
   ponen `target_metadata = Base.metadata` y cablean el `env.py` (ver abajo).
5. Corren `ruff format` sobre lo generado, si `ruff` está instalado. No es un requisito: dejó de
   ser dependencia de runtime justamente porque se le imponía a todo el mundo por un comando que
   se corre una vez.

### El `env.py` que genera

Es la parte que más conviene mirar, porque contiene las tres líneas que evitan una pérdida de
datos:

```python
ensure_framework_models_loaded()      # las tablas del framework (hexcore_cron_jobs)

DARWIN_PLUGINS: list[str] = []        # ← llenalo con los plugins que uses
ensure_identity_schema_loaded(plugins=DARWIN_PLUGINS)

import_all_models(models)             # las tuyas, recursivo
```

⚠️ Una tabla que existe en la base y está **ausente de `Base.metadata`** recibe un
`op.drop_table` en la próxima migración autogenerada. Con datos adentro. Es el peor modo de
falla del framework porque no falla: la migración se genera limpia y el daño aparece al
aplicarla.

El bloque de Darwin va dentro de un `try/except ImportError`, así que un proyecto sin el extra
`[darwin]` no se rompe.

---

## `hexcore create-domain-module` — un módulo de dominio

```sh
hexcore create-domain-module ventas
```

Crea, bajo el directorio de dominio del proyecto:

```
ventas/
├─ __init__.py
├─ entities.py
├─ repositories.py      # I<Entidad>Repository, derivada de IBaseRepository
├─ services.py
├─ value_objects.py
├─ events.py
├─ enums.py
└─ exceptions.py
```

Y el módulo de tests correspondiente. El nombre tiene que ser un identificador válido de Python;
si el módulo ya existe, no lo pisa.

---

## Migraciones

```sh
hexcore make-migrations "agrega la tabla de tickets"   # alembic revision --autogenerate -m "..."
hexcore migrate                                        # alembic upgrade head
```

Son envoltorios finos sobre Alembic: corren en el directorio actual y usan tu `alembic.ini`. La
URL sale de `config.sql_database_url` —el DSN **síncrono**—, que es lo que el `env.py` generado
inyecta con `config.set_main_option`.

---

## `hexcore test`

```sh
hexcore test
hexcore test src/domain --extra-args "-k usuarios -q"
```

Envoltorio sobre `pytest`. El primer argumento es la ruta (default: `test`), y `--extra-args`
se pasa tal cual. Propaga el código de salida, así que sirve en CI.

---

## `hexcore identity` — los comandos de Darwin

La sub-app de identidad. Sólo importa `typer` y stdlib en su nivel superior, así que aparece en
el `--help` aunque no tengas el extra instalado, y cada comando importa lo suyo al ejecutarse.

### `generate-secret`

```sh
export HEXCORE_DARWIN_SECRET_KEY="$(hexcore identity generate-secret)"
```

Emite un `secrets.token_urlsafe(48)`. Existe porque el secreto **no tiene default a propósito**,
y el primer obstáculo de quien cablea Darwin es tener que buscar cómo generar uno. Un comando lo
resuelve sin que nadie caiga en la tentación de poner `"changeme"`.

### `generate-keys`

```sh
hexcore identity generate-keys --algorithm Ed25519 --kid 2026-01
```

Emite el par de claves de firma en JWK, por stdout, para redirigir a un secret manager:

```json
{
  "kid": "2026-01",
  "algorithm": "Ed25519",
  "status": "active",
  "public_jwk": { "...": "..." },
  "private_jwk": { "...": "..." }
}
```

⚠️ **La privada sale en claro.** No la dejes en el historial del shell ni en un archivo del
repo. El aviso va a **stderr** justamente para que no ensucie lo que redirijas.

Los dos JWK se emiten **parseados**, y no como el string que guarda `SigningKey`: un secret
manager que recibe un JSON con un string de JSON adentro obliga a un doble parseo del otro lado,
y ése es el paso que alguien termina resolviendo pegando la privada en un archivo intermedio.

### `create-tables`

```sh
hexcore identity create-tables
```

Crea las tablas de identidad contra `config.async_sql_database_url`. **Es un atajo para
desarrollo y tests.** En producción usá Alembic: la función es idempotente pero no versiona
nada, así que un cambio de esquema más adelante no tiene desde dónde migrar.

### `check-schema`

```sh
hexcore identity check-schema
```

Verifica que las tablas de identidad estén en `Base.metadata` y **sale con código 1** si falta
alguna, para poder ponerlo en un pre-commit o en CI:

```
Faltan en Base.metadata: darwin_session, darwin_verification.

`alembic revision --autogenerate` les va a emitir op.drop_table. Importalas desde tu
paquete models/, o agregá `ensure_identity_schema_loaded()` al env.py.
```

Es el chequeo que evita la pérdida de datos más cara del módulo: con Darwin, la tabla que se
borra es el almacén de credenciales completo.

### `plugins`

```sh
hexcore identity plugins myapp.identity
```

Lee un módulo que exponga un `plugins: PluginRegistry` o un `PLUGINS: list[DarwinPlugin]`, y
lista lo que aporta cada plugin: rutas, comandos, hooks y tablas. Sirve para dos cosas —ver de
un vistazo la superficie que estás montando, y sacar la lista exacta que va en el
`DARWIN_PLUGINS` del `env.py`—.

---

## `manage.py`

`hexcore init` deja en la raíz del proyecto:

```python
from hexcore.infrastructure.cli import app as CLI

if __name__ == "__main__":
    CLI()
```

Es la misma CLI, invocada desde el proyecto: `python manage.py migrate` equivale a
`hexcore migrate`, con la ventaja de que corre con el intérprete del entorno del proyecto sin
depender de que el ejecutable esté en el `PATH`.

---

## Siguiente

→ **[Darwin: identidad](./darwin/)**.
