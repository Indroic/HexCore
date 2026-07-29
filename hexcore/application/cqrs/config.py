"""
Configuración declarativa de CQRS integrada en ServerConfig.
"""
from __future__ import annotations

import typing as t

from pydantic import BaseModel, ConfigDict, Field


class MiddlewareConfig(BaseModel):
    """Configuración de un middleware individual."""

    enabled: bool = True
    order: int = 0  # Menor = se ejecuta primero
    options: dict[str, t.Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)


class BusConfig(BaseModel):
    """Configuración de un bus individual (command, query o event)."""

    # Clase del bus a instanciar (dotted path o referencia directa)
    # Si es None, usa el bus in-memory por defecto
    backend: t.Optional[str] = None
    middlewares: list[str] = Field(default_factory=list)
    options: dict[str, t.Any] = Field(default_factory=dict)

    model_config = ConfigDict(frozen=True)


class CQRSConfig(BaseModel):
    """
    Configuración global de CQRS para Hexcore.
    Se integra como campo opcional en ServerConfig.

    Ejemplo en config.py del proyecto::

        from hexcore.config import ServerConfig
        from hexcore.application.cqrs.config import CQRSConfig, BusConfig

        config = ServerConfig(
            cqrs=CQRSConfig(
                command_bus=BusConfig(
                    backend="hexcore.infrastructure.cqrs.procrastinate.ProcrastinateCommandBus",
                    middlewares=[
                        "hexcore.infrastructure.cqrs.middlewares.LoggingMiddleware",
                        "hexcore.infrastructure.cqrs.middlewares.RetryMiddleware",
                    ],
                    options={"max_retries": 3},
                ),
            ),
        )
    """

    enabled: bool = True
    # Sin middlewares por defecto (P0-6). `TransactionMiddleware` *era* el default,
    # pero adivinaba la sesión con el session factory interno de HexCore en vez del
    # engine de la aplicación, y comiteaba por segunda vez sobre los handlers que ya
    # gestionan su transacción. Si lo querés, declaralo explícitamente con su
    # `uow_factory`.
    command_bus: BusConfig = Field(default_factory=BusConfig)
    query_bus: BusConfig = Field(default_factory=BusConfig)
    event_bus: BusConfig = Field(default_factory=BusConfig)
    serializer: t.Optional[str] = None  # None = PydanticSerializer por defecto

    model_config = ConfigDict(arbitrary_types_allowed=True)
