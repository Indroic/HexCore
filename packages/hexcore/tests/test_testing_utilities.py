"""
F9: utilidades de test publicadas en `hexcore.testing`.

Requisito del plan: el import no debe requerir dependencias opcionales duras.
"""
from __future__ import annotations

import subprocess
import sys
import typing as t

import pytest

from hexcore.application.cqrs.registry import HandlerRegistry
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.decorators import background_command, background_handler
from hexcore.domain.events import DomainEvent
from hexcore.testing import (
    FakeLockProvider,
    InMemoryTaskEnqueuer,
    RecordedEnqueue,
    build_test_buses,
    override_cqrs,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@background_command(queue="emails")
class SendEmail(Command):
    to: str


class SendEmailHandler:
    async def handle(self, cmd: SendEmail) -> None: ...


class PlainCommand(Command):
    value: str


class PlainHandler:
    async def handle(self, cmd: PlainCommand) -> str:
        return f"ok:{cmd.value}"


class SomethingHappened(DomainEvent):
    info: str


@background_handler(queue="analytics")
async def on_something(event: SomethingHappened) -> None: ...


# ── InMemoryTaskEnqueuer ───────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_enqueuer_records_commands_with_their_queue():
    buses = build_test_buses()
    buses.registry.register_command_handler(SendEmail, SendEmailHandler())

    await buses.command_bus.dispatch(SendEmail(to="a@b.c"))

    assert buses.enqueuer.command_names == ["SendEmail"]
    assert buses.enqueuer.commands[0].queue == "emails"
    assert buses.enqueuer.commands[0].payload["__data__"]["to"] == "a@b.c"


@pytest.mark.anyio
async def test_enqueuer_records_handlers():
    buses = build_test_buses()
    buses.event_bus.subscribe(SomethingHappened, on_something)

    await buses.event_bus.publish(SomethingHappened(info="x"))

    assert len(buses.enqueuer.handlers) == 1
    assert buses.enqueuer.handlers[0].queue == "analytics"


@pytest.mark.anyio
async def test_enqueuer_separates_the_four_kinds():
    enqueuer = InMemoryTaskEnqueuer()

    await enqueuer.enqueue_command("C", {}, "q1")
    await enqueuer.enqueue_event("E", {}, "q2")
    await enqueuer.enqueue_handler("H", {}, "q3")
    await enqueuer.enqueue_task("T", {}, "q4")

    assert enqueuer.command_names == ["C"]
    assert [e.name for e in enqueuer.events] == ["E"]
    assert enqueuer.handler_names == ["H"]
    assert enqueuer.task_names == ["T"]
    assert len(enqueuer) == 4


@pytest.mark.anyio
async def test_enqueuer_can_be_told_to_fail():
    enqueuer = InMemoryTaskEnqueuer(fail_on={"Boom"})

    await enqueuer.enqueue_command("Fine", {}, "q")
    with pytest.raises(RuntimeError, match="Boom"):
        await enqueuer.enqueue_command("Boom", {}, "q")

    assert enqueuer.command_names == ["Fine"]


@pytest.mark.anyio
async def test_enqueuer_clear():
    enqueuer = InMemoryTaskEnqueuer()
    await enqueuer.enqueue_task("T", {}, "q")

    enqueuer.clear()

    assert len(enqueuer) == 0


def test_recorded_enqueue_is_comparable():
    assert RecordedEnqueue("task", "T") == RecordedEnqueue("task", "T")


# ── FakeLockProvider ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_fake_lock_grants_by_default():
    lock = FakeLockProvider()

    assert await lock.acquire_lock("k", 60) is True
    assert await lock.acquire_lock("k", 60) is True
    assert lock.acquired == ["k", "k"]


@pytest.mark.anyio
async def test_fake_lock_can_always_deny():
    lock = FakeLockProvider(grant=False)

    assert await lock.acquire_lock("k", 60) is False


@pytest.mark.anyio
async def test_fake_lock_shared_mode_behaves_like_a_real_lock():
    lock = FakeLockProvider(shared=True)

    assert await lock.acquire_lock("k", 60) is True
    assert await lock.acquire_lock("k", 60) is False
    assert await lock.acquire_lock("otra", 60) is True

    await lock.release_lock("k")
    assert await lock.acquire_lock("k", 60) is True


@pytest.mark.anyio
async def test_fake_lock_can_raise():
    lock = FakeLockProvider(raise_on_acquire=ConnectionError("redis down"))

    with pytest.raises(ConnectionError):
        await lock.acquire_lock("k", 60)


@pytest.mark.anyio
async def test_fake_lock_records_releases():
    lock = FakeLockProvider()
    await lock.release_lock("k")

    assert lock.released == ["k"]


# ── build_test_buses ───────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_build_test_buses_is_ready_for_smart_routing():
    """El error más común al testear CQRS: bus sin enqueuer ni serializer."""
    buses = build_test_buses()
    buses.registry.register_command_handler(SendEmail, SendEmailHandler())

    await buses.command_bus.dispatch(SendEmail(to="x@y.z"))  # no debe lanzar


@pytest.mark.anyio
async def test_build_test_buses_executes_plain_commands():
    buses = build_test_buses()
    buses.registry.register_command_handler(PlainCommand, PlainHandler())

    assert await buses.command_bus.dispatch(PlainCommand(value="v")) == "ok:v"


def test_build_test_buses_accepts_an_existing_registry():
    registry = HandlerRegistry()
    buses = build_test_buses(registry)

    assert buses.registry is registry


def test_build_test_buses_shares_the_serializer_across_buses():
    buses = build_test_buses()

    assert buses.command_bus._serializer is buses.serializer
    assert buses.event_bus._serializer is buses.serializer


# ── override_cqrs ──────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_override_cqrs_replaces_and_restores():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")

    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from hexcore.application.cqrs.config import CQRSConfig
    from hexcore.domain.cqrs.buses import AbstractCommandBus
    from hexcore.infrastructure.api.cqrs import (
        configure_cqrs,
        provide_command_bus,
        reset_cqrs,
    )

    registry = HandlerRegistry()
    registry.register_command_handler(PlainCommand, PlainHandler())
    configure_cqrs(registry, config=CQRSConfig())

    app = FastAPI()

    @app.get("/run")
    async def run(
        bus: AbstractCommandBus = Depends(provide_command_bus),
    ) -> dict[str, t.Any]:
        return {"result": await bus.dispatch(PlainCommand(value="v"))}

    try:
        buses = build_test_buses()
        buses.registry.register_command_handler(PlainCommand, PlainHandler())

        with TestClient(app) as client:
            with override_cqrs(app, command_bus=buses.command_bus):
                assert client.get("/run").json() == {"result": "ok:v"}
                assert provide_command_bus in app.dependency_overrides

            # Restaurado al salir: si no, el override se filtra a los demás tests.
            assert provide_command_bus not in app.dependency_overrides
            assert client.get("/run").json() == {"result": "ok:v"}
    finally:
        reset_cqrs()


def test_override_cqrs_can_be_nested():
    pytest.importorskip("fastapi")

    from fastapi import FastAPI

    from hexcore.infrastructure.api.cqrs import provide_command_bus

    app = FastAPI()
    outer = build_test_buses().command_bus
    inner = build_test_buses().command_bus

    with override_cqrs(app, command_bus=outer):
        with override_cqrs(app, command_bus=inner):
            assert app.dependency_overrides[provide_command_bus]() is inner
        # El anidado no debe destruir el override externo.
        assert app.dependency_overrides[provide_command_bus]() is outer

    assert provide_command_bus not in app.dependency_overrides


def test_override_cqrs_restores_even_if_the_block_raises():
    pytest.importorskip("fastapi")

    from fastapi import FastAPI

    from hexcore.infrastructure.api.cqrs import provide_command_bus

    app = FastAPI()

    with pytest.raises(RuntimeError):
        with override_cqrs(app, command_bus=build_test_buses().command_bus):
            raise RuntimeError("boom")

    assert provide_command_bus not in app.dependency_overrides


# ── El requisito de import ─────────────────────────────────────────────────────


@pytest.mark.parametrize("hidden", ["fastapi", "sqlalchemy", "redis", "procrastinate"])
def test_hexcore_testing_imports_without_optional_dependencies(hidden):
    code = f"""
import sys

class Blocker:
    @classmethod
    def find_spec(cls, fullname, path, target=None):
        if fullname.split(".")[0] == "{hidden}":
            raise ImportError("bloqueado: " + fullname)
        return None

sys.meta_path.insert(0, Blocker)

import hexcore.testing
from hexcore.testing import InMemoryTaskEnqueuer, FakeLockProvider, build_test_buses
assert InMemoryTaskEnqueuer() is not None
assert FakeLockProvider() is not None
print("ok")
"""
    import os

    env = os.environ.copy()
    env["PYTHONPATH"] = "."
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )

    assert result.returncode == 0, result.stderr
    assert "ok" in result.stdout


def test_fixtures_module_is_importable():
    """Se carga sólo cuando lo pides, pero tiene que cargar."""
    import hexcore.testing.fixtures as fixtures

    assert hasattr(fixtures, "cqrs_buses")
    assert hasattr(fixtures, "sqlite_engine")
    assert hasattr(fixtures, "uow")
