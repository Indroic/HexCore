"""
P0-6: `TransactionMiddleware` ya no es el default de `CQRSConfig` y exige
`uow_factory` explícito en vez de adivinar la sesión.
"""
from __future__ import annotations

import typing as t

import pytest

from hexcore.application.cqrs.config import CQRSConfig
from hexcore.infrastructure.cqrs.middlewares import TransactionMiddleware


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
