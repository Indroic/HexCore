"""
Ratchet de tipado: la deuda existente se congela y sólo puede bajar.

El problema que resuelve: prender `pyright --strict` sobre un repo con deuda da 216 errores
el primer día. Un gate que exige cero es un gate que se desactiva en el segundo PR. Un gate
que exige "no peor que ayer" se puede prender **hoy** y se puede ir apretando.

Cómo funciona:

- `typing-baseline.json` guarda un presupuesto de errores **por archivo**, más el total.
- Un archivo que supera su presupuesto falla.
- Un archivo **nuevo** con cualquier error falla: es la cláusula que impide que la deuda
  crezca. La deuda vieja se tolera; la nueva, no.
- Un archivo que mejora no falla: avisa con un `::notice::` para que alguien baje el
  baseline. El baseline **nunca** se actualiza solo en CI: se mueve en un PR humano, con
  diff y revisor, así que `git blame` sigue sirviendo para las regresiones de tipado.

Uso::

    # medir y congelar (una vez, y después en cada PR que mejore)
    uv run pyright --outputjson hexcore > pyright.json
    uv run python scripts/typing_ratchet.py errors --report pyright.json --update

    # verificar (lo que corre CI)
    uv run python scripts/typing_ratchet.py errors --report pyright.json

    # completeness de la API pública
    uv run pyright --verifytypes hexcore --outputjson > verifytypes.json
    uv run python scripts/typing_ratchet.py types --report verifytypes.json
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = REPO_ROOT / "typing-baseline.json"

#: Banda muerta de la completeness, en puntos porcentuales. Sin ella, el ruido de coma
#: flotante y el drift entre parches de pyright generan avisos de "mejoraste" espurios.
COMPLETENESS_DEAD_BAND = 0.5


# ── Utilidades ────────────────────────────────────────────────────────────────
def _load_json(path: Path) -> dict:
    if not path.exists():
        _fail(f"no existe {path}. ¿Corriste pyright con --outputjson?")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        _fail(f"{path} no es JSON válido: {exc}")
    raise AssertionError("unreachable")


def _load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {"error_total": 0, "per_file": {}, "verifytypes_min_completeness": 0.0}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def _save_baseline(baseline: dict) -> None:
    BASELINE_PATH.write_text(
        json.dumps(baseline, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _fail(message: str) -> None:
    print(f"::error::{message}")
    raise SystemExit(1)


def _relative(path_str: str) -> str:
    """Ruta relativa al repo y con separadores POSIX, para que el baseline sea portable."""
    try:
        return Path(path_str).resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return Path(path_str).as_posix()


# ── errors: pyright --outputjson ──────────────────────────────────────────────
def check_errors(report_path: Path, *, update: bool) -> int:
    report = _load_json(report_path)
    diagnostics = report.get("generalDiagnostics", [])

    actual: Counter[str] = Counter()
    for diagnostic in diagnostics:
        if diagnostic.get("severity") != "error":
            continue
        actual[_relative(diagnostic["file"])] += 1

    total = sum(actual.values())

    if update:
        _save_baseline(
            {
                **_load_baseline(),
                "error_total": total,
                "per_file": dict(sorted(actual.items())),
            }
        )
        print(f"baseline actualizado: {total} error(es) en {len(actual)} archivo(s).")
        return 0

    baseline = _load_baseline()
    budget: dict[str, int] = baseline.get("per_file", {})
    limite_total: int = baseline.get("error_total", 0)

    regresiones: list[str] = []
    mejoras: list[str] = []

    for path, count in sorted(actual.items()):
        presupuesto = budget.get(path)
        if presupuesto is None:
            # Archivo nuevo (o renombrado) con errores: no hereda deuda de nadie.
            regresiones.append(
                f"{path}: {count} error(es) y no está en el baseline. Un archivo nuevo "
                f"arranca en cero — la deuda vieja se tolera, la nueva no."
            )
        elif count > presupuesto:
            regresiones.append(
                f"{path}: {count} error(es), el baseline permite {presupuesto}."
            )
        elif count < presupuesto:
            mejoras.append(f"{path}: bajó de {presupuesto} a {count}.")

    for path, presupuesto in sorted(budget.items()):
        if path not in actual and presupuesto > 0:
            mejoras.append(f"{path}: quedó en cero (el baseline permitía {presupuesto}).")

    for linea in mejoras:
        print(f"::notice::{linea}")

    if regresiones:
        for linea in regresiones:
            print(f"::error::{linea}")
        print(
            f"\nEl tipado empeoró: {total} error(es) contra un baseline de {limite_total}.\n"
            "Arreglá lo de arriba. Si el cambio es intencional y el número global baja, "
            "regenerá el baseline:\n\n"
            "    uv run pyright --outputjson hexcore > pyright.json\n"
            "    uv run python scripts/typing_ratchet.py errors --report pyright.json --update\n"
        )
        return 1

    if total > limite_total:
        _fail(
            f"el total ({total}) supera el baseline ({limite_total}) sin que ningún "
            f"archivo individual se pase. Revisá si el baseline quedó inconsistente."
        )

    if mejoras:
        print(
            f"\nTipado OK: {total} error(es), baseline {limite_total}. "
            f"Bajó en {len(mejoras)} archivo(s) — bajá el baseline con `--update`."
        )
    else:
        print(f"Tipado OK: {total} error(es), baseline {limite_total}.")
    return 0


# ── types: pyright --verifytypes --outputjson ─────────────────────────────────
def check_types(report_path: Path, *, update: bool) -> int:
    report = _load_json(report_path)
    completeness = report.get("typeCompleteness")
    if completeness is None:
        _fail(
            "el reporte no trae `typeCompleteness`. `--verifytypes` necesita el paquete "
            "instalado con su `py.typed` (PEP 561): instalá la wheel, no el árbol."
        )
        raise AssertionError("unreachable")

    score = round(float(completeness.get("completenessScore", 0.0)) * 100, 2)
    baseline = _load_baseline()
    minimo = float(baseline.get("verifytypes_min_completeness", 0.0))

    if update:
        _save_baseline({**baseline, "verifytypes_min_completeness": score})
        print(f"baseline de completeness actualizado a {score}%.")
        return 0

    if score < minimo - COMPLETENESS_DEAD_BAND:
        _fail(
            f"la completeness de tipos bajó a {score}% (mínimo {minimo}%). Algún símbolo "
            f"público perdió su tipo. Mirá `verifytypes.json` para ver cuál."
        )
        return 1

    if score > minimo + COMPLETENESS_DEAD_BAND:
        print(
            f"::notice::La completeness subió de {minimo}% a {score}%. "
            f"Subí el mínimo con `--update`."
        )

    print(f"Completeness OK: {score}% (mínimo {minimo}%).")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["errors", "types"])
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument(
        "--update",
        action="store_true",
        help="Reescribe el baseline con lo medido. Nunca se usa en CI: el baseline se "
        "mueve en un PR humano.",
    )
    args = parser.parse_args()

    if args.mode == "errors":
        return check_errors(args.report, update=args.update)
    return check_types(args.report, update=args.update)


if __name__ == "__main__":
    sys.exit(main())
