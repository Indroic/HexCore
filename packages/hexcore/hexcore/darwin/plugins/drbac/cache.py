"""
`PolicySetCache`: las reglas ya compiladas de un scope, versionadas.

**Sólo LRU de proceso, a diferencia de `rbac.cache.PermissionMatrixCache`.** Lo que se cachea
acá no es un dato serializable como un `CompiledPermissionSet` (exactos + prefijos, JSON-safe):
es una tupla de reglas **ya compiladas a closures Python** (`conditions.compile_condition`), y
una closure no cruza a Redis. Compartir esta cache entre workers obligaría a volver a compilar
del lado del que la recibe de todos modos, así que no hay nada que ganar agregando una capa de
red — cada worker compila una vez y listo.

**La clave lleva la versión adentro**, igual que `PermissionMatrixCache`: subir
`AbstractDrbacAuthzVersionRepository` para un scope deja la clave vieja huérfana, y el LRU la
desaloja sola con el tiempo. No hace falta invalidar nada a mano.
"""
from __future__ import annotations

import typing as t
from collections import OrderedDict

if t.TYPE_CHECKING:
    from hexcore.darwin.plugins.drbac.pdp import CompiledRule

__all__ = ["PolicySetCache"]


class PolicySetCache:
    """
    Uso::

        cache = PolicySetCache(max_local=2048)

        reglas = await cache.get_or_compute(
            scope_key, version, compute=lambda: pdp._compilar_capa(scope_key),
        )
    """

    def __init__(self, *, max_local: int = 4096) -> None:
        self._max_local = max_local
        self._local: OrderedDict[str, tuple["CompiledRule", ...]] = OrderedDict()

    @staticmethod
    def _clave(scope_key: str, version: int) -> str:
        return f"darwin:drbac:{scope_key}:v{version}"

    async def get_or_compute(
        self,
        scope_key: str,
        version: int,
        *,
        compute: t.Callable[[], t.Awaitable[tuple["CompiledRule", ...]]],
    ) -> tuple["CompiledRule", ...]:
        clave = self._clave(scope_key, version)

        en_lru = self._local.get(clave)
        if en_lru is not None:
            self._local.move_to_end(clave)
            return en_lru

        compiladas = await compute()
        self._local[clave] = compiladas
        self._local.move_to_end(clave)
        while len(self._local) > self._max_local:
            self._local.popitem(last=False)
        return compiladas

    def clear_local(self) -> None:
        """Vacía el LRU. Para tests."""
        self._local.clear()
