"""
Configuración declarativa de CQRS integrada en ServerConfig.
"""
from __future__ import annotations

import typing as t

from pydantic import BaseModel, ConfigDict, Field


class BusConfig(BaseModel):
    """
    Configuración de un bus individual (command, query o event).

    `middlewares` sólo admite middlewares construibles sin argumentos: se instancian con
    `cls()`. Los que necesitan configuración —`TransactionMiddleware` y su `uow_factory`,
    `RetryMiddleware` y su política— hay que instanciarlos a mano y pasar el
    `MiddlewarePipeline` al bus.

    `options` se reenvía al **bus**, no a los middlewares.
    """

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
                    ],
                    options={"queue_name": "hexcore_commands"},
                ),
            ),
        )

    Nota: `options` se le pasa al **bus**, no a los middlewares. Un middleware que
    necesite configuración se instancia a mano y se le pasa el pipeline al bus.
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
