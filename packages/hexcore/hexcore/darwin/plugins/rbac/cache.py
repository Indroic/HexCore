"""
`PermissionMatrixCache`: el `CompiledPermissionSet` de un usuario en un scope, con dos capas.

Resolverlo desde cero recorre `active_role_ids_for` y después, por cada rol, la herencia y los
permisos directos — varias consultas por decisión, y `AuthorizationEngine.decide()` corre en
**cada** request protegida. Las dos capas:

1. **LRU de proceso.** Un diccionario acotado, sin red. Cubre el caso caliente de un mismo
   worker respondiendo muchas requests del mismo usuario seguidas.
2. **`ICache`** (Redis u otro backend compartido). Cubre el caso frío: el primer request de
   ese usuario en *este* worker, cuando otro ya lo calculó.

**La clave lleva la versión adentro, y por eso no hace falta invalidar nada.** Subir
`darwin_authz_version` para un scope hace que la clave vieja quede huérfana — nadie la va a
volver a pedir, y con el tiempo el LRU la desaloja y el TTL de `ICache` la vence sola. Es el
mismo truco que ya usan las claves `darwin:authz:v{ver}:...`: versionar la clave es más simple
y más seguro que "encontrar y borrar todas las entradas viejas", que en un cache distribuido
casi nunca se puede hacer barato.
"""
from __future__ import annotations

import typing as t
from collections import OrderedDict

from hexcore.darwin.plugins.rbac.matcher import CompiledPermissionSet

if t.TYPE_CHECKING:
    from hexcore.infrastructure.cache import ICache

__all__ = ["PermissionMatrixCache"]

_NAMESPACE = "darwin:rbac"


async def _tal_vez_await(valor: t.Any) -> None:
    """`ICache.set`/`delete` están declarados sync en el puerto pero los backends reales son
    async. Mismo parche que `CacheRevocationList._tal_vez_await`."""
    if hasattr(valor, "__await__"):
        await valor


class PermissionMatrixCache:
    """
    Uso::

        cache = PermissionMatrixCache(max_local=2048, ttl=300)

        compilado = await cache.get_or_compute(
            user_id, scope_key, version,
            compute=lambda: servicio.effective_permissions(user_id, scope_key),
        )
    """

    def __init__(
        self,
        *,
        cache: "ICache | None" = None,
        max_local: int = 4096,
        ttl: int = 300,
    ) -> None:
        self._cache = cache
        self._max_local = max_local
        self._ttl = ttl
        #: LRU de proceso: `OrderedDict` con `move_to_end` en cada acceso, y se descarta el
        #: primero al superar `max_local`. No hace falta más que eso para un LRU correcto de
        #: un solo hilo lógico — `asyncio` no interrumpe entre estas líneas.
        self._local: OrderedDict[str, CompiledPermissionSet] = OrderedDict()

    def _backend(self) -> "ICache | None":
        if self._cache is not None:
            return self._cache
        try:
            from hexcore.config import LazyConfig

            return LazyConfig.get_config().cache_backend
        except Exception:
            # Sin `LazyConfig` configurado (un test unitario del cache, por ejemplo) se
            # degrada a sólo-LRU-local en vez de lanzar: la cache es una optimización, no un
            # requisito para que `decide()` funcione.
            return None

    @staticmethod
    def _clave(user_id: t.Any, scope_key: str, version: int) -> str:
        return f"{_NAMESPACE}:{scope_key}:v{version}:{user_id}"

    async def get_or_compute(
        self,
        user_id: t.Any,
        scope_key: str,
        version: int,
        *,
        compute: t.Callable[[], t.Awaitable[CompiledPermissionSet]],
    ) -> CompiledPermissionSet:
        clave = self._clave(user_id, scope_key, version)

        en_lru = self._local.get(clave)
        if en_lru is not None:
            self._local.move_to_end(clave)
            return en_lru

        backend = self._backend()
        if backend is not None:
            try:
                crudo: t.Any = await backend.get(clave)
            except Exception:
                # Fail-open acá, al revés que la revocación: una cache de **permisos
                # concedidos** que falla no es la vulnerabilidad — `AuthorizationEngine` es
                # default-deny, así que perder el atajo sólo cuesta latencia, nunca seguridad.
                crudo = None
            if crudo is not None:
                compilado = _desde_json(crudo)
                self._guardar_local(clave, compilado)
                return compilado

        compilado = await compute()
        self._guardar_local(clave, compilado)
        if backend is not None:
            await _tal_vez_await(backend.set(clave, _a_json(compilado), expire=self._ttl))
        return compilado

    def _guardar_local(self, clave: str, compilado: CompiledPermissionSet) -> None:
        self._local[clave] = compilado
        self._local.move_to_end(clave)
        while len(self._local) > self._max_local:
            self._local.popitem(last=False)

    def clear_local(self) -> None:
        """Vacía el LRU de proceso. Para tests."""
        self._local.clear()


def _a_json(compilado: CompiledPermissionSet) -> dict[str, t.Any]:
    return {
        "exact": sorted(compilado.exact),
        "wildcard_prefixes": sorted(compilado.wildcard_prefixes),
        "has_global_wildcard": compilado.has_global_wildcard,
    }


def _desde_json(crudo: t.Any) -> CompiledPermissionSet:
    return CompiledPermissionSet(
        exact=frozenset(crudo.get("exact", ())),
        wildcard_prefixes=frozenset(crudo.get("wildcard_prefixes", ())),
        has_global_wildcard=bool(crudo.get("has_global_wildcard", False)),
    )
