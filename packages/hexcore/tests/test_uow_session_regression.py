import asyncio
from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
pytest.importorskip("sqlalchemy")

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
        self.event_bus = AsyncMock()
        self.repository_discovery_paths = {
            "myapp.features.users.infrastructure.repositories"
        }


def _build_sql_uow(session: Any, **opciones: Any) -> SqlAlchemyUnitOfWork:
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
        return SqlAlchemyUnitOfWork(session=session, **opciones)


def test_get_async_db_session_no_manual_rollback_on_error():
    async def _run():
        session = MagicMock()
        session.rollback = AsyncMock()

        with patch(
            "hexcore.infrastructure.repositories.orms.sqlalchemy.session.get_session_factory",
            return_value=lambda: _DummySessionContext(session),
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
    """
    El `finally` que limpia las entidades trackeadas aunque la publicacion falle.

    Se mockea `_publish` y no `dispatch_events`: desde 9.0, `commit()` recolecta los eventos
    **antes** de comitear y los publica despues, asi que ya no pasa por `dispatch_events()`
    -- esa quedo para quien la llame a mano. La propiedad que este test fija no cambio.
    """

    async def _run():
        session = MagicMock()
        session.commit = AsyncMock()

        uow = _build_sql_uow(session)
        uow.collect_domain_events = MagicMock(return_value=["un-evento"])
        uow._publish = AsyncMock(side_effect=RuntimeError("dispatch failed"))
        uow.clear_tracked_entities = MagicMock()

        raised = None
        try:
            await uow.commit()
        except RuntimeError as exc:
            raised = exc

        assert raised is not None
        session.commit.assert_awaited_once()
        uow._publish.assert_awaited_once()
        uow.clear_tracked_entities.assert_called_once()

    asyncio.run(_run())


def test_sql_uow_recolecta_los_eventos_antes_de_comitear():
    """
    Regresion del defecto que 9.0 cierra.

    `commit()` comiteaba primero y recien despues llamaba a `collect_domain_events()`, que
    recorre `session.new | dirty | deleted`. Post-commit esas tres colecciones estan vacias,
    asi que el UoW no recogia ningun evento y no publicaba nada -- sin error y sin log.

    Se fija el **orden de las llamadas**, no el resultado: verificar que se publico algo
    pasaria igual si la recoleccion siguiera siendo posterior pero la sesion fuera un mock
    que no se vacia.
    """

    async def _run():
        orden: list[str] = []

        session = MagicMock()

        async def commit_de_sesion():
            orden.append("session.commit")

        session.commit = AsyncMock(side_effect=commit_de_sesion)

        uow = _build_sql_uow(session)

        def recolectar():
            orden.append("collect")
            return ["un-evento"]

        async def publicar(_eventos):
            orden.append("publish")

        uow.collect_domain_events = MagicMock(side_effect=recolectar)
        uow._publish = AsyncMock(side_effect=publicar)

        await uow.commit()

        assert orden == ["collect", "session.commit", "publish"], (
            f"el orden fue {orden}: recolectar despues del commit no encuentra ningun "
            f"evento, porque SQLAlchemy ya vacio session.new|dirty|deleted"
        )

    asyncio.run(_run())


def test_sql_uow_escribe_en_el_store_antes_de_comitear():
    """
    La garantia que hace del event store algo mas que un log paralelo: el evento y el cambio
    entran en la misma transaccion, o no entra ninguno.
    """

    async def _run():
        orden: list[str] = []

        session = MagicMock()

        async def commit_de_sesion():
            orden.append("session.commit")

        session.commit = AsyncMock(side_effect=commit_de_sesion)

        store = AsyncMock()

        async def append(*_args, **_kwargs):
            orden.append("store.append")
            return []

        store.append = AsyncMock(side_effect=append)

        uow = _build_sql_uow(session)
        uow.event_store = store
        uow.collect_domain_events = MagicMock(return_value=["un-evento"])
        uow._publish = AsyncMock()

        await uow.commit()

        assert orden == ["store.append", "session.commit"]

    asyncio.run(_run())


def test_sql_uow_sin_store_no_intenta_escribir():
    """El enganche es opcional: sin store configurado, el UoW se comporta como antes."""

    async def _run():
        session = MagicMock()
        session.commit = AsyncMock()

        uow = _build_sql_uow(session)
        uow.collect_domain_events = MagicMock(return_value=["un-evento"])
        uow._publish = AsyncMock()

        await uow.commit()

        assert uow.event_store is None
        uow._publish.assert_awaited_once()

    asyncio.run(_run())


def test_sql_uow_publish_after_commit_false_no_publica():
    """
    Lo que evita la doble publicacion cuando corre un relay: el relay lee del almacen y
    publica, asi que con los dos activos cada evento saldria dos veces.
    """

    async def _run():
        session = MagicMock()
        session.commit = AsyncMock()

        uow = _build_sql_uow(session, publish_after_commit=False)
        uow.collect_domain_events = MagicMock(return_value=["un-evento"])
        uow._publish = AsyncMock()

        await uow.commit()

        uow._publish.assert_not_awaited()

    asyncio.run(_run())


def test_sql_uow_sets_event_bus_from_config():
    async def _run():
        session = MagicMock()
        configured_bus = AsyncMock()
        configured_bus.publish = AsyncMock()
        configured = _DummyConfig()
        configured.event_bus = configured_bus

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

        assert uow.event_bus is configured_bus

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
