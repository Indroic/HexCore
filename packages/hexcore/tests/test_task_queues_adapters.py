import pytest
import sys
import asyncio
from unittest.mock import MagicMock, AsyncMock, ANY

# Mockear dependencias opcionales globalmente
sys.modules["celery"] = MagicMock()
sys.modules["procrastinate"] = MagicMock()

from hexcore.infrastructure.task_queues import celery_adapter, procrastinate_adapter
from hexcore.infrastructure.task_queues.celery_adapter import CeleryEnqueuer, register_hexcore_celery_tasks
from hexcore.infrastructure.task_queues.procrastinate_adapter import ProcrastinateEnqueuer, register_hexcore_procrastinate_tasks


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.mark.anyio
async def test_celery_enqueuer():
    mock_app = MagicMock()
    enqueuer = CeleryEnqueuer(mock_app)
    
    await enqueuer.enqueue_command("test_cmd", {"data": 1}, "default")
    mock_app.send_task.assert_called_once_with(
        "hexcore.process_command",
        kwargs={"payload": {"data": 1}},
        queue="default"
    )
    
    mock_app.reset_mock()
    await enqueuer.enqueue_handler("test_handler", {"data": 2}, "high")
    mock_app.send_task.assert_called_once_with(
        "hexcore.process_handler",
        kwargs={"handler_name": "test_handler", "payload": {"data": 2}},
        queue="high"
    )


def test_register_celery_tasks():
    mock_app = MagicMock()
    mock_consumer = MagicMock()
    
    register_hexcore_celery_tasks(mock_app, mock_consumer)
    
    assert mock_app.task.call_count == 3
    # Check that task bindings are registered
    args = [call.kwargs.get("name") for call in mock_app.task.call_args_list]
    assert "hexcore.process_command" in args
    assert "hexcore.process_handler" in args
    assert "hexcore.process_task" in args


@pytest.mark.anyio
async def test_procrastinate_enqueuer():
    mock_app = MagicMock()
    mock_task = AsyncMock()
    mock_app.configure_task.return_value = mock_task
    
    enqueuer = ProcrastinateEnqueuer(mock_app)
    
    await enqueuer.enqueue_command("test_cmd", {"data": 1}, "default")
    mock_app.configure_task.assert_called_with(name="hexcore.process_command", queue="default")
    mock_task.defer_async.assert_awaited_once_with(payload={"data": 1})
    
    mock_task.reset_mock()
    await enqueuer.enqueue_task("my_task", {"data": 3}, "low")
    mock_app.configure_task.assert_called_with(name="hexcore.process_task", queue="low")
    mock_task.defer_async.assert_awaited_once_with(task_name="my_task", payload={"data": 3})


def test_register_procrastinate_tasks():
    mock_app = MagicMock()
    mock_consumer = MagicMock()

    register_hexcore_procrastinate_tasks(mock_app, mock_consumer)

    assert mock_app.task.call_count == 3
    args = [call.kwargs.get("name") for call in mock_app.task.call_args_list]
    assert "hexcore.process_command" in args
    assert "hexcore.process_handler" in args
    assert "hexcore.process_task" in args


# ── P1-3: enqueue_event no puede tragarse el evento en silencio ───────────────


@pytest.mark.anyio
async def test_procrastinate_enqueue_event_raises_instead_of_losing_the_event():
    enqueuer = ProcrastinateEnqueuer(MagicMock())

    with pytest.raises(NotImplementedError, match="background_handler"):
        await enqueuer.enqueue_event("SomeEvent", {"data": 1}, "default")


@pytest.mark.anyio
async def test_celery_enqueue_event_raises_instead_of_losing_the_event():
    enqueuer = CeleryEnqueuer(MagicMock())

    with pytest.raises(NotImplementedError, match="background_handler"):
        await enqueuer.enqueue_event("SomeEvent", {"data": 1}, "default")


# ── P1-6: registrar dos veces no debe reventar ─────────────────────────────────


class FakeApp:
    """App mínima referenciable débilmente que rechaza nombres duplicados."""

    def __init__(self) -> None:
        self.registered: list[str] = []

    def task(self, *args, **kwargs):
        name = kwargs.get("name")

        def decorator(func):
            if name in self.registered:
                raise ValueError(f"Task name already registered: {name}")
            self.registered.append(name)
            return func

        return decorator


def test_register_procrastinate_tasks_is_idempotent():
    app = FakeApp()
    consumer = MagicMock()

    assert register_hexcore_procrastinate_tasks(app, consumer) is True
    assert procrastinate_adapter.is_registered(app) is True
    # Sin idempotencia, esta segunda llamada revienta con "already registered".
    assert register_hexcore_procrastinate_tasks(app, consumer) is False
    assert app.registered == list(procrastinate_adapter.HEXCORE_TASK_NAMES)


def test_register_celery_tasks_is_idempotent():
    app = FakeApp()
    consumer = MagicMock()

    assert register_hexcore_celery_tasks(app, consumer) is True
    assert celery_adapter.is_registered(app) is True
    assert register_hexcore_celery_tasks(app, consumer) is False
    assert app.registered == list(celery_adapter.HEXCORE_TASK_NAMES)


def test_is_registered_is_per_app():
    app_a, app_b = FakeApp(), FakeApp()
    register_hexcore_procrastinate_tasks(app_a, MagicMock())

    assert procrastinate_adapter.is_registered(app_a) is True
    assert procrastinate_adapter.is_registered(app_b) is False


def test_force_reregisters():
    app = FakeApp()
    register_hexcore_celery_tasks(app, MagicMock())

    # `force` reintenta el registro; esta app lo rechaza, que es justo lo que
    # documenta el parámetro.
    with pytest.raises(ValueError, match="already registered"):
        register_hexcore_celery_tasks(app, MagicMock(), force=True)
