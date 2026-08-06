"""
El gate de tipado, verificado desde la suite.

Tres cosas que antes no tenían ninguna verificación:

1. Los `.pyi` generados están **sincronizados** con el `_EXPORTS` del que salen. Un stub
   desincronizado promete símbolos que no existen, que es peor que no tener stub.
2. Las fachadas **no tipan `Any`** (`tests/typing/`, chequeado con Pyright de verdad).
3. `__all__` del stub coincide exactamente con el de runtime.

Marcados `typing` y deseleccionados por defecto: invocan Pyright, que tarda ~10 s. El
workflow los corre en su propio paso.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.typing

REPO_ROOT = Path(__file__).resolve().parent.parent
FACHADAS = ("cqrs", "sql", "fastapi")


def _pyright(*paths: str) -> dict:
    """Corre Pyright y devuelve su JSON, o saltea si no está instalado."""
    ejecutable = shutil.which("pyright")
    if ejecutable is None:
        pytest.skip("pyright no está en el PATH (instalá el grupo dev)")

    resultado = subprocess.run(
        [ejecutable, "--outputjson", *paths],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    # Pyright sale con 1 cuando hay errores, así que el returncode no sirve de veredicto:
    # el veredicto lo da el contenido del reporte.
    if not resultado.stdout.strip():
        pytest.fail(f"pyright no devolvió JSON. stderr:\n{resultado.stderr[-2000:]}")
    return json.loads(resultado.stdout)


# ── 1. Los stubs no derivaron ─────────────────────────────────────────────────
def test_los_stubs_estan_sincronizados_con_exports():
    """
    Mismo chequeo que el job `stubs-drift`, corriendo local.

    Que esté en la suite significa que quien edite un `_EXPORTS` se entera al correr los
    tests, no recién en el PR.
    """
    resultado = subprocess.run(
        [sys.executable, "scripts/gen_stubs.py", "--check"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    assert resultado.returncode == 0, (
        "los .pyi están desincronizados con su _EXPORTS. Regeneralos con "
        f"`python scripts/gen_stubs.py --write`.\n\n{resultado.stdout}"
    )


@pytest.mark.parametrize("modulo", FACHADAS)
def test_existe_un_stub_por_fachada(modulo: str):
    assert (REPO_ROOT / "hexcore" / f"{modulo}.pyi").is_file()


@pytest.mark.parametrize("modulo", FACHADAS)
def test_el_all_del_stub_coincide_con_el_de_runtime(modulo: str):
    """
    El `__all__` del stub es literal y el de runtime es `sorted(_EXPORTS)`. Si divergen, el
    checker y el intérprete no están de acuerdo sobre qué exporta el módulo.
    """
    import ast
    import importlib

    en_runtime = list(importlib.import_module(f"hexcore.{modulo}").__all__)

    arbol = ast.parse((REPO_ROOT / "hexcore" / f"{modulo}.pyi").read_text(encoding="utf-8"))
    en_el_stub = None
    for nodo in arbol.body:
        if (
            isinstance(nodo, ast.Assign)
            and isinstance(nodo.targets[0], ast.Name)
            and nodo.targets[0].id == "__all__"
        ):
            en_el_stub = ast.literal_eval(nodo.value)

    assert en_el_stub is not None, f"hexcore/{modulo}.pyi no declara __all__"
    assert en_el_stub == en_runtime


@pytest.mark.parametrize("modulo", FACHADAS)
def test_el_stub_no_declara_getattr(modulo: str):
    """
    Un `def __getattr__(name: str) -> Any` en el stub haría que Pyright acepte cualquier
    atributo del módulo, y se pierde la detección de typos — la mitad de la razón para
    tener el stub.

    Se busca la **declaración** en el AST, no la subcadena: la cabecera generada menciona
    `__getattr__` en prosa para explicar por qué existe el stub.
    """
    import ast

    arbol = ast.parse((REPO_ROOT / "hexcore" / f"{modulo}.pyi").read_text(encoding="utf-8"))

    declarados = {
        nodo.name
        for nodo in arbol.body
        if isinstance(nodo, (ast.FunctionDef, ast.AsyncFunctionDef))
    } | {
        nodo.targets[0].id
        for nodo in arbol.body
        if isinstance(nodo, ast.Assign) and isinstance(nodo.targets[0], ast.Name)
    }

    assert "__getattr__" not in declarados


# ── 2. Las fachadas no tipan Any ──────────────────────────────────────────────
def test_las_fachadas_no_tipan_any():
    """
    Pyright sobre `tests/typing/`, exigiendo cero errores.

    Los `assert_type(Base, type[Base])` de ahí fallan si el símbolo es `Any`, que es
    exactamente el estado anterior a los stubs generados.
    """
    reporte = _pyright("tests/typing")

    errores = [d for d in reporte["generalDiagnostics"] if d["severity"] == "error"]
    detalle = "\n".join(
        f"  {Path(d['file']).name}:{d['range']['start']['line'] + 1} "
        f"[{d.get('rule', 'sin-regla')}] {d['message'].splitlines()[0]}"
        for d in errores
    )

    assert not errores, (
        f"{len(errores)} error(es) de tipo en tests/typing/. Si un `assert_type` falla "
        f"porque el símbolo es `Any`, revisá que los .pyi estén generados:\n\n{detalle}"
    )


def test_el_ratchet_no_esta_en_regresion():
    """
    El mismo veredicto que el job `typecheck`, corriendo local sobre el baseline commiteado.
    """
    reporte_json = REPO_ROOT / "pyright.json"
    reporte = _pyright("hexcore")
    reporte_json.write_text(json.dumps(reporte), encoding="utf-8")

    try:
        resultado = subprocess.run(
            [
                sys.executable,
                "scripts/typing_ratchet.py",
                "errors",
                "--report",
                str(reporte_json),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    finally:
        reporte_json.unlink(missing_ok=True)

    assert resultado.returncode == 0, (
        f"el tipado empeoró respecto de typing-baseline.json:\n\n{resultado.stdout}"
    )
