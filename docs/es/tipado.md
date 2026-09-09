# Tipado

HexCore shippea tipos: trae `py.typed`, corre Pyright en modo `strict` sobre todo el paquete, y
publica stubs generados para las fachadas. Este documento cubre **lo que necesitás saber como
consumidor**; el contrato completo para contribuir está en
[`docs/ARCHITECTURE_TYPING.md`](../ARCHITECTURE_TYPING.md).

---

## Como consumidor

```sh
pip install hexcore
```

El paquete incluye el marcador PEP 561 (`py.typed`) y los `.pyi`, así que tu type checker ve los
tipos sin instalar nada más. No hay un `types-hexcore` aparte.

⚠️ Esto no siempre fue así, y el modo de falla es sutil: `MANIFEST.in` gobierna el **sdist**, no
la wheel. Durante un tiempo `pip install hexcore` entregaba un paquete que los checkers
ignoraban, porque el marcador estaba en el repositorio y no en el artefacto. Hoy está declarado
en `[tool.setuptools.package-data]` **y hay un test que lo verifica sobre la wheel construida**.

### Las fachadas y los stubs

Las cuatro fachadas (`hexcore.fastapi`, `hexcore.cqrs`, `hexcore.sql`, `hexcore.darwin`)
resuelven sus nombres en runtime con `__getattr__` (PEP 562). Eso es lo que permite que
`import hexcore.cqrs` funcione sin ningún extra instalado.

El costo es que, sin ayuda, **un checker vería `Any` en los 64 símbolos de `hexcore.cqrs`**: un
`__getattr__` que devuelve `t.Any` es exactamente eso. Por eso cada fachada tiene un `.pyi`
generado al lado, con un `from … import X as X` por símbolo.

Python usa el `.py` y el checker usa el `.pyi`, así que **la carga perezosa se mantiene y los
tipos son reales**. No hay que hacer nada para aprovecharlo.

Los `.pyi` están **generados** y llevan un encabezado que lo dice:

```sh
uv run python scripts/gen_stubs.py --write
```

Un job de CI (`stubs-drift`) revierte cualquier edición a mano.

---

## La regla de la casa

Es una sola, y explica casi todo el estilo del código:

> **Un módulo es «hoja con extra» o es «núcleo». No hay una tercera opción.**

- Una **hoja con extra** importa su paquete de tercero en el top level, **sin guardas**. Una
  sola definición, sin ramas, sin `# type: ignore`. `sqlalchemy` está o el módulo no se importa.
- Un **núcleo** es importable sin ningún extra, y difiere la resolución de los nombres que sí
  los necesitan.

El idioma que esta regla mata es el `try: import X / except ImportError: X = None` seguido de
dos definiciones de la misma clase. Pyright analiza **las dos ramas**, se queda con la última
—la vacía— y el tipo termina siendo `Any` incluso con el paquete instalado. O sea: el patrón que
parecía «defensivo» era el que borraba los tipos.

El error amable se conserva, pero pasa a ser **de runtime y de un solo lugar**:
`require_extra(modulo, para=...)` en [`hexcore.capabilities`](./instalacion.md#importar-sin-extras).

### `# type: ignore`

La política es explícita: **no hay supresiones peladas**. Cada una es un
`# pyright: ignore[regla]` con la razón al lado, y `reportUnnecessaryTypeIgnoreComment` está en
`error`, así que una supresión que dejó de hacer falta rompe el build.

El motivo es que, sin esa regla, una supresión que ya no tapa nada es **indistinguible** de una
que sí, y la deuda vuelve por donde se fue.

---

## Los gates

| Gate | Qué hace | Cuándo corre |
| :-- | :-- | :-- |
| `pyright hexcore` | Modo `strict`, Python 3.12 | Siempre |
| `scripts/typing_ratchet.py` | Compara contra `typing-baseline.json` | El veredicto real |
| `stubs-drift` | Regenera los `.pyi` y falla si cambian | Siempre |
| `scripts/house_rules.py` | Verifica «hoja o núcleo» | Siempre |
| `scripts/stub_quality.py` | `pyright --verifytypes`: qué porcentaje de la superficie pública tipa de verdad, y no deja que baje | Siempre |
| `scripts/extra_smoke.py` | Con **un solo** extra puesto: que alcance para lo suyo y no para lo ajeno | Matriz de CI |
| `pytest -m typing` | Los tests de tipo de `tests/typing/` | Job propio |
| `pytest -m packaging` | Construye la wheel y verifica su contenido | Job propio |

### Por qué un ratchet y no «cero errores»

El veredicto no lo da el exit code de Pyright, sino el ratchet: **la deuda existente queda
congelada y sólo puede bajar**.

Prender todas las reglas el primer día pondría `master` en rojo, y un gate rojo desde el arranque
se termina desactivando — que es la única forma garantizada de no arreglar nada. Cada regla se
prende **después** de que el código que gatea esté limpio.

Las dos que faltan están nombradas en el `pyproject`: `reportMissingTypeStubs = "error"` y
`enableTypeIgnoreComments = false`.

### Los tests de tipo

`tests/typing/` **no se ejecuta**: son archivos que se le pasan a Pyright. `norecursedirs` los
saca de la colección de pytest, y `tests/test_typing_gate.py` es el que invoca al checker.

Un test que afirma «esto tipa `Any`» tiene que ser un archivo que el checker lea, no uno que el
intérprete corra: en runtime `Any` y el tipo real son indistinguibles.

---

## Escribir código tipado contra HexCore

### Handlers

Heredar del abstracto es lo que le da al checker el tipo del resultado:

```python
from hexcore.domain.cqrs import AbstractCommandHandler


class CrearTicketHandler(AbstractCommandHandler[CrearTicket, str]):
    async def handle(self, command: CrearTicket) -> str:
        ...
```

### Repositorios

Los dos parámetros genéricos son la entidad y el modelo:

```python
class TicketRepository(SqlAlchemyRepository[Ticket, TicketModel]):
    ...
```

### El `ServerConfig` y los campos `t.Any`

`ServerConfig.cqrs` y `ServerConfig.darwin` están anotados `t.Any` a propósito: anotarlos de
verdad obligaría a `hexcore.config` a importar esos módulos, y `hexcore.config` lo carga medio
framework. Si querés el tipo en tu propio código, anotalo del lado del consumidor:

```python
from hexcore.darwin import IdentityConfig

identidad: IdentityConfig = config.darwin
```

---

## Siguiente

← Volver al **[índice](./)**, o leer el contrato completo en
[`ARCHITECTURE_TYPING.md`](../ARCHITECTURE_TYPING.md).
