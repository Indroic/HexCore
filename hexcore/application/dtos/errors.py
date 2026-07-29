"""
Errores de validación de campos de query, con datos estructurados.

`ValueError("Campo de orden no soportado: nope")` sólo se puede mostrar; el cliente no
puede señalar el input concreto ni ofrecer las alternativas. Esta subclase lleva `field` y
`allowed`, y sigue siendo un `ValueError`, así que todo el código que ya lo capturaba
—incluidos los handlers de F5— sigue funcionando.
"""
from __future__ import annotations

import typing as t

__all__ = ["UnsupportedQueryFieldError"]


class UnsupportedQueryFieldError(ValueError):
    """Un campo de filtro, orden o búsqueda que el modelo no expone."""

    def __init__(
        self,
        field: str,
        context: str,
        allowed: t.Iterable[str] | None = None,
    ) -> None:
        self.field = field
        self.context = context
        self.allowed: list[str] = sorted(allowed) if allowed is not None else []
        # Se conserva el mensaje anterior palabra por palabra: hay tests y logs que lo
        # buscan, y cambiarlo no aporta nada.
        super().__init__(f"Campo de {context} no soportado: {field}")
