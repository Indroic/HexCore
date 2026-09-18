"""
`CompiledPermissionSet`: un conjunto de permisos ya resuelto, listo para preguntarle `grants()`
sin recorrer `Permission.grants` uno por uno.

`RoleRegistry.has_permission` hace exactamente ese recorrido — `for concedido in ...: if
Permission(value=concedido).grants(required)` — y para un rol con pocos permisos declarados
está perfecto. El problema aparece cuando los permisos de un usuario salen de **resolver varios
roles con scope** (Fase F2): la unión puede tener decenas de entradas, y evaluarla en cada
`AuthorizationEngine.decide()` de cada request es plata que no hace falta gastar dos veces.

La compilación separa las entradas en tres formas, cada una con su propio costo de consulta:

- **Exactas** (`"invoice.read"`): un `frozenset`, O(1).
- **Comodín** (`"invoice.*"` → prefijo `"invoice"`): un `frozenset` de prefijos, y `grants`
  recorre los prefijos de lo pedido —de más específico a más general— en vez de recorrer los
  comodines guardados. O(profundidad de lo pedido), que está acotada por el separador `.`.
- **Comodín total** (`"*"`): un booleano.

La semántica es la misma que `Permission.grants`, letra por letra — incluido que un comodín
**no** concede el nodo pelado (`"users.*"` no concede `"users"`) — porque este módulo no inventa
un lenguaje de permisos nuevo, sólo precompila el que ya existe.
"""
from __future__ import annotations

import typing as t
from dataclasses import dataclass

from hexcore.darwin.domain.permissions import SEPARATOR, WILDCARD

__all__ = ["CompiledPermissionSet"]


@dataclass(frozen=True, slots=True)
class CompiledPermissionSet:
    """
    Uso::

        compilado = CompiledPermissionSet.compile({"users.*", "invoice.read"})
        compilado.grants("users.invite")     # True, por el comodín
        compilado.grants("invoice.read")     # True, exacto
        compilado.grants("invoice.approve")  # False
        compilado.grants("users")            # False: el comodín no concede el nodo pelado
    """

    exact: frozenset[str]
    #: Los prefijos de comodín, **sin** el `.*` final: `"users.*"` queda `"users"`.
    wildcard_prefixes: frozenset[str]
    has_global_wildcard: bool

    @classmethod
    def compile(cls, permissions: t.Iterable[str]) -> "CompiledPermissionSet":
        """Precompila un conjunto de permisos en bruto (la forma de `Permission.value`)."""
        sufijo = f"{SEPARATOR}{WILDCARD}"

        exact: set[str] = set()
        prefixes: set[str] = set()
        global_wildcard = False

        for permiso in permissions:
            if permiso == WILDCARD:
                global_wildcard = True
            elif permiso.endswith(sufijo):
                prefixes.add(permiso[: -len(sufijo)])
            else:
                exact.add(permiso)

        return cls(
            exact=frozenset(exact),
            wildcard_prefixes=frozenset(prefixes),
            has_global_wildcard=global_wildcard,
        )

    def grants(self, required: str) -> bool:
        """Si este conjunto concede `required`."""
        if self.has_global_wildcard:
            return True
        if required in self.exact:
            return True
        if not self.wildcard_prefixes:
            return False

        # Los prefijos de `required`, de más específico a más general: "a.b.c" prueba "a.b" y
        # después "a", nunca "a.b.c" entero (un comodín no concede el nodo pelado). A lo sumo
        # profundidad(required) chequeos en un `frozenset`, en vez de un `fnmatch` por comodín
        # guardado — que es exactamente lo que este módulo existe para evitar.
        partes = required.split(SEPARATOR)
        for i in range(len(partes) - 1, 0, -1):
            if SEPARATOR.join(partes[:i]) in self.wildcard_prefixes:
                return True
        return False

    def __bool__(self) -> bool:
        # Explícito, mismo criterio que `PluginRegistry`: un conjunto vacío no debería leerse
        # como falsy por accidente en un `if compilado:` que quería decir "existe".
        return True
