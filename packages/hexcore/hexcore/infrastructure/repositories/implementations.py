"""
Los repositorios genéricos, y el argumento común que comparten.

`HasBasicArgs` vive acá porque no necesita ningún extra. `SqlAlchemyRepository` y
`BeanieRepository` viven en módulos hoja —`_sqlalchemy_impl` y `_beanie_impl`— y este módulo los
reexporta: se resuelven al primer acceso, así que importarlo sigue funcionando sin `[sql]` y sin
`[mongo]`, que es el contrato que verifica `tests/test_optional_dependencies.py`.

**Por qué no un `try/except ImportError` con una clase vacía**, que es como estaba::

    except ImportError:
        M = t.TypeVar("M")
        class SqlAlchemyRepository(t.Generic[T, M]): ...

Funcionaba en runtime y arruinaba el tipado. Pyright analiza las dos ramas del `try` y se queda
con la última definición del nombre, o sea la vacía: el consumidor que hacía hover sobre su
repositorio no veía `save`, ni `get_by_id`, ni `model_cls`, ni `query_cursor` — y todo lo que
pasaba por ahí se propagaba como `Unknown`. Eran 64 de los errores de Pyright del paquete, y la
mayoría de los `reportUnknown*` de los módulos de más arriba.

El reparto de responsabilidades es el mismo que en cualquier otro punto del framework donde un
extra puede faltar: el módulo hoja importa lo que necesita **sin guardas** —si falta, que falle
donde está la causa— y el módulo público traduce esa falla en un error que dice qué instalar.
Acá el `__getattr__` (PEP 562) hace de traductor.

El `__getattr__` va detrás de `if not t.TYPE_CHECKING:` a propósito. Para Pyright existen los
imports de arriba, que apuntan a las clases **reales**; el `__getattr__` no llega a verlo, así
que el namespace del módulo queda cerrado y un nombre mal escrito sigue siendo un error de
tipado en vez de un `Any` silencioso. En runtime pasa al revés: los imports no se ejecutan y
resuelve el `__getattr__`.
"""
from __future__ import annotations

import typing as t

from hexcore.types import FieldResolversType, FieldSerializersType

from .base import T

if t.TYPE_CHECKING:
    from ._beanie_impl import BeanieRepository as BeanieRepository, D as D
    from ._sqlalchemy_impl import M as M, SqlAlchemyRepository as SqlAlchemyRepository

__all__ = [
    "BeanieRepository",
    "D",
    "HasBasicArgs",
    "M",
    "SqlAlchemyRepository",
]

A = t.TypeVar("A")


class HasBasicArgs(t.Generic[T, A]):
    @property
    def entity_cls(self) -> t.Type[T]:
        raise NotImplementedError("Debe implementar la propiedad entity_cls")

    @property
    def not_found_exception(self) -> t.Type[Exception]:
        raise NotImplementedError("Debe implementar la propiedad not_found_exception")

    @property
    def fields_serializers(self) -> FieldSerializersType[T]:
        """
        Serializadores para campos complejos en la conversión entre Entidad -> Documento/Modelo.
        """
        return {}

    @property
    def fields_resolvers(self) -> FieldResolversType[A]:
        """
        Resolvedores para campos complejos en la conversión entre Documento/Modelo -> Entidad.
        Debe ser implementado por cada repositorio específico.
        """
        return {}


#: Qué módulo hoja y qué paquete de tercero hay detrás de cada nombre diferido.
#:
#: Las rutas van absolutas y no relativas: `import_module("._beanie_impl", __name__)` resuelve
#: contra `__name__`, que acá es el **módulo** y no el paquete, y falla con un
#: "`...implementations` is not a package" que no señala la causa.
_DIFERIDOS: dict[str, tuple[str, str]] = {
    "SqlAlchemyRepository": (
        "hexcore.infrastructure.repositories._sqlalchemy_impl",
        "sqlalchemy",
    ),
    "M": ("hexcore.infrastructure.repositories._sqlalchemy_impl", "sqlalchemy"),
    "BeanieRepository": (
        "hexcore.infrastructure.repositories._beanie_impl",
        "beanie",
    ),
    "D": ("hexcore.infrastructure.repositories._beanie_impl", "beanie"),
}


if not t.TYPE_CHECKING:

    def __getattr__(name: str) -> t.Any:
        diferido = _DIFERIDOS.get(name)
        if diferido is None:
            # El mensaje estándar de Python, palabra por palabra. Un texto propio que hablara
            # de extras sugeriría que el nombre existe en algún modo, y `tests/test_deprecations.py`
            # exige "has no attribute" para los alias que se removieron.
            raise AttributeError(
                f"module {__name__!r} has no attribute {name!r}"
            )

        modulo, paquete = diferido
        import importlib

        from hexcore.capabilities import require_extra

        # Se pregunta **antes** de importar para que la falta del extra dé el error con la
        # remediación, y no un `ModuleNotFoundError` crudo desde tres módulos más adentro.
        require_extra(paquete, para=f"`{name}`")
        return getattr(importlib.import_module(modulo), name)
