import pytest
import sys
import asyncio
from unittest.mock import MagicMock, AsyncMock, ANY

# Mockear dependencias opcionales globalmente
sys.modules["celery"] = MagicMock()
sys.modules["procrastinate"] = MagicMock()

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
