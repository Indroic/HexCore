"""
Los pisos anti-verde-falso, verificados desde la suite.

`house_rules.py` y `typing_ratchet.py` recorren directorios con `rglob`/analizan un
`include` de pyright. Ninguna de las dos operaciones lanza si el directorio de destino está
vacío o no existe: `rglob` devuelve cero resultados, pyright analiza cero archivos, y ambos
scripts imprimían "en verde" habiendo revisado nada. Es exactamente el modo de falla que una
reorganización de directorios (por ejemplo, un monorepo) dispara sin romper ningún test —
hasta este archivo, nada afirmaba que las anclas resuelven a *algo*.

Este archivo no reemplaza los pisos de los scripts (`PISO_DE_ARCHIVOS`,
`min_files_analyzed`): los complementa desde la suite normal, que corre en cada PR sin
invocar pyright ni pedir un JSON de reporte. Es `Path.exists()` y `rglob`, milisegundos.
"""
from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
PAQUETE = REPO_ROOT / "hexcore"

#: Los mismos 11 archivos de §1.2 del plan de monorepo: 3 scripts + 8 tests que anclan con
#: `Path(__file__).resolve().parent.parent`. Si uno de estos deja de anclar correctamente,
#: el archivo pasa a apuntar a un directorio ajeno sin que ningún import falle.
ARCHIVOS_CON_ANCLA = (
    "scripts/gen_stubs.py",
    "scripts/house_rules.py",
    "scripts/stub_quality.py",
    "scripts/typing_ratchet.py",
    "tests/test_darwin_backend_neutrality.py",
    "tests/test_darwin_cli.py",
    "tests/test_darwin_models.py",
    "tests/test_darwin_plugin_decoupling.py",
    "tests/test_deprecations.py",
    "tests/test_documentation_examples.py",
    "tests/test_packaging.py",
    "tests/test_typing_gate.py",
)


class TestLasAnclasDelRepoResuelvenAAlgo:
    def test_cada_archivo_con_ancla_existe(self) -> None:
        faltantes = [
            ruta for ruta in ARCHIVOS_CON_ANCLA if not (REPO_ROOT / ruta).is_file()
        ]
        assert not faltantes, (
            f"estos archivos ya no existen donde el inventario de anclas dice: {faltantes}. "
            "Si se movieron, actualizá ARCHIVOS_CON_ANCLA junto con la mudanza."
        )

    def test_el_paquete_hexcore_tiene_al_menos_doscientos_modulos(self) -> None:
        """
        Espejo de `PISO_DE_ARCHIVOS` en `scripts/house_rules.py`: si `PAQUETE` deja de
        apuntar al paquete real, este test lo dice con un `AssertionError` legible en vez de
        dejar que `house_rules.py` (que sólo corre en CI) sea la única defensa.
        """
        modulos = [
            p
            for p in PAQUETE.rglob("*.py")
            if "__pycache__" not in p.parts
        ]
        assert len(modulos) >= 200, (
            f"sólo hay {len(modulos)} módulo(s) .py bajo {PAQUETE} — ¿sigue siendo la ruta "
            "del paquete? (Con cero o pocos módulos, `house_rules.py` pasaría en verde sin "
            "revisar nada.)"
        )

    def test_el_include_de_pyright_apunta_a_un_directorio_con_codigo(self) -> None:
        pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        includes = pyproject["tool"]["pyright"]["include"]
        assert includes, "`[tool.pyright].include` está vacío: pyright no analizaría nada."

        for entrada in includes:
            directorio = REPO_ROOT / entrada
            modulos = [
                p for p in directorio.rglob("*.py") if "__pycache__" not in p.parts
            ]
            assert len(modulos) >= 200, (
                f"`[tool.pyright].include` apunta a {entrada!r}, que sólo tiene "
                f"{len(modulos)} módulo(s) .py. Con un `include` roto, el ratchet de tipado "
                "mediría cero errores contra un baseline positivo y pasaría en verde para "
                "siempre."
            )

    def test_toda_clave_del_baseline_de_tipado_existe_en_disco(self) -> None:
        """
        Cada clave de `per_file` en `typing-baseline.json` es una ruta relativa al repo. Si
        el baseline quedó describiendo un árbol que ya no existe (por ejemplo, tras un
        `git mv` masivo sin actualizar `_relative()`), cada archivo real caería en la rama de
        "archivo nuevo" del ratchet — que sí falla, pero con un mensaje que no explica la
        causa raíz. Esta aserción la nombra directo.
        """
        baseline_path = REPO_ROOT / "typing-baseline.json"
        if not baseline_path.exists():
            pytest.skip("no hay typing-baseline.json en este árbol")

        import json

        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        claves = baseline.get("per_file", {})
        faltantes = [clave for clave in claves if not (REPO_ROOT / clave).is_file()]
        assert not faltantes, (
            f"estas claves de `typing-baseline.json` no resuelven a un archivo real: "
            f"{faltantes}. El baseline probablemente describe rutas de antes de una "
            "reorganización — regeneralo con "
            "`scripts/typing_ratchet.py errors --report pyright.json --update`."
        )
