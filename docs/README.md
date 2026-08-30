# Documentación de HexCore

| Dónde | Qué encontrás |
| :-- | :-- |
| [README del proyecto](../README.md) | El framework: CQRS, capa SQL, FastAPI, task queues, cron |
| [DOCS.md](../DOCS.md) | Referencia estructural: directorios, entidades, repositorios |
| **[Darwin](./darwin/)** | El módulo de identidad |
| [ARCHITECTURE_TYPING.md](./ARCHITECTURE_TYPING.md) | El sistema de tipos y su gate de CI |

---

## Darwin

| Documento | Qué cubre |
| :-- | :-- |
| [Introducción](./darwin/README.md) | Qué es, quickstart, y las decisiones que cambian cómo lo integrás |
| [Almacenamiento](./darwin/almacenamiento.md) | Backends, esquema, Alembic, `init_beanie` |
| [Plugins incluidos](./darwin/plugins-incluidos.md) | Los seis, con rutas y advertencias |
| [Escribir un plugin propio](./darwin/plugins-propios.md) | Puntos de extensión, hooks, y las trampas |

---

## Sobre esta documentación

Acá vive lo que le sirve a quien **usa** HexCore. Los ejemplos son código que corre: los del
README y `DOCS.md` los ejecuta `tests/test_documentation_examples.py`, y el plugin de ejemplo de
la guía de extensión vive ejercitado en `tests/test_darwin_custom_plugin.py`.

Esa es la regla: **si un documento muestra código, hay un test que lo corre**. Una guía que no
corre envejece sin avisar — el día que un punto de extensión cambia de firma, el documento sigue
diciendo lo de antes y el primero en enterarse es alguien que ya escribió medio plugin
siguiéndolo.

Lo que **no** vive acá es la historia de cómo se construyó cada cosa. Hubo un
`ARCHITECTURE_DARWIN.md` de 2.300 líneas que documentaba las trece fases de desarrollo de
Darwin: era un registro de decisiones útil mientras se escribía y un obstáculo para quien llega
a usarlo, porque obligaba a leer la construcción para encontrar la interfaz. Lo que de ahí le
sirve a un consumidor —las decisiones de diseño que cambian cómo integrás el módulo— está
repartido en los documentos de arriba, en el lugar donde se necesita. El resto está en el
historial de git, que es donde va la historia.

`ARCHITECTURE_TYPING.md` se queda porque cumple otra función: describe un **contrato vigente**
—la regla de tipado de la casa y los gates que la verifican— que hay que conocer para contribuir.
