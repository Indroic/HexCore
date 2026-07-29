"""
Helpers de test que tocan la capa de aplicación o FastAPI.

Los imports de FastAPI son perezosos, para que `import hexcore.testing` funcione en un
entorno sin el extra `[api]`.
"""
from __future__ import annotations

import typing as t
from contextlib import contextmanager

if t.TYPE_CHECKING:
    from fastapi import FastAPI

    from hexcore.application.cqrs.in_memory_buses import (
        InMemoryCommandBus,
        InMemoryEventBus,
        InMemoryQueryBus,
    )
    from hexcore.application.cqrs.registry import HandlerRegistry
    from hexcore.domain.cqrs.buses import (
        AbstractCommandBus,
        AbstractEventBus,
        AbstractQueryBus,
    )
    from hexcore.domain.cqrs.serializer import ISerializer

    from .fakes import InMemoryTaskEnqueuer

__all__ = ["build_test_buses", "override_cqrs", "TestBuses"]


class TestBuses(t.NamedTuple):
    """Los buses de un test, con el registry y el enqueuer para asertar."""

    registry: "HandlerRegistry"
    command_bus: "InMemoryCommandBus"
    query_bus: "InMemoryQueryBus"
    event_bus: "InMemoryEventBus"
    enqueuer: "InMemoryTaskEnqueuer"
    serializer: "ISerializer"


def build_test_buses(
    registry: "HandlerRegistry | None" = None,
    *,
    enqueuer: "InMemoryTaskEnqueuer | None" = None,
) -> TestBuses:
    """
    Construye los tres buses in-memory listos para Smart Routing.

    Evita el error más común al testear CQRS: montar el bus sin enqueuer ni serializer y
    que el primer `@background_command` lance `RuntimeError`.

    Uso::

        buses = build_test_buses()
        buses.registry.register_command_handler(MiComando, MiHandler())

        await buses.command_bus.dispatch(MiComando(...))
        assert buses.enqueuer.command_names == ["MiComando"]
    """
    from hexcore.application.cqrs.in_memory_buses import (
        InMemoryCommandBus,
        InMemoryEventBus,
        InMemoryQueryBus,
    )
    from hexcore.application.cqrs.registry import HandlerRegistry
    from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer

    from .fakes import InMemoryTaskEnqueuer

    resolved_registry = registry or HandlerRegistry()
    resolved_enqueuer = enqueuer or InMemoryTaskEnqueuer()
    serializer = PydanticSerializer()

    return TestBuses(
        registry=resolved_registry,
        command_bus=InMemoryCommandBus(
            registry=resolved_registry,
            enqueuer=resolved_enqueuer,
            serializer=serializer,
        ),
        query_bus=InMemoryQueryBus(registry=resolved_registry),
        event_bus=InMemoryEventBus(
            enqueuer=resolved_enqueuer, serializer=serializer
        ),
        enqueuer=resolved_enqueuer,
        serializer=serializer,
    )


@contextmanager
def override_cqrs(
    app: "FastAPI",
    *,
    command_bus: "AbstractCommandBus | None" = None,
    query_bus: "AbstractQueryBus | None" = None,
    event_bus: "AbstractEventBus | None" = None,
    registry: "HandlerRegistry | None" = None,
) -> t.Iterator["FastAPI"]:
    """
    Sobreescribe los providers CQRS de una app y los restaura al salir.

    Restaurar importa: `app.dependency_overrides` es un dict de instancia, así que un
    override que no se limpia se filtra a todos los tests que reusen la app.

    Uso::

        buses = build_test_buses()
        with override_cqrs(app, command_bus=buses.command_bus):
            response = client.post("/users", json={...})
        assert buses.enqueuer.command_names == ["CreateUser"]
    """
    from hexcore.infrastructure.api.cqrs import (
        provide_command_bus,
        provide_event_bus,
        provide_query_bus,
        provide_registry,
    )

    replacements: dict[t.Any, t.Any] = {}
    if command_bus is not None:
        replacements[provide_command_bus] = lambda: command_bus
    if query_bus is not None:
        replacements[provide_query_bus] = lambda: query_bus
    if event_bus is not None:
        replacements[provide_event_bus] = lambda: event_bus
    if registry is not None:
        replacements[provide_registry] = lambda: registry

    # Se guarda el valor previo de cada clave —no sólo se borra— para poder anidar
    # `override_cqrs` sin que el interno destruya el override del externo.
    sentinel = object()
    previous = {
        key: app.dependency_overrides.get(key, sentinel) for key in replacements
    }

    app.dependency_overrides.update(replacements)
    try:
        yield app
    finally:
        for key, value in previous.items():
            if value is sentinel:
                app.dependency_overrides.pop(key, None)
            else:
                app.dependency_overrides[key] = value
