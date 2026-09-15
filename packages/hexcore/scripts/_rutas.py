"""
Ancla de rutas para los scripts de mantenimiento.

Antes de la migración a monorepo, `Path(__file__).resolve().parent.parent` significaba "la
raíz del repo" en cada script de `scripts/`. Tras mover `scripts/` a `packages/hexcore/scripts/`
junto con el resto del paquete, esa misma expresión sigue resolviendo al lugar correcto —
`packages/hexcore/`—, pero ahora significa "la raíz del paquete". Es el punto central de §1.1
del plan de monorepo: mover el código y sus anclas juntos hace que la constante apunte bien
sin tocar el cálculo.

Se centraliza acá, en vez de dejar que cada script recalcule lo mismo, por dos razones: evita
que un script nuevo copie el cálculo con un `parent` de más o de menos, y da un solo lugar
donde subir hasta la raíz del **monorepo** (`REPO`) para el único caso que la necesita
(`tests/test_packaging.py`, que lee un workflow bajo `.github/`).

Cada constante se verifica al importar el módulo. Sin la verificación, un anclaje que apunta a
un directorio inexistente —por ejemplo, tras otra reorganización futura— es un bug silencioso
hasta que algún `rglob()` sobre él encuentra cero archivos y el script que lo usa "pasa" sin
haber revisado nada.
"""
from __future__ import annotations

from pathlib import Path

#: La raíz del paquete Python: el directorio donde vive `packages/hexcore/pyproject.toml`.
PAQUETE = Path(__file__).resolve().parent.parent

#: El código fuente del paquete — el directorio importable `hexcore/`.
FUENTE = PAQUETE / "hexcore"

#: La raíz del monorepo (dos niveles arriba de `PAQUETE`: `packages/hexcore` → `packages` →
#: la raíz). Sólo la necesita `tests/test_packaging.py`.
REPO = PAQUETE.parent.parent

for _ruta, _nombre in ((PAQUETE, "PAQUETE"), (FUENTE, "FUENTE"), (REPO, "REPO")):
    if not _ruta.is_dir():
        raise RuntimeError(
            f"_rutas.{_nombre} no resuelve a un directorio: {_ruta}. "
            "¿Se movió este archivo sin actualizar el cálculo?"
        )

del _ruta, _nombre
