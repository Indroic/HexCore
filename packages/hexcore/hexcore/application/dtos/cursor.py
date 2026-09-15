"""
Paginación por cursor.

`limit`/`offset` degrada en tablas grandes: un `OFFSET 100000` obliga a la base a
escanear y descartar 100.000 filas antes de devolver la primera. Para los listados
grandes (tickets, líneas, movimientos) hace falta la variante por cursor, que salta
directo con un `WHERE (sort_key, id) > (...)`.

Se **añade**, no sustituye a `QueryResponseDTO`.

El cursor es opaco a propósito: codifica `(sort_key, id)` en base64url. Si fuera legible,
los clientes empezarían a construirlo a mano y quedaría congelado como API pública.
"""
from __future__ import annotations

import base64
import binascii
import json
import typing as t

from pydantic import Field

from .base import DTO
from .query import FilterConditionDTO, SortDirection

T = t.TypeVar("T")

__all__ = [
    "CursorPageDTO",
    "CursorRequestDTO",
    "encode_cursor",
    "decode_cursor",
    "InvalidCursorError",
]


class InvalidCursorError(ValueError):
    """El cursor recibido no se puede decodificar."""


class CursorPageDTO(DTO, t.Generic[T]):
    """
    Una página por cursor.

    `next_cursor` es None cuando no hay más resultados, que es la única señal de fin que
    necesita el cliente: no hay `total`, porque contar es justamente lo que esta
    paginación evita.
    """

    items: list[T] = Field(default_factory=list)
    next_cursor: str | None = None


class CursorRequestDTO(DTO):
    """Petición de una página por cursor."""

    limit: int = Field(default=50, ge=1, le=500)
    cursor: str | None = None
    sort_field: str = "created_at"
    direction: SortDirection = SortDirection.DESC
    search: str | None = None
    search_fields: list[str] = Field(default_factory=list)
    filters: list[FilterConditionDTO] = Field(default_factory=list)


def encode_cursor(sort_value: t.Any, entity_id: t.Any) -> str:
    """
    Codifica la posición de la última fila de la página.

    Se incluye el `id` además del campo de orden para desempatar: con sólo el `sort_key`,
    varias filas con el mismo `created_at` se saltarían o se repetirían.
    """
    payload = json.dumps(
        {"k": _jsonable(sort_value), "i": _jsonable(entity_id)},
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[t.Any, t.Any]:
    """
    Decodifica un cursor a `(sort_value, entity_id)`.

    Raises:
        InvalidCursorError: Si el cursor está corrupto o no tiene la forma esperada. Es
            un `ValueError`, así que los handlers de F5 lo traducen a 422.
    """
    try:
        padding = "=" * (-len(cursor) % 4)
        payload = base64.urlsafe_b64decode(cursor + padding).decode()
        data = json.loads(payload)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise InvalidCursorError(f"Cursor inválido: {cursor!r}") from exc

    if not isinstance(data, dict) or "k" not in data or "i" not in data:
        raise InvalidCursorError(f"Cursor inválido: {cursor!r}")

    return data["k"], data["i"]


def _jsonable(value: t.Any) -> t.Any:
    """Normaliza a algo serializable en JSON conservando el orden lexicográfico."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        # ISO-8601 ordena lexicográficamente igual que cronológicamente, que es lo que
        # necesita la comparación del WHERE.
        return isoformat()
    return str(value)
