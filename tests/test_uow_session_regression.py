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


def _build_sql_uow(session: Any) -> SqlAlchemyUnitOfWork:
    with patch("hexcore.infrastructure.uow.discover_sql_repositories", return_value={}):
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
