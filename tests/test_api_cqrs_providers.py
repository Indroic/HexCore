"""
F6: providers FastAPI del CQRS.

Existen como funciones por una única razón: poder sobreescribirlos en los tests con
`app.dependency_overrides`. Y leen del **mismo** contenedor que el worker, para que no
haya dos fuentes de verdad.
"""
from __future__ import annotations

import typing as t

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi import Depends, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from hexcore.application.cqrs.config import CQRSConfig  # noqa: E402
from hexcore.application.cqrs.registry import HandlerRegistry  # noqa: E402
from hexcore.domain.cqrs.buses import AbstractCommandBus  # noqa: E402
from hexcore.domain.cqrs.commands import Command  # noqa: E402
from hexcore.domain.cqrs.decorators import background_command  # noqa: E402
from hexcore.domain.cqrs.task_queues import ITaskEnqueuer  # noqa: E402
from hexcore.infrastructure.api.cqrs import (  # noqa: E402
    configure_cqrs,
    get_cqrs_container,
    provide_command_bus,
    provide_event_bus,
    provide_query_bus,
    provide_registry,
    reset_cqrs,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _clean_container():
    reset_cqrs()
    yield
    reset_cqrs()


class Greet(Command):
    name: str


class GreetHandler:
    async def handle(self, cmd: Greet) -> str:
        return f"hola {cmd.name}"


@background_command(queue="bg")
class BackgroundGreet(Command):
    name: str


class SpyEnqueuer(ITaskEnqueuer):
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def enqueue_command(self, command_name: str, payload: dict, queue: str) -> None:
        self.commands.append(command_name)

    async def enqueue_event(self, *a, **k) -> None: ...
    async def enqueue_handler(self, *a, **k) -> None: ...
    async def enqueue_task(self, *a, **k) -> None: ...


def _registry() -> HandlerRegistry:
    registry = HandlerRegistry()
    registry.register_command_handler(Greet, GreetHandler())
    return registry


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/greet/{name}")
    async def greet(
        name: str, bus: AbstractCommandBus = Depends(provide_command_bus)
    ) -> dict[str, t.Any]:
        return {"result": await bus.dispatch(Greet(name=name))}

    return app


# ── Configuración ──────────────────────────────────────────────────────────────


def test_providers_fail_with_a_clear_error_before_configuration():
    with pytest.raises(RuntimeError, match="configure_cqrs"):
        provide_command_bus()


def test_configure_cqrs_returns_a_usable_container():
    container = configure_cqrs(_registry(), config=CQRSConfig())

    assert container is get_cqrs_container()
    assert container.command_bus() is not None
    assert container.query_bus() is not None
    assert container.event_bus() is not None


def test_buses_are_cached():
    configure_cqrs(_registry(), config=CQRSConfig())

    assert provide_command_bus() is provide_command_bus()
    assert provide_query_bus() is provide_query_bus()
    assert provide_event_bus() is provide_event_bus()


def test_provide_registry_returns_the_configured_registry():
    registry = _registry()
    configure_cqrs(registry, config=CQRSConfig())

    assert provide_registry() is registry


def test_reset_cqrs_forces_reconfiguration():
    configure_cqrs(_registry(), config=CQRSConfig())
    reset_cqrs()

    with pytest.raises(RuntimeError):
        provide_command_bus()


# ── Uso desde un endpoint ──────────────────────────────────────────────────────


def test_endpoint_resolves_the_command_bus():
    configure_cqrs(_registry(), config=CQRSConfig())

    with TestClient(_app()) as client:
        assert client.get("/greet/mundo").json() == {"result": "hola mundo"}


def test_dependency_override_replaces_the_bus_in_tests():
    """La razón por la que estos providers son funciones."""
    configure_cqrs(_registry(), config=CQRSConfig())
    app = _app()

    class FakeBus(AbstractCommandBus):
        async def dispatch(self, command: Command) -> t.Any:
            return "reemplazado"

    app.dependency_overrides[provide_command_bus] = lambda: FakeBus()

    with TestClient(app) as client:
        assert client.get("/greet/mundo").json() == {"result": "reemplazado"}


# ── Una sola fuente de verdad con el worker ────────────────────────────────────


def test_consumer_shares_registry_and_serializer_with_the_web_buses():
    registry = _registry()
    container = configure_cqrs(registry, config=CQRSConfig())

    consumer = container.build_consumer()

    assert consumer._command_bus is provide_command_bus()
    assert consumer._event_bus is provide_event_bus()
    assert consumer._serializer is container.serializer


@pytest.mark.anyio
async def test_container_propagates_the_enqueuer_for_smart_routing():
    registry = _registry()
    registry.register_command_handler(BackgroundGreet, GreetHandler())
    enqueuer = SpyEnqueuer()
    configure_cqrs(registry, config=CQRSConfig(), enqueuer=enqueuer)

    await provide_command_bus().dispatch(BackgroundGreet(name="x"))

    assert enqueuer.commands == ["BackgroundGreet"]


def test_missing_enqueuer_with_background_commands_fails_at_container_build():
    """Hereda la validación de P0-5."""
    registry = _registry()
    registry.register_command_handler(BackgroundGreet, GreetHandler())
    configure_cqrs(registry, config=CQRSConfig())

    with pytest.raises(RuntimeError, match="BackgroundGreet"):
        provide_command_bus()


def test_serializer_is_the_same_instance_everywhere():
    container = configure_cqrs(_registry(), config=CQRSConfig())

    assert container.serializer is container.serializer
