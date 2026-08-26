"""
Verifica la regla de tipado de la casa sobre `hexcore/`.

Antes de esto había siete idiomas conviviendo para lo mismo: `from typing import Optional`,
`import typing as t`, `from typing import TYPE_CHECKING`, dos bloques `if TYPE_CHECKING`
separados en el mismo archivo, un `try/except ImportError` con una clase vacía adentro del
bloque… Todos funcionan. El problema de tener siete no es estético: cuando cada archivo
resuelve lo mismo distinto, nadie puede leer un archivo nuevo y saber si la diferencia
significa algo. Y cuando además una de las variantes rompe el tipado —la clase vacía en el
`except` le gana la resolución del nombre a la real—, la inconsistencia deja de ser gusto y
pasa a ser un defecto que se copia y pega.

La regla, una sola, en tres partes:

1. **`import typing as t`**, no `from typing import X`. Un solo nombre importado del módulo
   más importado del árbol, y `t.Optional` dice de dónde sale sin que haya que subir a mirar
   los imports.
2. **Como mucho un bloque `if t.TYPE_CHECKING:`** por archivo. Dos bloques separados son la
   forma en que un import de tipo se termina duplicando o contradiciendo.
3. **Ese bloque contiene sólo imports.** Es lo que garantiza que borrarlo mentalmente sea una
   operación segura: si adentro hay lógica, el archivo se comporta distinto para el checker
   que para el intérprete, que es justo lo que este script existe para evitar.

Lo que **sí** se permite y no cuenta como bloque es `if not t.TYPE_CHECKING:`. Es el idioma
contrario y resuelve otro problema: el respaldo que tiene que existir en runtime pero que el
checker no debe ver, porque si lo ve se queda con él. Se usa en `implementations.py` y en
`uow/__init__.py`, y cada uso lleva el comentario que explica qué pasa sin él.

Uso::

    uv run python scripts/house_rules.py

Sale con 1 y una anotación `::error file=…,line=…::` por incumplimiento, para que en un PR
aparezca sobre la línea y no haya que abrir el log.
"""
from __future__ import annotations

import ast
import pathlib
import sys
import typing as t

RAIZ = pathlib.Path(__file__).resolve().parent.parent
PAQUETE = RAIZ / "hexcore"


class Falta(t.NamedTuple):
    ruta: str
    linea: int
    regla: str
    mensaje: str


def _es_type_checking(prueba: ast.expr) -> bool:
    """`t.TYPE_CHECKING` o `TYPE_CHECKING` pelado, sin el `not`."""
    if isinstance(prueba, ast.Attribute):
        return prueba.attr == "TYPE_CHECKING"
    return isinstance(prueba, ast.Name) and prueba.id == "TYPE_CHECKING"


def _revisar(archivo: pathlib.Path) -> list[Falta]:
    rel = archivo.relative_to(RAIZ).as_posix()
    try:
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return [Falta(rel, exc.lineno or 1, "sintaxis", f"no parsea: {exc.msg}")]

    faltas: list[Falta] = []

    # ── 1. `import typing as t` ───────────────────────────────────────────────
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.ImportFrom) and nodo.module == "typing":
            nombres = ", ".join(a.name for a in nodo.names)
            faltas.append(
                Falta(
                    rel,
                    nodo.lineno,
                    "typing-idiom",
                    f"`from typing import {nombres}`. La casa usa `import typing as t` y "
                    f"`t.{nodo.names[0].name}` en el uso.",
                )
            )

    # ── 2 y 3. El bloque `if t.TYPE_CHECKING:` ────────────────────────────────
    bloques = [
        nodo
        for nodo in ast.walk(arbol)
        if isinstance(nodo, ast.If) and _es_type_checking(nodo.test)
    ]

    if len(bloques) > 1:
        for extra in bloques[1:]:
            faltas.append(
                Falta(
                    rel,
                    extra.lineno,
                    "type-checking-duplicado",
                    f"Segundo bloque `if t.TYPE_CHECKING:` en el archivo (el primero está en "
                    f"la línea {bloques[0].lineno}). Juntalos: dos bloques es como un import "
                    f"de tipo termina duplicado o contradicho.",
                )
            )

    for bloque in bloques:
        for sentencia in bloque.body:
            if isinstance(sentencia, (ast.Import, ast.ImportFrom, ast.Pass)):
                continue
            faltas.append(
                Falta(
                    rel,
                    sentencia.lineno,
                    "type-checking-con-logica",
                    "Un bloque `if t.TYPE_CHECKING:` lleva sólo imports. Lo que tenga que "
                    "correr en runtime va afuera; lo que el checker no debe ver va en un "
                    "`if not t.TYPE_CHECKING:`, que es el idioma contrario y sí lo admite.",
                )
            )

    return faltas


def main() -> int:
    faltas: list[Falta] = []
    for archivo in sorted(PAQUETE.rglob("*.py")):
        if "__pycache__" in archivo.parts:
            continue
        faltas.extend(_revisar(archivo))

    if not faltas:
        print("Regla de la casa: en verde.")
        return 0

    for f in faltas:
        print(f"::error file={f.ruta},line={f.linea}::[{f.regla}] {f.mensaje}")

    print()
    print(f"{len(faltas)} incumplimiento(s) de la regla de tipado de la casa.")
    print("Está explicada entera en el docstring de `scripts/house_rules.py`.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
