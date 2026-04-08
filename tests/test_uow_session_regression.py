import asyncio
from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

from hexcore.infrastructure.repositories.orms.sqlalchemy.session import (
    get_async_db_session,
)
from hexcore.infrastructure.uow import SqlAlchemyUnitOfWork


class _DummySessionContext:
    def __init__(self, session: Any) -> None:
        self._session = session

    async def __aenter__(self) -> Any:
        return self._session

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> bool:
        return False


class _DummyRepository:
    def __init__(self, uow: Any) -> None:
        self.uow = uow


class _DummyConfig:
    def __init__(self) -> None:
        self.event_dispatcher = AsyncMock()
        self.repository_discovery_paths = {
            "myapp.features.users.infrastructure.repositories"
        }


def _build_sql_uow(session: Any) -> SqlAlchemyUnitOfWork:
    with (
        patch(
            "hexcore.infrastructure.uow.LazyConfig.get_config",
            return_value=_DummyConfig(),
        ),
        patch(
            "hexcore.infrastructure.uow.discover_sql_repositories",
            return_value={"dummy": _DummyRepository},
        ),
    ):
        return SqlAlchemyUnitOfWork(session=session)


def test_get_async_db_session_no_manual_rollback_on_error():
    async def _run():
        session = MagicMock()
        session.rollback = AsyncMock()

        with patch(
            "hexcore.infrastructure.repositories.orms.sqlalchemy.session.AsyncSessionLocal",
            return_value=_DummySessionContext(session),
        ):
            session_generator = cast(AsyncGenerator[Any, None], get_async_db_session())
            yielded = await anext(session_generator)
            assert yielded is session

            try:
                await session_generator.athrow(ValueError("boom"))
            except ValueError:
                pass

        session.rollback.assert_not_awaited()

    asyncio.run(_run())


def test_sql_uow_aexit_does_not_rollback_session():
    async def _run():
        session = MagicMock()
        session.rollback = AsyncMock()

        uow = _build_sql_uow(session)
        uow.clear_tracked_entities = MagicMock()

        await uow.__aexit__(ValueError, ValueError("boom"), None)

        session.rollback.assert_not_awaited()
        uow.clear_tracked_entities.assert_called_once()

    asyncio.run(_run())


def test_sql_uow_rollback_keeps_explicit_rollback_behavior():
    async def _run():
        session = MagicMock()
        session.in_transaction.return_value = True
        session.rollback = AsyncMock()

        uow = _build_sql_uow(session)
        uow.clear_tracked_entities = MagicMock()

        await uow.rollback()

        session.rollback.assert_awaited_once()
        uow.clear_tracked_entities.assert_called_once()

    asyncio.run(_run())


def test_sql_uow_commit_clears_entities_even_if_dispatch_fails():
    async def _run():
        session = MagicMock()
        session.commit = AsyncMock()

        uow = _build_sql_uow(session)
        uow.dispatch_events = AsyncMock(side_effect=RuntimeError("dispatch failed"))
        uow.clear_tracked_entities = MagicMock()

        raised = None
        try:
            await uow.commit()
        except RuntimeError as exc:
            raised = exc

        assert raised is not None
        session.commit.assert_awaited_once()
        uow.dispatch_events.assert_awaited_once()
        uow.clear_tracked_entities.assert_called_once()

    asyncio.run(_run())


def test_sql_uow_sets_events_dispatcher_from_config():
    async def _run():
        session = MagicMock()
        configured_dispatcher = AsyncMock()
        configured_dispatcher.dispatch = AsyncMock()
        configured = _DummyConfig()
        configured.event_dispatcher = configured_dispatcher

        with (
            patch(
                "hexcore.infrastructure.uow.LazyConfig.get_config",
                return_value=configured,
            ),
            patch(
                "hexcore.infrastructure.uow.discover_sql_repositories",
                return_value={"dummy": _DummyRepository},
            ),
        ):
            uow = SqlAlchemyUnitOfWork(session=session)

        assert uow.events_dispatcher is configured_dispatcher

    asyncio.run(_run())


def test_sql_uow_fail_fast_message_when_no_repositories_discovered():
    async def _run():
        session = MagicMock()
        configured = _DummyConfig()
        configured.repository_discovery_paths = {"myapp.features.payments.repositories"}

        with (
            patch(
                "hexcore.infrastructure.uow.LazyConfig.get_config",
                return_value=configured,
            ),
            patch(
                "hexcore.infrastructure.uow.discover_sql_repositories",
                return_value={},
            ),
        ):
            raised = None
            try:
                SqlAlchemyUnitOfWork(session=session)
            except RuntimeError as exc:
                raised = exc

        assert raised is not None
        assert "fallback implicito" in str(raised)
        assert "repository_discovery_paths" in str(raised)
        assert "myapp.features.payments.repositories" in str(raised)

    asyncio.run(_run())
