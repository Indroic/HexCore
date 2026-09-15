"""
Abstracciones base para Queries en el patrón CQRS.
"""
from __future__ import annotations

import typing as t

from pydantic import BaseModel, ConfigDict


TResult = t.TypeVar("TResult")


class Query(BaseModel, t.Generic[TResult]):
    """
    Clase base para todas las queries.
    Una Query representa una solicitud de información sin efectos secundarios.
    El tipo genérico TResult indica el tipo de dato que retorna.
    """

    model_config = ConfigDict(
        frozen=True,
        from_attributes=True,
    )


TQuery = t.TypeVar("TQuery", bound=Query[t.Any])
