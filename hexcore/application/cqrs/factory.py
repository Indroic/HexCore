"""
Factory para construir buses CQRS a partir de la configuración.
Resuelve backends, serializers y middlewares por dotted path.
"""
from __future__ import annotations

import importlib
import typing as t

from hexcore.domain.cqrs.buses import ICommandBus, IQueryBus, IEventBus
from hexcore.domain.cqrs.middleware import IMiddleware
from hexcore.domain.cqrs.serializer import ISerializer

from .config import CQRSConfig, BusConfig
from .registry import HandlerRegistry
from .pipeline import MiddlewarePipeline
from .in_memory_buses import InMemoryCommandBus, InMemoryQueryBus, InMemoryEventBus


def _import_class(dotted_path: str) -> type:
    """Importa una clase a partir de su dotted path."""
    module_path, class_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def _build_middlewares(dotted_paths: list[str]) -> list[IMiddleware]:
    """Instancia middlewares a partir de sus dotted paths."""
    middlewares: list[IMiddleware] = []
    for path in dotted_paths:
        cls = _import_class(path)
        middlewares.append(cls())
    return middlewares


def _build_pipeline(bus_config: BusConfig) -> MiddlewarePipeline:
    """Construye un MiddlewarePipeline desde la configuración de un bus."""
    middlewares = _build_middlewares(bus_config.middlewares)
    return MiddlewarePipeline(middlewares)


class CQRSFactory:
    """
    Factory que construye las instancias de buses CQRS.

    Usa CQRSConfig para determinar qué implementación de bus instanciar,
    qué middlewares configurar y qué serializer utilizar.

    Uso::

        config = CQRSConfig(...)
        registry = HandlerRegistry()
        # ...registrar handlers...

        factory = CQRSFactory(config, registry)
        command_bus = factory.create_command_bus()
        query_bus = factory.create_query_bus()
    """

    def __init__(
        self,
        config: CQRSConfig,
        registry: HandlerRegistry,
    ) -> None:
        self._config = config
        self._registry = registry

    def create_serializer(self) -> ISerializer:
        """Crea el serializer configurado (PydanticSerializer por defecto)."""
        if self._config.serializer:
            cls = _import_class(self._config.serializer)
            return cls()
        from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer

        return PydanticSerializer()

    def create_command_bus(self, **extra_kwargs: t.Any) -> ICommandBus:
        """
        Crea el CommandBus configurado.
        Si no hay backend explícito, retorna InMemoryCommandBus.
        """
        bus_config = self._config.command_bus
        pipeline = _build_pipeline(bus_config)

        if bus_config.backend is None:
            return InMemoryCommandBus(
                registry=self._registry,
                pipeline=pipeline,
            )

        # Backend personalizado (ej. ProcrastinateCommandBus)
        cls = _import_class(bus_config.backend)
        return cls(
            registry=self._registry,
            serializer=self.create_serializer(),
            pipeline=pipeline,
            **bus_config.options,
            **extra_kwargs,
        )

    def create_query_bus(self) -> IQueryBus:
        """
        Crea el QueryBus configurado.
        Nota: Las queries siempre son síncronas en CQRS puro.
        """
        bus_config = self._config.query_bus
        pipeline = _build_pipeline(bus_config)

        if bus_config.backend is None:
            return InMemoryQueryBus(
                registry=self._registry,
                pipeline=pipeline,
            )

        cls = _import_class(bus_config.backend)
        return cls(
            registry=self._registry,
            pipeline=pipeline,
            **bus_config.options,
        )

    def create_event_bus(self) -> IEventBus:
        """Crea el EventBus configurado."""
        bus_config = self._config.event_bus
        pipeline = _build_pipeline(bus_config)

        if bus_config.backend is None:
            return InMemoryEventBus(pipeline=pipeline)

        cls = _import_class(bus_config.backend)
        return cls(pipeline=pipeline, **bus_config.options)
