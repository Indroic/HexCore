"""
Submódulo de autenticación y permisos del kernel. **Deprecado: lo reemplaza `hexcore.darwin`.**

Los dos nombres que exporta quedaron obsoletos por lo que les falta, no por su nombre:

- `TokenClaims` **no tiene `sid`**, así que una sesión emitida con él no se puede revocar: no hay
  a qué fila apuntar. Tampoco tiene `aud`, `nbf` ni `typ` —o sea que un refresh token se puede
  presentar donde se espera un access token— tiene `client_id` obligatorio para un caso de uso que
  casi nadie tiene, y un default mutable en `scopes`. Lo reemplaza
  `hexcore.darwin.AccessTokenClaims`.
- `PermissionsRegistry` era un `dict[str, str]` con métodos alrededor: sin jerarquía, sin
  comodines, sin detección de ciclos. Lo reemplaza `hexcore.darwin.RoleRegistry`, que resuelve
  herencia transitiva y rechaza un ciclo al registrarlo en vez de al consultarlo.

**No hay migración automática**: los tipos no son intercambiables, así que un alias devolvería algo
con otros campos y rompería en la línea siguiente. Lo que hay es el aviso, y dos majors de margen.

Se elimina en la versión que indica `hexcore._deprecation.REMOVED_IN`.
"""
from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:
    # Sólo para el checker: en runtime los sirve el `__getattr__` de abajo, que avisa. Sin esto,
    # pyright reporta `reportUnsupportedDunderAll` porque los nombres están en `__all__` y no en
    # el módulo — y silenciarlo con un ignore esconde el caso real de un nombre mal escrito.
    from .permissions import PermissionsRegistry as PermissionsRegistry
    from .value_objects import TokenClaims as TokenClaims

from hexcore._deprecation import deprecated_lazy_names

__all__ = [
    "PermissionsRegistry",
    "TokenClaims",
]

_DEPRECADOS = {
    "PermissionsRegistry": "hexcore.darwin.RoleRegistry",
    "TokenClaims": "hexcore.darwin.AccessTokenClaims",
}


def _cargar_permissions_registry() -> t.Any:
    from .permissions import PermissionsRegistry

    return PermissionsRegistry


def _cargar_token_claims() -> t.Any:
    from .value_objects import TokenClaims

    return TokenClaims


# El aviso va acá y no en cada submódulo: `from hexcore.domain.auth import TokenClaims` es como
# se lo consume, y quien importe `hexcore.domain.auth.value_objects` directo está mirando los
# internos — ese caso lo cubre el aviso del paquete padre cuando pase por acá.
__getattr__ = deprecated_lazy_names(
    __name__,
    _DEPRECADOS,
    {
        "PermissionsRegistry": _cargar_permissions_registry,
        "TokenClaims": _cargar_token_claims,
    },
)
