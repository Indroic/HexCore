"""
Fase 0: lo que el árbol promete tiene que llegar al artefacto.

Dos fallas que eran invisibles desde el repo y sólo se veían en la wheel:

1. **`packages.find` sin `include`.** Con `where = ["."]` y nada más, setuptools tomaba
   como paquetes top-level todo directorio con `__init__.py`: la wheel declaraba
   ``top_level.txt = dist, hexcore, refs, scripts, tests``, así que
   ``pip install hexcore`` te dejaba un paquete `tests` y un paquete `scripts` en
   site-packages — y `dist`, que es el directorio de salida del build anterior.
2. **`py.typed` y los `.pyi` sin `package-data`.** `MANIFEST.in` los incluye, pero
   `MANIFEST.in` gobierna el **sdist**. Que el marcador PEP 561 esté en el repo no sirve
   de nada si no está en la wheel: sin él, Pyright y mypy ignoran los tipos del paquete
   por completo.

Estos tests construyen la wheel de verdad, porque es el único lugar donde el bug se ve.
Van marcados `packaging` y deseleccionados por defecto: construir cuesta unos segundos y
no tiene sentido pagarlos en cada corrida.
"""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.packaging

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> zipfile.ZipFile:
    """Construye la wheel en un directorio temporal y la devuelve abierta."""
    outdir = tmp_path_factory.mktemp("wheel")
    result = subprocess.run(
        [sys.executable, "-m", "build", "--wheel", "--outdir", str(outdir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.skip(
            "no se pudo construir la wheel (¿falta `build` en el entorno?): "
            f"{result.stderr[-500:]}"
        )

    wheels = list(outdir.glob("*.whl"))
    assert len(wheels) == 1, f"se esperaba una wheel, se encontraron {wheels}"
    return zipfile.ZipFile(wheels[0])


def _names(wheel: zipfile.ZipFile) -> list[str]:
    return sorted(wheel.namelist())


# ── PEP 561 ───────────────────────────────────────────────────────────────────
def test_la_wheel_lleva_el_marcador_py_typed(wheel: zipfile.ZipFile):
    """Sin esto, todo el trabajo de tipado es invisible para el consumidor."""
    assert "hexcore/py.typed" in _names(wheel)


def test_la_wheel_lleva_todos_los_pyi_del_arbol(wheel: zipfile.ZipFile):
    """
    Cada `.pyi` del repo tiene que estar en la wheel.

    Hoy no hay ninguno, así que el test pasa trivialmente — y eso es a propósito: cuando
    se agreguen los stubs generados de las fachadas, este test ya está puesto y falla si
    `package-data` no los incluye.
    """
    en_el_arbol = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "hexcore").rglob("*.pyi")
    }
    en_la_wheel = {name for name in _names(wheel) if name.endswith(".pyi")}

    assert en_el_arbol == en_la_wheel


# ── Paquetes publicados ───────────────────────────────────────────────────────
def test_la_wheel_solo_publica_hexcore(wheel: zipfile.ZipFile):
    """La regresión concreta: `tests`, `scripts`, `refs` y `dist` no son API pública."""
    top_level = {
        name.split("/")[0]
        for name in _names(wheel)
        if "/" in name and not name.endswith(".dist-info")
    }
    intrusos = top_level - {"hexcore"} - {
        n for n in top_level if n.endswith(".dist-info")
    }

    assert intrusos == set(), f"la wheel publica paquetes que no son parte de la API: {intrusos}"


def test_top_level_txt_declara_solo_hexcore(wheel: zipfile.ZipFile):
    entradas = [n for n in _names(wheel) if n.endswith("top_level.txt")]
    assert entradas, "la wheel no trae top_level.txt"

    declarados = wheel.read(entradas[0]).decode().split()
    assert declarados == ["hexcore"]


def test_todo_modulo_del_arbol_esta_en_la_wheel(wheel: zipfile.ZipFile):
    """
    `include = ["hexcore*"]` no puede haber recortado un subpaquete real.

    Es el riesgo del fix anterior: al pasar de "todo" a un patrón, un subpaquete que no
    matchee desaparece silenciosamente de la distribución.
    """
    en_el_arbol = {
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "hexcore").rglob("*.py")
    }
    en_la_wheel = {n for n in _names(wheel) if n.endswith(".py")}

    faltantes = en_el_arbol - en_la_wheel
    assert faltantes == set(), f"la wheel no incluye estos módulos: {sorted(faltantes)}"


# ── El paquete importado es el del repo ───────────────────────────────────────
def test_hexcore_se_importa_desde_el_repo_y_no_de_site_packages():
    """
    Declarar `[build-system]` hace que `uv sync` instale el proyecto, así que ahora hay
    dos candidatos en `sys.path`. Se instala editable, o sea que tiene que seguir
    resolviendo al árbol: si resolviera a una copia en site-packages, los tests estarían
    corriendo contra código viejo y un `isinstance` entre las dos copias fallaría.
    """
    import hexcore

    assert hexcore.__file__ is not None
    resuelto = Path(hexcore.__file__).resolve()
    assert resuelto.is_relative_to(REPO_ROOT), (
        f"`import hexcore` resolvió a {resuelto}, que está fuera del repo"
    )
