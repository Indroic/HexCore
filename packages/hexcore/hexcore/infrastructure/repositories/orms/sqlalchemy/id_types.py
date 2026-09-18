"""
Hace coincidir un id con el tipo Python que espera la columna, antes de bindearlo.

`sa.Uuid(as_uuid=False)` no coacciona un `uuid.UUID` al insertar ni al comparar — revienta con
``AttributeError: 'UUID' object has no attribute 'replace'`` porque su `bind_processor` asume
que ya recibió un `str`. `BaseEntity.id` es siempre `UUID` (`hexcore/domain/base.py`), así que
cualquier repo que construya un modelo o un filtro a partir de una entidad pasa un `UUID` a una
columna que puede estar declarada `as_uuid=False` — como hacen los proyectos que, igual que
redv2, trabajan sus ids como `str` en la capa de aplicación. `sa.Uuid()` (as_uuid=True, el
default) tiene el problema simétrico en el otro sentido: acepta un `str` porque lo coacciona,
pero perpetuar `str` donde el resto del código espera `UUID` reintroduce el mismo bug más
adelante.

Este módulo no adivina la convención del proyecto: lee la columna real de `model_cls` y
convierte al tipo que esa columna declaró.
"""
from __future__ import annotations

import typing as t
from uuid import UUID

from sqlalchemy import Uuid as UuidType

__all__ = ["coerce_id_for_column", "coerce_ids_for_model"]


def coerce_id_for_column(model_cls: type, field_name: str, value: t.Any) -> t.Any:
    """Convierte `value` al tipo Python que declara la columna `field_name` de `model_cls`.

    Deja `value` intacto si la columna no existe, no es `Uuid`, o `value` es `None` — para que
    llamarlo sea seguro incluso con columnas ajenas al problema (`scope_key`, `name`, ...).
    """
    if value is None:
        return value
    table = getattr(model_cls, "__table__", None)
    if table is None:
        return value
    column = table.columns.get(field_name)
    if column is None:
        return value
    coltype = column.type
    if not isinstance(coltype, UuidType):
        return value
    if coltype.as_uuid:
        return value if isinstance(value, UUID) else UUID(str(value))
    return str(value) if isinstance(value, UUID) else value


def coerce_ids_for_model(
    model_cls: type, values: t.Mapping[str, t.Any]
) -> dict[str, t.Any]:
    """`coerce_id_for_column` sobre cada entrada de `values` — para kwargs de construcción."""
    return {
        field_name: coerce_id_for_column(model_cls, field_name, value)
        for field_name, value in values.items()
    }
