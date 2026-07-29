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
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer

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
    """
    Instancia middlewares a partir de sus dotted paths.

    Sólo sirve para middlewares construibles sin argumentos. Los que necesitan
    configuración (p. ej. `TransactionMiddleware`, que requiere un `uow_factory`
    atado al engine de la aplicación) hay que instanciarlos a mano y pasar el
    `MiddlewarePipeline` al bus.
    """
    middlewares: list[IMiddleware] = []
    for path in dotted_paths:
        cls = _import_class(path)
        try:
            middlewares.append(cls())
        except TypeError as exc:
            raise TypeError(
                f"El middleware '{path}' no se puede construir sin argumentos: {exc}. "
                "Declararlo por dotted path sólo funciona para middlewares sin "
                "configuración; instancialo a mano y pasá el MiddlewarePipeline al bus."
            ) from exc
        except ValueError as exc:
            raise ValueError(
                f"El middleware '{path}' rechazó su construcción por defecto: {exc} "
                "Declararlo por dotted path sólo funciona para middlewares sin "
                "configuración; instancialo a mano y pasá el MiddlewarePipeline al bus."
            ) from exc
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

        factory = CQRSFactory(config, registry, enqueuer=ProcrastinateEnqueuer(app))
        command_bus = factory.create_command_bus()
        query_bus = factory.create_query_bus()
        event_bus = factory.create_event_bus()

    El `enqueuer` es lo que habilita el Smart Routing: sin él, los buses in-memory
    no pueden enrutar un `@background_command` y la factory lo dice **al construir**,
    no en el primer dispatch.
    """

    def __init__(
        self,
        config: CQRSConfig,
        registry: HandlerRegistry,
        enqueuer: ITaskEnqueuer | None = None,
    ) -> None:
        self._config = config
        self._registry = registry
        self._enqueuer = enqueuer
        self._serializer: ISerializer | None = None

    def create_serializer(self) -> ISerializer:
        """
        Crea el serializer configurado (PydanticSerializer por defecto).

        La instancia se cachea: los buses y el consumer tienen que compartir el
        mismo serializer para que el round-trip por la cola sea coherente.
        """
        if self._serializer is not None:
            return self._serializer

        if self._config.serializer:
            cls = _import_class(self._config.serializer)
            self._serializer = t.cast(ISerializer, cls())
        else:
            from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer

            self._serializer = PydanticSerializer()
        return self._serializer

    def create_command_bus(self, **extra_kwargs: t.Any) -> ICommandBus:
        """
        Crea el CommandBus configurado.
        Si no hay backend explícito, retorna InMemoryCommandBus con el enqueuer y el
        serializer necesarios para el Smart Routing.
        """
        bus_config = self._config.command_bus
        pipeline = _build_pipeline(bus_config)

        if bus_config.backend is None:
            self._assert_enqueuer_for_background_commands()
            return InMemoryCommandBus(
                registry=self._registry,
                pipeline=pipeline,
                enqueuer=self._enqueuer,
                serializer=self.create_serializer(),
                **extra_kwargs,
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

    def create_event_bus(self, **extra_kwargs: t.Any) -> IEventBus:
        """
        Crea el EventBus configurado.

        Al bus in-memory se le pasan enqueuer y serializer para que los suscriptores
        marcados con `@background_handler` se puedan enrutar.
        """
        bus_config = self._config.event_bus
        pipeline = _build_pipeline(bus_config)

        if bus_config.backend is None:
            return InMemoryEventBus(
                pipeline=pipeline,
                enqueuer=self._enqueuer,
                serializer=self.create_serializer(),
                **extra_kwargs,
            )

        cls = _import_class(bus_config.backend)
        return cls(
            pipeline=pipeline,
            **bus_config.options,
            **extra_kwargs,
        )

    # ── Validación ────────────────────────────────────────────────────────────

    def _assert_enqueuer_for_background_commands(self) -> None:
        """
        Falla al construir si hay commands de background registrados y no hay
        enqueuer. El error en el primer dispatch llega demasiado tarde: para
        entonces la petición del usuario ya está en vuelo.
        """
        if self._enqueuer is not None:
            return

        background = [
            command_type.__name__
            for command_type in self._registry.registered_commands
            if getattr(command_type, "__cqrs_background__", False)
        ]
        if not background:
            return

        raise RuntimeError(
            "CQRSFactory no tiene 'enqueuer', pero hay commands decorados con "
            f"@background_command: {', '.join(sorted(background))}. "
            "Pasá un ITaskEnqueuer al construir la factory, p. ej. "
            "CQRSFactory(config, registry, enqueuer=ProcrastinateEnqueuer(app))."
        )
