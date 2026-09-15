"""
Mide qué porcentaje de la superficie pública de `hexcore` tipa de verdad, y no deja que baje.

`pyright --verifytypes` recorre todo lo que el paquete exporta y cuenta cuánto tiene tipo
conocido, ambiguo o desconocido. Es la única medida que mira lo mismo que ve el consumidor: el
ratchet de `typing_ratchet.py` cuenta errores **adentro** de `hexcore/`, y un paquete puede no
tener un solo error interno y aun así exportar todo como `Any` — que es exactamente lo que
pasaba con las fachadas antes de los `.pyi` generados.

Se mide contra la **wheel instalada**, no contra el árbol de fuentes, y eso no es un detalle de
implementación: `--verifytypes` resuelve por PEP 561, o sea que necesita el `py.typed` y los
`.pyi` adentro del paquete instalado. Medir el árbol de fuentes diría que todo está bien aunque
el empaquetado se olvide de publicarlos, que es el modo de falla que importa — el tipado es una
propiedad del artefacto, no del repo.

El piso vive en `typing-baseline.json`, en `stub_completeness`, y sólo puede subir.

Uso::

    uv run python scripts/stub_quality.py --python .venv-wheel/bin/python
    uv run python scripts/stub_quality.py --python .venv-wheel/bin/python --update
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

RAIZ = pathlib.Path(__file__).resolve().parent.parent
BASELINE = RAIZ / "typing-baseline.json"
CLAVE = "stub_completeness"

#: Cuánto puede bajar sin que se considere una regresión, en puntos porcentuales.
#:
#: Cero: el punto del ratchet es que no baje. La tolerancia existe en otros gates porque miden
#: algo ruidoso (tiempos, tamaños); acá el número es determinista para una versión dada de
#: pyright, así que cualquier baja es un cambio real en lo que el consumidor ve.
TOLERANCIA = 0.0


def _medir(python: str | None) -> tuple[float, dict[str, int], int]:
    cmd = ["pyright", "--verifytypes", "hexcore", "--outputjson"]
    if python:
        cmd[1:1] = ["--pythonpath", python]

    salida = subprocess.run(
        cmd, cwd=RAIZ, capture_output=True, text=True, encoding="utf-8", errors="replace"
    ).stdout
    if "{" not in salida:
        raise SystemExit(
            "::error::pyright no devolvió JSON. ¿Está instalado y en el PATH del entorno?"
        )
    datos = json.loads(salida[salida.index("{") :])
    resumen = datos.get("typeCompleteness", {})
    puntaje = float(resumen.get("completenessScore", 0.0)) * 100
    conteos = resumen.get("exportedSymbolCounts", {})
    modulos = len(resumen.get("modules", []))
    return puntaje, conteos, modulos


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python",
        default=None,
        help="Intérprete del entorno donde está instalada la wheel. Sin esto se mide el "
        "entorno actual, que en un checkout editable NO resuelve el paquete.",
    )
    parser.add_argument(
        "--update",
        action="store_true",
        help="Guardar el puntaje medido como piso nuevo.",
    )
    args = parser.parse_args()

    puntaje, conteos, modulos = _medir(args.python)

    # El modo de falla silencioso: pyright no resuelve el paquete y devuelve todo en cero. Sin
    # este chequeo, un `--update` en esa situación grabaría un piso de 0 % y el gate quedaría
    # incapaz de fallar para siempre — la peor versión de un gate, porque parece que está.
    if modulos == 0 or sum(conteos.values()) == 0:
        print(
            "::error::`--verifytypes` no encontró el paquete: 0 módulos y 0 símbolos. No es "
            "que el tipado sea malo, es que pyright no lo resolvió por PEP 561.\n"
            "    La causa habitual es el entorno: la wheel no está instalada donde apunta "
            "`--python`, o se instaló en modo editable — el finder de `__editable__` no es "
            "resoluble por PEP 561.\n"
            "    Ojo con darlo por resuelto ahí: con la wheel bien instalada en un venv "
            "limpio esto igual devuelve cero, mientras `--verifytypes pydantic` en el mismo "
            "pyright resuelve bien. Es específico de este paquete y está sin aislar."
        )
        return 1

    print("::group::Completitud de tipos de la superficie pública")
    print(f"  módulos analizados: {modulos}")
    print(f"  símbolos exportados: {sum(conteos.values())}")
    for k, v in sorted(conteos.items()):
        print(f"    {k}: {v}")
    print(f"  completitud: {puntaje:.2f} %")
    print("::endgroup::")

    baseline: dict[str, object] = {}
    if BASELINE.exists():
        baseline = json.loads(BASELINE.read_text(encoding="utf-8"))

    if args.update:
        baseline[CLAVE] = round(puntaje, 2)
        BASELINE.write_text(
            json.dumps(baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        print(f"Piso actualizado: {puntaje:.2f} %")
        return 0

    piso = baseline.get(CLAVE)
    if piso is None:
        print(
            f"::notice::No hay piso registrado todavía. El medido es {puntaje:.2f} %. "
            f"Grabalo con:\n"
            f"    uv run python scripts/stub_quality.py --python <python> --update"
        )
        return 0

    piso_f = float(piso)  # type: ignore[arg-type]
    if puntaje + TOLERANCIA < piso_f:
        print(
            f"::error::La completitud de tipos bajó: {puntaje:.2f} % contra un piso de "
            f"{piso_f:.2f} %.\n"
            f"    Algo que el paquete exporta dejó de tipar. Mirá el detalle de arriba: los "
            f"símbolos con tipo desconocido son los que hay que anotar."
        )
        return 1

    if puntaje > piso_f:
        print(
            f"::notice::Subió de {piso_f:.2f} % a {puntaje:.2f} %. Fijalo con `--update` para "
            f"que no se pueda volver."
        )
    else:
        print(f"Completitud de tipos OK: {puntaje:.2f} %, piso {piso_f:.2f} %.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
