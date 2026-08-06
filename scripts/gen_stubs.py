"""
Genera los `.pyi` de las fachadas perezosas desde su `_EXPORTS`.

El problema: `hexcore/cqrs.py`, `hexcore/sql.py` y `hexcore/fastapi.py` —las tres fachadas
que la documentación publicita como "un import obvio por tarea"— resuelven sus exports con
`_EXPORTS` + `__getattr__` y declaran `__all__ = sorted(_EXPORTS)`. Las dos cosas son
expresiones de **runtime**, así que ningún type checker las puede evaluar: los 126 símbolos
de la superficie pública recomendada tipan `Any`.

La ironía es que la superficie **deprecada** sí tiene shims `if TYPE_CHECKING` que la hacen
resoluble. Lo viejo tipa bien y lo nuevo tipa `Any`.

Un `.pyi` lo arregla sin tocar el runtime: Pyright usa **sólo** el `.pyi` y Python usa
**sólo** el `.py`, así que el stub describe la superficie estática y el fuente conserva la
pereza (importar `hexcore.sql` sigue sin arrastrar sqlalchemy).

Por qué generarlos y no escribirlos a mano: un stub escrito a mano se desincroniza, y un
stub desincronizado promete símbolos que no existen — peor que no tenerlo. `_EXPORTS` **es**
la fuente de verdad, así que el mapeo es mecánico y determinista, y el job `stubs-drift` lo
verifica en cada PR.

Se trabaja sobre el **AST**, no importando el módulo: así no hace falta ningún extra
instalado y el job es el más rápido del workflow.

Uso::

    uv run python scripts/gen_stubs.py --write    # regenera
    uv run python scripts/gen_stubs.py --check    # falla si hay drift (lo que corre CI)
"""
from __future__ import annotations

import argparse
import ast
import difflib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Las fachadas que necesitan stub. Deliberadamente **sólo** éstas: un `.pyi` es una segunda
#: copia que hay que mantener, así que se justifica sólo donde la superficie es una
#: expresión de runtime que ningún checker puede evaluar. Todo lo demás se arregla inline,
#: en el fuente.
#:
#: `darwin` es un **paquete**, así que su fachada vive en `hexcore/darwin/__init__.py`: un
#: módulo y un paquete con el mismo nombre no pueden coexistir (el paquete gana y el módulo
#: queda muerto), así que el `__init__` es la fachada. `_rutas()` resuelve las dos formas.
FACHADAS = ("cqrs", "sql", "fastapi", "darwin")


def _rutas(modulo: str) -> tuple[Path, Path]:
    """
    Devuelve `(fuente, stub)` para una fachada, sea módulo o paquete.

    Raises:
        SystemExit: si no existe ni `hexcore/<modulo>.py` ni `hexcore/<modulo>/__init__.py`.
    """
    como_modulo = REPO_ROOT / "hexcore" / f"{modulo}.py"
    if como_modulo.is_file():
        return como_modulo, como_modulo.with_suffix(".pyi")

    como_paquete = REPO_ROOT / "hexcore" / modulo / "__init__.py"
    if como_paquete.is_file():
        return como_paquete, como_paquete.with_suffix(".pyi")

    raise SystemExit(
        f"::error::no existe ni hexcore/{modulo}.py ni hexcore/{modulo}/__init__.py."
    )

CABECERA = '''\
# ⚠️  ARCHIVO GENERADO — NO EDITAR A MANO.
#
# Generado por `scripts/gen_stubs.py` desde el `_EXPORTS` de `{fuente}`.
# Si editás esto a mano, el job `stubs-drift` de CI te lo va a revertir.
#
# Para regenerar:
#
#     uv run python scripts/gen_stubs.py --write
#
# Existe porque la fachada resuelve sus exports con `__getattr__` y declara
# `__all__ = sorted(_EXPORTS)`: las dos son expresiones de runtime, así que sin este stub
# los {total} símbolos de `hexcore.{modulo}` tipan `Any`. El runtime no cambia — Python usa
# el `.py` y el checker usa el `.pyi`, así que la carga perezosa se mantiene.
'''


def _leer_exports(ruta: Path) -> dict[str, tuple[str, str]]:
    """
    Extrae el literal `_EXPORTS` del AST del módulo, sin importarlo.

    Se exige que sea un literal: si alguien lo construye en runtime (un `.update()`, una
    comprensión), el generador tiene que fallar en vez de emitir un stub incompleto.
    """
    arbol = ast.parse(ruta.read_text(encoding="utf-8"), filename=str(ruta))

    for nodo in arbol.body:
        objetivo = None
        if isinstance(nodo, ast.AnnAssign) and isinstance(nodo.target, ast.Name):
            objetivo, valor = nodo.target.id, nodo.value
        elif isinstance(nodo, ast.Assign) and len(nodo.targets) == 1:
            if isinstance(nodo.targets[0], ast.Name):
                objetivo, valor = nodo.targets[0].id, nodo.value

        if objetivo != "_EXPORTS":
            continue
        if valor is None:
            break

        try:
            crudo = ast.literal_eval(valor)
        except ValueError as exc:
            raise SystemExit(
                f"::error::{ruta}: `_EXPORTS` no es un literal evaluable ({exc}). El "
                f"generador trabaja sobre el AST a propósito —para no necesitar extras "
                f"instalados—, así que `_EXPORTS` tiene que ser un dict literal."
            ) from exc

        exports: dict[str, tuple[str, str]] = {}
        for nombre, destino in crudo.items():
            if not (isinstance(destino, tuple) and len(destino) == 2):
                raise SystemExit(
                    f"::error::{ruta}: la entrada {nombre!r} de `_EXPORTS` no es una tupla "
                    f"(modulo, atributo), es {destino!r}."
                )
            exports[nombre] = (destino[0], destino[1])
        return exports

    raise SystemExit(f"::error::{ruta} no declara `_EXPORTS`.")


def _generar(
    modulo: str, exports: dict[str, tuple[str, str]], ruta_fuente: str
) -> str:
    lineas = [
        CABECERA.format(modulo=modulo, total=len(exports), fuente=ruta_fuente),
        "",
    ]

    # Agrupado por módulo de origen y ordenado, para que la salida sea determinista: dos
    # corridas sobre el mismo `_EXPORTS` tienen que dar byte por byte lo mismo, o el job de
    # drift daría falsos positivos.
    por_origen: dict[str, list[tuple[str, str]]] = {}
    for nombre, (origen, atributo) in exports.items():
        por_origen.setdefault(origen, []).append((nombre, atributo))

    for origen in sorted(por_origen):
        for nombre, atributo in sorted(por_origen[origen]):
            # El `as` es redundante a la vista pero **obligatorio**: en un `.pyi`, un import
            # sin `as` no cuenta como re-exportado (PEP 484), así que sin él el stub no
            # exporta nada y todo `from hexcore.sql import X` sería un error.
            lineas.append(f"from {origen} import {atributo} as {nombre}")

    lineas.append("")
    # `__all__` literal, que es lo que `sorted(_EXPORTS)` no puede ser para un checker.
    # Mismo orden que en runtime, y hay un test que los compara.
    lineas.append("__all__ = [")
    for nombre in sorted(exports):
        lineas.append(f'    "{nombre}",')
    lineas.append("]")
    lineas.append("")

    # Sin `def __getattr__(name: str) -> Any`: declararlo haría que Pyright acepte
    # cualquier atributo del módulo y se pierde la detección de typos, que es la mitad de la
    # razón para tener el stub.
    return "\n".join(lineas)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modo = parser.add_mutually_exclusive_group(required=True)
    modo.add_argument("--write", action="store_true", help="Escribe los .pyi")
    modo.add_argument("--check", action="store_true", help="Falla si hay drift")
    args = parser.parse_args()

    desincronizados: list[str] = []

    for modulo in FACHADAS:
        fuente, stub = _rutas(modulo)

        esperado = _generar(
            modulo, _leer_exports(fuente), fuente.relative_to(REPO_ROOT).as_posix()
        )

        if args.write:
            stub.write_text(esperado, encoding="utf-8")
            print(f"escrito {stub.relative_to(REPO_ROOT).as_posix()}")
            continue

        actual = stub.read_text(encoding="utf-8") if stub.exists() else ""
        if actual == esperado:
            print(f"ok {stub.relative_to(REPO_ROOT).as_posix()}")
            continue

        desincronizados.append(modulo)
        ruta = stub.relative_to(REPO_ROOT).as_posix()
        print(
            f"::error::{ruta} está desincronizado con el `_EXPORTS` de "
            f"{fuente.relative_to(REPO_ROOT).as_posix()}."
        )
        diff = difflib.unified_diff(
            actual.splitlines(),
            esperado.splitlines(),
            fromfile=f"{ruta} (en el repo)",
            tofile=f"{ruta} (esperado)",
            lineterm="",
        )
        print("::group::diff")
        for linea in diff:
            print(linea)
        print("::endgroup::")

    if desincronizados:
        print(
            f"\n{len(desincronizados)} stub(s) desincronizado(s). Regeneralos:\n\n"
            "    uv run python scripts/gen_stubs.py --write\n"
        )
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
