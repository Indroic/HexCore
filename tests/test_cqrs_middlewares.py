"""
P0-6: `TransactionMiddleware` ya no es el default de `CQRSConfig` y exige
`uow_factory` explícito en vez de adivinar la sesión.
"""
from __future__ import annotations

import typing as t

import pytest

from hexcore.application.cqrs.config import CQRSConfig
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.decorators import background_command
from hexcore.infrastructure.cqrs.middlewares import RetryMiddleware, TransactionMiddleware


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_cqrs_config_has_no_default_middlewares():
    config = CQRSConfig()

    assert config.command_bus.middlewares == []
    assert config.query_bus.middlewares == []
    assert config.event_bus.middlewares == []


def test_transaction_middleware_requires_explicit_uow_factory():
    with pytest.raises(ValueError, match="uow_factory"):
        TransactionMiddleware()


class FakeUoW:
    def __init__(self) -> None:
        self.entered = False
        self.commits = 0

    async def __aenter__(self) -> "FakeUoW":
        self.entered = True
        return self

    async def __aexit__(self, *exc: t.Any) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


@pytest.mark.anyio
async def test_transaction_middleware_uses_the_injected_factory():
    uow = FakeUoW()
    middleware = TransactionMiddleware(uow_factory=lambda: uow)

    async def next_handler(message: t.Any) -> str:
        return f"handled:{message}"

    result = await middleware.handle("msg", next_handler)

    assert result == "handled:msg"
    assert uow.entered is True
    assert uow.commits == 1


# ── P2-3: RetryMiddleware vs el retry de la cola ───────────────────────────────


@background_command(queue="bg")
class RetriedBackgroundCommand(Command):
    value: str


class PlainRetriedCommand(Command):
    value: str


@pytest.mark.anyio
async def test_retry_middleware_warns_when_the_queue_also_retries(caplog):
    import logging

    middleware = RetryMiddleware(max_retries=2, base_delay=0)

    async def ok(message: t.Any) -> str:
        return "ok"

    with caplog.at_level(logging.WARNING, logger="hexcore.cqrs.retry"):
        await middleware.handle(RetriedBackgroundCommand(value="x"), ok)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "reintentos se multiplican" in warnings[0].getMessage()


@pytest.mark.anyio
async def test_the_warning_is_emitted_once_per_type_not_per_message(caplog):
    import logging

    middleware = RetryMiddleware(max_retries=1, base_delay=0)

    async def ok(message: t.Any) -> str:
        return "ok"

    with caplog.at_level(logging.WARNING, logger="hexcore.cqrs.retry"):
        for i in range(5):
            await middleware.handle(RetriedBackgroundCommand(value=str(i)), ok)

    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


@pytest.mark.anyio
async def test_no_warning_for_synchronous_commands(caplog):
    import logging

    middleware = RetryMiddleware(max_retries=1, base_delay=0)

    async def ok(message: t.Any) -> str:
        return "ok"

    with caplog.at_level(logging.WARNING, logger="hexcore.cqrs.retry"):
        await middleware.handle(PlainRetriedCommand(value="x"), ok)

    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


@pytest.mark.anyio
async def test_retry_middleware_still_retries():
    attempts: list[int] = []
    middleware = RetryMiddleware(max_retries=2, base_delay=0)

    async def flaky(message: t.Any) -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise RuntimeError("aún no")
        return "ok"

    assert await middleware.handle(PlainRetriedCommand(value="x"), flaky) == "ok"
    assert len(attempts) == 3


@pytest.mark.anyio
async def test_retry_middleware_reraises_after_exhausting_retries():
    middleware = RetryMiddleware(max_retries=1, base_delay=0)

    async def always_fails(message: t.Any) -> str:
        raise RuntimeError("siempre falla")

    with pytest.raises(RuntimeError, match="siempre falla"):
        await middleware.handle(PlainRetriedCommand(value="x"), always_fails)
