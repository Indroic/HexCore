"""
Los adaptadores de almacenamiento de `organization`.

Darwin se parte en tres piezas instalables, y esta es la frontera:

- **`[darwin]`** — el núcleo: dominio, servicios, tokens, transportes, plugins. **Sin almacenamiento.**
- **`[darwin-sqlalchemy]`** — este paquete, sobre `sqlalchemy` + `alembic`.
- **`[darwin-beanie]`** — el equivalente sobre Beanie/MongoDB.

**Este `__init__` no importa ninguno de los dos**, y eso es el punto: `import
hexcore.darwin...orms` en un proceso que sólo tiene uno de los extras no puede fallar. Cada
backend se importa por su nombre, y el contenedor elige cuál según `IdentityConfig.storage`.
"""
