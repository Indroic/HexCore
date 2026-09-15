"""
`InMemoryEventBus` — deprecado en 9.0, se elimina en 10.0.

HexCore tenía dos clases con este nombre, colgadas de dos puertos incompatibles: ésta, un
diccionario de handlers y poco más, y la de `hexcore.application.cqrs.in_memory_buses`, que
además tiene pipeline de middlewares y Smart Routing hacia las colas de background. Gana la
segunda, que es un superconjunto estricto de esta.

El paquete `hexcore.infrastructure.events` entero se elimina en 10.0: no contiene nada más.

El alias es perezoso —no un `from ... import` arriba— por dos motivos: para no cargar el
módulo de CQRS en cada import de este, y sobre todo para que **importar este módulo no emita
el aviso**. Si avisara al importar, el usuario no podría saber quién usa el nombre viejo, y
el aviso se volvería ruido que se aprende a ignorar. Lo verifica
`tests/test_deprecations.py::test_importar_no_avisa`.
"""
from __future__ import annotations

import typing as t

from hexcore._deprecation import deprecated_lazy_names

if t.TYPE_CHECKING:
    from hexcore.application.cqrs.in_memory_buses import InMemoryEventBus as InMemoryEventBus

__all__ = ["InMemoryEventBus"]


def _cargar_bus_en_memoria() -> t.Any:
    from hexcore.application.cqrs.in_memory_buses import InMemoryEventBus

    return InMemoryEventBus


__getattr__ = deprecated_lazy_names(
    __name__,
    {"InMemoryEventBus": "hexcore.cqrs.InMemoryEventBus"},
    {"InMemoryEventBus": _cargar_bus_en_memoria},
    since="9.0",
)
