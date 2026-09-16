"""
conftest.py for a project built on HexCore.

`hexcore.testing` requires no extras: the doubles let you test a hexagonal app without
standing up Redis, Postgres or a broker.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import hexcore.cqrs as cqrs
from hexcore.config import LazyConfig
from hexcore.fastapi import SqlEngineStep, build_lifespan, create_app, reset_cqrs
from hexcore.testing import (
    FakeRepository,
    FakeUnitOfWork,
    build_test_buses,
    override_cqrs,
)

# Provides: anyio_backend, task_enqueuer, lock_provider, cqrs_buses, sqlite_engine,
# sqlite_session, uow.
#
# `sqlite_engine` uses StaticPool on purpose: without it each connection to :memory: opens a
# DIFFERENT database, and the test that creates the table is not the one that queries it.
pytest_plugins = ["hexcore.testing.fixtures"]

# With Darwin, add its fixtures too -- identity_clock, identity_audit, identity_users and
# identity_container, the last already wired to the other three and cleaned up after each
# test, which is what stops one case's identity container leaking into the next:
#
#     pytest_plugins = ["hexcore.testing.fixtures", "hexcore.darwin.testing.fixtures"]


# HexCore's suite uses anyio, never pytest-asyncio -- it is not a dependency of the
# framework. Mark async tests with @pytest.mark.anyio.


@pytest.fixture(autouse=True)
def _isolate_global_state():
    """
    Both of these cache process-wide, and a leak between tests shows up as an unrelated test
    failing. Before 9.0 ServerConfig.event_bus was worse: its default was evaluated at class
    definition, so every ServerConfig() in the process shared one bus and one handler
    dictionary, and a subscription made by one test was seen by the next.
    """
    yield
    reset_cqrs()
    LazyConfig.clear_cache()


@pytest.fixture
def app():
    # In-memory SQLite per test run, through the lifespan rather than around it.
    return create_app(lifespan=build_lifespan(SqlEngineStep("sqlite+aiosqlite:///:memory:")))


@pytest.fixture
def client(app):
    # The `with` is NOT optional: TestClient(app) without it does not run the lifespan, so
    # the engine is never initialised and the first endpoint touching the database fails
    # with an error pointing at init_engine rather than at the test.
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def buses():
    """
    build_test_buses() wires an enqueuer and a serializer. Building the buses without them
    is the most common CQRS testing mistake: the first @background_command blows up with a
    RuntimeError that says nothing about the test that caused it.

    Returns a NamedTuple: registry, command_bus, query_bus, event_bus, enqueuer, serializer.
    """
    return build_test_buses()


@pytest.fixture
def uow():
    """FakeUnitOfWork COUNTS commit() and rollback() rather than flagging them with a
    boolean, which is what makes a double commit visible -- the bug TransactionMiddleware
    causes on a handler that already manages its own transaction."""
    return FakeUnitOfWork({"tickets": FakeRepository(entities=[])})


# ---- What a test looks like --------------------------------------------------
#
# @pytest.mark.anyio
# async def test_creating_a_ticket_enqueues_the_notification(app, client, buses):
#     buses.registry.register_command_handler(CreateTicket, CreateTicketHandler(FakeUoW()))
#
#     # override_cqrs saves each previous value and restores even if the block raises.
#     # app.dependency_overrides is an instance dict: an override that is not cleaned up
#     # leaks into every test reusing the app, and the one that fails is an unrelated one.
#     with override_cqrs(app, command_bus=buses.command_bus):
#         response = client.post("/tickets", json={"title": "Something broke"})
#
#     assert response.status_code == 200
#     assert buses.enqueuer.command_names == ["NotifyCustomer"]
#
#
# Cover BOTH halves of Smart Routing -- it is the contract that breaks if somebody builds
# separate buses for the web process and the worker:
#
# @pytest.mark.anyio
# async def test_the_worker_executes_what_the_bus_enqueued(buses):
#     consumer = cqrs.CQRSConsumer(buses.command_bus, buses.event_bus)
#
#     await buses.command_bus.dispatch(SendEmailCommand(user_id="1", template="welcome"))
#     assert handled == []                       # not yet: it was enqueued
#
#     await consumer.process_command(buses.enqueuer.commands[0].payload)
#     assert handled == ["1"]                    # the consumer executed it
