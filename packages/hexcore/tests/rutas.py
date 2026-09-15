"""
Ancla de rutas para la suite de tests. Espejo de `scripts/_rutas.py`.

Es un módulo aparte y no un import cruzado con `scripts/_rutas.py` porque `scripts/` y
`tests/` no se importan entre sí sin gimnasia de `sys.path` — `tests/test_packaging.py` ya
hace un `sys.path.insert` puntual para leer `extra_smoke.py`, y esa gimnasia es justo el tipo
de cosa que se rompe callada si se generaliza. Dos módulos de ~20 líneas cada uno es más
barato que mantener esa gimnasia para todo el mundo.

Ver el docstring de `scripts/_rutas.py` para el razonamiento completo sobre por qué la
constante sigue apuntando bien tras el `git mv` sin tocar el cálculo, y por qué las rutas se
verifican al importar.
"""
from __future__ import annotations

from pathlib import Path

#: La raíz del paquete Python: el directorio donde vive `packages/hexcore/pyproject.toml`.
PAQUETE = Path(__file__).resolve().parent.parent

#: El código fuente del paquete — el directorio importable `hexcore/`.
FUENTE = PAQUETE / "hexcore"

#: La raíz del monorepo. Sólo la necesita `test_packaging.py`, que lee un workflow bajo
#: `.github/`.
REPO = PAQUETE.parent.parent

for _ruta, _nombre in ((PAQUETE, "PAQUETE"), (FUENTE, "FUENTE"), (REPO, "REPO")):
    if not _ruta.is_dir():
        raise RuntimeError(
            f"rutas.{_nombre} no resuelve a un directorio: {_ruta}. "
            "¿Se movió este archivo sin actualizar el cálculo?"
        )

del _ruta, _nombre
