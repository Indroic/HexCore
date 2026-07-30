"""
Tests unitarios para el módulo CQRS de Hexcore.
Cubre: Command, Query, HandlerRegistry, MiddlewarePipeline,
InMemory buses, Middlewares, PydanticSerializer, Adapters y Factory.
"""
from __future__ import annotations

import asyncio
import typing as t

import pytest

# ── Domain ────────────────────────────────────────────────────────
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.queries import Query
from hexcore.domain.cqrs.handlers import AbstractCommandHandler, AbstractQueryHandler
from hexcore.domain.cqrs.buses import AbstractCommandBus, AbstractQueryBus, AbstractEventBus
from hexcore.domain.cqrs.middleware import AbstractMiddleware, NextHandler
from hexcore.domain.cqrs.serializer import AbstractSerializer
from hexcore.domain.cqrs.exceptions import (
    HandlerNotFoundError,
    DuplicateHandlerError,
    DeserializationError,
)
from hexcore.domain.events import DomainEvent

# ── Application ───────────────────────────────────────────────────
from hexcore.application.cqrs.registry import HandlerRegistry
from hexcore.application.cqrs.pipeline import MiddlewarePipeline
from hexcore.application.cqrs.in_memory_buses import (
    InMemoryCommandBus,
    InMemoryQueryBus,
    InMemoryEventBus,
)
from hexcore.application.cqrs.adapters import (
    UseCaseCommandHandler,
)
from hexcore.application.cqrs.config import CQRSConfig, BusConfig
from hexcore.application.cqrs.factory import CQRSFactory

# ── Infrastructure ────────────────────────────────────────────────
from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer
from hexcore.infrastructure.cqrs.middlewares import (
    LoggingMiddleware,
    RetryMiddleware,
    ValidationMiddleware,
)


def _run(coro: t.Any) -> t.Any:
    """Helper para ejecutar coroutines en tests sin pytest-asyncio."""
    return asyncio.run(coro)


# ═══════════════════════════════════════════════════════════════════
# Fixtures y tipos de prueba
# ═══════════════════════════════════════════════════════════════════


class CreateUserCommand(Command):
    name: str
    email: str


class DeleteUserCommand(Command):
    user_id: str


class GetUserQuery(Query[dict[str, str]]):
    user_id: str


class ListUsersQuery(Query[list[dict[str, str]]]):
    limit: int = 10


class UserCreatedEvent(DomainEvent):
    user_id: str
    name: str


class CreateUserHandler(AbstractCommandHandler[CreateUserCommand, str]):
    async def handle(self, command: CreateUserCommand) -> str:
        return f"created:{command.name}"


class DeleteUserHandler(AbstractCommandHandler[DeleteUserCommand, None]):
    async def handle(self, command: DeleteUserCommand) -> None:
        pass


class GetUserHandler(AbstractQueryHandler[GetUserQuery, dict[str, str]]):
    async def handle(self, query: GetUserQuery) -> dict[str, str]:
        return {"id": query.user_id, "name": "Alice"}


class ListUsersHandler(AbstractQueryHandler[ListUsersQuery, list[dict[str, str]]]):
    async def handle(self, query: ListUsersQuery) -> list[dict[str, str]]:
        return [{"id": "1", "name": "Alice"}]


# ═══════════════════════════════════════════════════════════════════
# Tests: Command & Query Models
# ═══════════════════════════════════════════════════════════════════


class TestCommandModel:
    def test_command_is_frozen(self) -> None:
        cmd = CreateUserCommand(name="Alice", email="alice@test.com")
        with pytest.raises(Exception):  # ValidationError (frozen)
            cmd.name = "Bob"  # type: ignore[misc]

    def test_command_has_auto_id(self) -> None:
        cmd = CreateUserCommand(name="Alice", email="alice@test.com")
        assert cmd.command_id is not None

    def test_command_has_timestamp(self) -> None:
        cmd = CreateUserCommand(name="Alice", email="alice@test.com")
        assert cmd.timestamp is not None

    def test_command_serializable(self) -> None:
        cmd = CreateUserCommand(name="Alice", email="alice@test.com")
        data = cmd.model_dump()
        assert data["name"] == "Alice"
        assert data["email"] == "alice@test.com"


class TestQueryModel:
    def test_query_is_frozen(self) -> None:
        query = GetUserQuery(user_id="123")
        with pytest.raises(Exception):
            query.user_id = "456"  # type: ignore[misc]

    def test_query_serializable(self) -> None:
        query = GetUserQuery(user_id="123")
        data = query.model_dump()
        assert data["user_id"] == "123"


# ═══════════════════════════════════════════════════════════════════
# Tests: HandlerRegistry
# ═══════════════════════════════════════════════════════════════════


class TestHandlerRegistry:
    def test_register_and_resolve_command_handler(self) -> None:
        registry = HandlerRegistry()
        handler = CreateUserHandler()
        registry.register_command_handler(CreateUserCommand, handler)
        resolved = registry.resolve_command_handler(CreateUserCommand)
        assert resolved is handler

    def test_register_and_resolve_query_handler(self) -> None:
        registry = HandlerRegistry()
        handler = GetUserHandler()
        registry.register_query_handler(GetUserQuery, handler)
        resolved = registry.resolve_query_handler(GetUserQuery)
        assert resolved is handler

    def test_handler_not_found_raises(self) -> None:
        registry = HandlerRegistry()
        with pytest.raises(HandlerNotFoundError):
            registry.resolve_command_handler(CreateUserCommand)

    def test_query_handler_not_found_raises(self) -> None:
        registry = HandlerRegistry()
        with pytest.raises(HandlerNotFoundError):
            registry.resolve_query_handler(GetUserQuery)

    def test_duplicate_handler_raises(self) -> None:
        registry = HandlerRegistry()
        registry.register_command_handler(CreateUserCommand, CreateUserHandler())
        with pytest.raises(DuplicateHandlerError):
            registry.register_command_handler(CreateUserCommand, CreateUserHandler())

    def test_allow_override(self) -> None:
        registry = HandlerRegistry(allow_override=True)
        handler1 = CreateUserHandler()
        handler2 = CreateUserHandler()
        registry.register_command_handler(CreateUserCommand, handler1)
        registry.register_command_handler(CreateUserCommand, handler2)
        assert registry.resolve_command_handler(CreateUserCommand) is handler2

    def test_factory_registration(self) -> None:
        registry = HandlerRegistry()
        handler = CreateUserHandler()
        registry.register_command_handler(CreateUserCommand, lambda: handler)
        resolved = registry.resolve_command_handler(CreateUserCommand)
        assert resolved is handler

    def test_factory_cached_after_first_resolve(self) -> None:
        call_count = 0

        def factory() -> CreateUserHandler:
            nonlocal call_count
            call_count += 1
            return CreateUserHandler()

        registry = HandlerRegistry()
        registry.register_command_handler(CreateUserCommand, factory)
        registry.resolve_command_handler(CreateUserCommand)
        registry.resolve_command_handler(CreateUserCommand)
        assert call_count == 1

    def test_fluent_api(self) -> None:
        registry = HandlerRegistry()
        result = (
            registry.register_command_handler(CreateUserCommand, CreateUserHandler())
            .register_command_handler(DeleteUserCommand, DeleteUserHandler())
        )
        assert result is registry
        assert len(registry.registered_commands) == 2

    def test_registered_commands_introspection(self) -> None:
        registry = HandlerRegistry()
        registry.register_command_handler(CreateUserCommand, CreateUserHandler())
        assert CreateUserCommand in registry.registered_commands

    def test_registered_queries_introspection(self) -> None:
        registry = HandlerRegistry()
        registry.register_query_handler(GetUserQuery, GetUserHandler())
        assert GetUserQuery in registry.registered_queries


# ═══════════════════════════════════════════════════════════════════
# Tests: MiddlewarePipeline
# ═══════════════════════════════════════════════════════════════════


class TrackingMiddleware(AbstractMiddleware):
    """Middleware que registra el orden de ejecución."""

    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log

    async def handle(self, message: t.Any, next_handler: NextHandler) -> t.Any:
        self.log.append(f"{self.name}:before")
        result = await next_handler(message)
        self.log.append(f"{self.name}:after")
        return result


class TestMiddlewarePipeline:
    def test_empty_pipeline(self) -> None:
        pipeline = MiddlewarePipeline()

        async def handler(msg: t.Any) -> str:
            return "ok"

        result = _run(pipeline.execute("test", handler))
        assert result == "ok"

    def test_middleware_execution_order(self) -> None:
        log: list[str] = []
        pipeline = MiddlewarePipeline([
            TrackingMiddleware("MW1", log),
            TrackingMiddleware("MW2", log),
        ])

        async def handler(msg: t.Any) -> str:
            log.append("handler")
            return "ok"

        _run(pipeline.execute("test", handler))
        assert log == ["MW1:before", "MW2:before", "handler", "MW2:after", "MW1:after"]

    def test_fluent_add(self) -> None:
        log: list[str] = []
        pipeline = MiddlewarePipeline()
        pipeline.add(TrackingMiddleware("MW1", log))

        async def handler(msg: t.Any) -> str:
            log.append("handler")
            return "ok"

        _run(pipeline.execute("test", handler))
        assert "MW1:before" in log

    def test_add_many(self) -> None:
        log: list[str] = []
        pipeline = MiddlewarePipeline()
        pipeline.add_many([
            TrackingMiddleware("MW1", log),
            TrackingMiddleware("MW2", log),
        ])

        async def handler(msg: t.Any) -> str:
            return "ok"

        _run(pipeline.execute("test", handler))
        assert "MW1:before" in log
        assert "MW2:before" in log


# ═══════════════════════════════════════════════════════════════════
# Tests: InMemory Buses
# ═══════════════════════════════════════════════════════════════════


class TestInMemoryCommandBus:
    def test_dispatch_command(self) -> None:
        registry = HandlerRegistry()
        registry.register_command_handler(CreateUserCommand, CreateUserHandler())
        bus = InMemoryCommandBus(registry=registry)

        result = _run(bus.dispatch(CreateUserCommand(name="Alice", email="a@test.com")))
        assert result == "created:Alice"

    def test_dispatch_unregistered_command_raises(self) -> None:
        registry = HandlerRegistry()
        bus = InMemoryCommandBus(registry=registry)

        with pytest.raises(HandlerNotFoundError):
            _run(bus.dispatch(CreateUserCommand(name="Alice", email="a@test.com")))

    def test_dispatch_with_middleware(self) -> None:
        log: list[str] = []
        registry = HandlerRegistry()
        registry.register_command_handler(CreateUserCommand, CreateUserHandler())
        pipeline = MiddlewarePipeline([TrackingMiddleware("MW", log)])
        bus = InMemoryCommandBus(registry=registry, pipeline=pipeline)

        _run(bus.dispatch(CreateUserCommand(name="Alice", email="a@test.com")))
        assert "MW:before" in log


class TestInMemoryQueryBus:
    def test_ask_query(self) -> None:
        registry = HandlerRegistry()
        registry.register_query_handler(GetUserQuery, GetUserHandler())
        bus = InMemoryQueryBus(registry=registry)

        result = _run(bus.ask(GetUserQuery(user_id="123")))
        assert result == {"id": "123", "name": "Alice"}

    def test_ask_unregistered_query_raises(self) -> None:
        registry = HandlerRegistry()
        bus = InMemoryQueryBus(registry=registry)

        with pytest.raises(HandlerNotFoundError):
            _run(bus.ask(GetUserQuery(user_id="123")))


class TestInMemoryEventBus:
    def test_publish_event(self) -> None:
        bus = InMemoryEventBus()
        received: list[DomainEvent] = []

        async def handler(event: DomainEvent) -> None:
            received.append(event)

        bus.subscribe(UserCreatedEvent, handler)
        event = UserCreatedEvent(user_id="1", name="Alice")
        _run(bus.publish(event))

        assert len(received) == 1
        assert received[0].user_id == "1"  # type: ignore[attr-defined]

    def test_publish_no_subscribers(self) -> None:
        bus = InMemoryEventBus()
        event = UserCreatedEvent(user_id="1", name="Alice")
        # No debería lanzar excepción
        _run(bus.publish(event))

    def test_multiple_subscribers(self) -> None:
        bus = InMemoryEventBus()
        count = 0

        async def handler1(event: DomainEvent) -> None:
            nonlocal count
            count += 1

        async def handler2(event: DomainEvent) -> None:
            nonlocal count
            count += 1

        bus.subscribe(UserCreatedEvent, handler1)
        bus.subscribe(UserCreatedEvent, handler2)
        _run(bus.publish(UserCreatedEvent(user_id="1", name="Alice")))

        assert count == 2


# ═══════════════════════════════════════════════════════════════════
# Tests: PydanticSerializer
# ═══════════════════════════════════════════════════════════════════


class TestPydanticSerializer:
    def test_serialize_command(self) -> None:
        serializer = PydanticSerializer()
        cmd = CreateUserCommand(name="Alice", email="alice@test.com")
        data = serializer.serialize(cmd)

        assert "__type__" in data
        assert "__data__" in data
        assert "CreateUserCommand" in data["__type__"]
        assert data["__data__"]["name"] == "Alice"

    def test_roundtrip(self) -> None:
        serializer = PydanticSerializer()
        original = CreateUserCommand(name="Alice", email="alice@test.com")
        data = serializer.serialize(original)
        restored = serializer.deserialize(data)

        assert isinstance(restored, CreateUserCommand)
        assert restored.name == original.name
        assert restored.email == original.email
        assert restored.command_id == original.command_id

    def test_deserialize_missing_type_raises(self) -> None:
        serializer = PydanticSerializer()
        with pytest.raises(DeserializationError):
            serializer.deserialize({"__data__": {}})

    def test_deserialize_bad_type_raises(self) -> None:
        serializer = PydanticSerializer()
        with pytest.raises(DeserializationError):
            serializer.deserialize({"__type__": "nonexistent.module.Class", "__data__": {}})


# ═══════════════════════════════════════════════════════════════════
# Tests: Middlewares Concretos
# ═══════════════════════════════════════════════════════════════════


class TestLoggingMiddleware:
    def test_logs_dispatch(self) -> None:
        import logging

        middleware = LoggingMiddleware(log_level=logging.DEBUG)

        async def handler(msg: t.Any) -> str:
            return "ok"

        result = _run(middleware.handle("test", handler))
        assert result == "ok"

    def test_logs_error(self) -> None:
        middleware = LoggingMiddleware()

        async def handler(msg: t.Any) -> str:
            raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            _run(middleware.handle("test", handler))


class TestRetryMiddleware:
    def test_retries_on_failure(self) -> None:
        call_count = 0

        async def handler(msg: t.Any) -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ValueError("transient")
            return "ok"

        middleware = RetryMiddleware(max_retries=3, base_delay=0.01)
        result = _run(middleware.handle("test", handler))
        assert result == "ok"
        assert call_count == 3

    def test_exhausts_retries(self) -> None:
        async def handler(msg: t.Any) -> str:
            raise ValueError("permanent")

        middleware = RetryMiddleware(max_retries=2, base_delay=0.01)
        with pytest.raises(ValueError, match="permanent"):
            _run(middleware.handle("test", handler))


class TestValidationMiddleware:
    def test_revalidates_pydantic_model(self) -> None:
        middleware = ValidationMiddleware()
        cmd = CreateUserCommand(name="Alice", email="alice@test.com")

        async def handler(msg: t.Any) -> t.Any:
            return msg

        result = _run(middleware.handle(cmd, handler))
        assert isinstance(result, CreateUserCommand)
        assert result.name == "Alice"

    def test_passthrough_non_pydantic(self) -> None:
        middleware = ValidationMiddleware()

        async def handler(msg: t.Any) -> t.Any:
            return msg

        result = _run(middleware.handle("plain_string", handler))
        assert result == "plain_string"


# ═══════════════════════════════════════════════════════════════════
# Tests: Adapters
# ═══════════════════════════════════════════════════════════════════


class TestUseCaseCommandHandler:
    def test_wraps_use_case(self) -> None:
        from hexcore.application.use_cases.base import UseCase

        class FakeUseCase(UseCase[t.Any, str]):
            async def execute(self, command: t.Any) -> str:
                return f"executed:{command.name}"

        use_case = FakeUseCase()
        handler = UseCaseCommandHandler(use_case)
        cmd = CreateUserCommand(name="Alice", email="a@test.com")
        result = _run(handler.handle(cmd))
        assert result == "executed:Alice"





# ═══════════════════════════════════════════════════════════════════
# Tests: CQRSFactory
# ═══════════════════════════════════════════════════════════════════


class TestCQRSFactory:
    def test_creates_in_memory_command_bus_by_default(self) -> None:
        config = CQRSConfig()
        registry = HandlerRegistry()
        factory = CQRSFactory(config, registry)
        bus = factory.create_command_bus()
        assert isinstance(bus, InMemoryCommandBus)

    def test_creates_in_memory_query_bus_by_default(self) -> None:
        config = CQRSConfig()
        registry = HandlerRegistry()
        factory = CQRSFactory(config, registry)
        bus = factory.create_query_bus()
        assert isinstance(bus, InMemoryQueryBus)

    def test_creates_in_memory_event_bus_by_default(self) -> None:
        config = CQRSConfig()
        registry = HandlerRegistry()
        factory = CQRSFactory(config, registry)
        bus = factory.create_event_bus()
        assert isinstance(bus, InMemoryEventBus)

    def test_creates_pydantic_serializer_by_default(self) -> None:
        config = CQRSConfig()
        registry = HandlerRegistry()
        factory = CQRSFactory(config, registry)
        serializer = factory.create_serializer()
        assert isinstance(serializer, PydanticSerializer)

    def test_creates_bus_with_middlewares_from_config(self) -> None:
        config = CQRSConfig(
            command_bus=BusConfig(
                middlewares=[
                    "hexcore.infrastructure.cqrs.middlewares.LoggingMiddleware",
                ],
            ),
        )
        registry = HandlerRegistry()
        registry.register_command_handler(CreateUserCommand, CreateUserHandler())
        factory = CQRSFactory(config, registry)
        bus = factory.create_command_bus()
        assert isinstance(bus, InMemoryCommandBus)


# ═══════════════════════════════════════════════════════════════════
# Tests: Config Model
# ═══════════════════════════════════════════════════════════════════


class TestCQRSConfig:
    def test_default_config(self) -> None:
        config = CQRSConfig()
        assert config.enabled is True
        assert config.command_bus.backend is None
        assert config.query_bus.backend is None
        assert config.event_bus.backend is None
        assert config.serializer is None

    def test_config_with_backend(self) -> None:
        config = CQRSConfig(
            command_bus=BusConfig(
                backend="hexcore.infrastructure.cqrs.procrastinate.ProcrastinateCommandBus"
            )
        )
        assert config.command_bus.backend is not None

    def test_server_config_cqrs_none_by_default(self) -> None:
        from hexcore.config import ServerConfig

        sc = ServerConfig()
        assert sc.cqrs is None

    def test_server_config_with_cqrs(self) -> None:
        from hexcore.config import ServerConfig

        sc = ServerConfig(cqrs=CQRSConfig())
        assert sc.cqrs is not None
        assert sc.cqrs.enabled is True
