"""
Abstracciones base para Commands en el patrón CQRS.
"""
from __future__ import annotations

import typing as t
from uuid import UUID, uuid4
from datetime import datetime, UTC

from pydantic import BaseModel, Field, ConfigDict


class Command(BaseModel):
    """
    Clase base para todos los comandos.
    Un Command representa una intención de modificar el estado del sistema.
    Es inmutable y serializable por defecto (Pydantic).
    """

    command_id: UUID = Field(default_factory=uuid4)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    model_config = ConfigDict(
        frozen=True,
        from_attributes=True,
    )


TCommand = t.TypeVar("TCommand", bound=Command)
TCommandResult = t.TypeVar("TCommandResult")
