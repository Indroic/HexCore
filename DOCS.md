# La documentación se mudó a `docs/`

Este archivo era la guía larga de HexCore. Se reemplazó por una documentación completa,
organizada por tema y disponible en dos idiomas.

**→ [`docs/`](./docs/) — 🇪🇸 [español](./docs/es/) · 🇬🇧 [English](./docs/en/)**

---

## Dónde quedó cada cosa

| Lo que buscabas acá | Dónde está ahora |
| :-- | :-- |
| Arranque: una app en una pantalla | [inicio-rapido](./docs/es/inicio-rapido.md) · [quickstart](./docs/en/quickstart.md) |
| Los tres imports y las fachadas | [referencia](./docs/es/referencia.md) · [reference](./docs/en/reference.md) |
| Configuración v2 y `LazyConfig` | [configuracion](./docs/es/configuracion.md) · [configuration](./docs/en/configuration.md) |
| Templates de la CLI | [cli](./docs/es/cli.md) · [cli](./docs/en/cli.md) |
| `BaseEntity`, eventos y repositorios | [repositorios](./docs/es/repositorios.md) · [repositories](./docs/en/repositories.md) |
| Scopes, UoW y Alembic | [sql](./docs/es/sql.md) · [sql](./docs/en/sql.md) |
| Documentos Beanie | [repositorios](./docs/es/repositorios.md) · [repositories](./docs/en/repositories.md) |
| Darwin: referencia estructural | [darwin/](./docs/es/darwin/) · [darwin/](./docs/en/darwin/) |
| Darwin: puertos, backends y esquema | [almacenamiento](./docs/es/darwin/almacenamiento.md) · [storage](./docs/en/darwin/storage.md) |

Lo que este archivo documentaba de los repositorios genéricos había quedado desactualizado: las
clases que mostraba —`SQLAlchemyCommonImplementationsRepo` y
`BeanieODMCommonImplementationsRepo`— se eliminaron en 7.0, y sus reemplazos son
`SqlAlchemyRepository` y `BeanieRepository`. La tabla completa de renombres está en
[versiones y migración](./docs/es/versiones-y-migracion.md) ·
[versions and migration](./docs/en/versions-and-migration.md).

---

## Por qué se mudó

Una guía larga en un solo archivo tiene dos problemas que se agravan con el tamaño: nadie la
lee entera, y las secciones que ya no son ciertas no se notan. Este archivo llegó a describir
una API removida dos majors atrás, y la única razón de que se descubriera fue leerlo de punta a
punta buscando otra cosa.

La documentación de `docs/` está partida por tema, y sus ejemplos se ejecutan: ver
[la regla](./docs/README.md#la-regla-de-esta-documentación).
