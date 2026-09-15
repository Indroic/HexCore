# The documentation moved to `docs/`

This file used to be HexCore's long-form guide. It has been replaced by a complete
documentation set, organized by topic and available in two languages.

**→ [`docs/`](./docs/) — 🇬🇧 [English](./docs/en/) · 🇪🇸 [español](./docs/es/)**

---

## Where everything went

| What you came here for | Where it is now |
| :-- | :-- |
| Startup: an app on one screen | [quickstart](./docs/en/quickstart.md) · [inicio-rapido](./docs/es/inicio-rapido.md) |
| The facade imports | [reference](./docs/en/reference.md) · [referencia](./docs/es/referencia.md) |
| Configuration and `LazyConfig` | [configuration](./docs/en/configuration.md) · [configuracion](./docs/es/configuracion.md) |
| CLI templates | [cli](./docs/en/cli.md) · [cli](./docs/es/cli.md) |
| `BaseEntity`, events and repositories | [repositories](./docs/en/repositories.md) · [repositorios](./docs/es/repositorios.md) |
| Scopes, UoW and Alembic | [sql](./docs/en/sql.md) · [sql](./docs/es/sql.md) |
| Beanie documents | [repositories](./docs/en/repositories.md) · [repositorios](./docs/es/repositorios.md) |
| Darwin: structural reference | [darwin/](./docs/en/darwin/) · [darwin/](./docs/es/darwin/) |
| Darwin: ports, backends and schema | [storage](./docs/en/darwin/storage.md) · [almacenamiento](./docs/es/darwin/almacenamiento.md) |

What this file documented about the generic repositories had gone stale: the classes it showed —
`SQLAlchemyCommonImplementationsRepo` and `BeanieODMCommonImplementationsRepo` — were removed in
7.0, and their replacements are `SqlAlchemyRepository` and `BeanieRepository`. The full rename
table is in [versions and migration](./docs/en/versions-and-migration.md) ·
[versiones y migración](./docs/es/versiones-y-migracion.md).

---

## Why it moved

A long guide in a single file has two problems that get worse with size: nobody reads it end to
end, and the sections that stopped being true go unnoticed. This file ended up describing an API
that had been removed two majors earlier, and the only reason anyone found out was reading it
front to back looking for something else.

The documentation in `docs/` is split by topic, and its examples are executed: see
[the rule](./docs/README.md#the-rule-of-this-documentation).
