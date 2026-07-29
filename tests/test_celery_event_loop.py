"""
P1-4: el adaptador de Celery ya no crea (y cierra) un event loop por tarea.

`asyncio.run()` por tarea, con un `AsyncEngine` de SQLAlchemy compartido, produce
`Event loop is closed` y `Future attached to a different loop`: el pool guarda conexiones
atadas al loop de la tarea anterior, que ya no existe. Es el problema que llevó a la app
real a abandonar Celery.
"""
from __future__ import annotations

import asyncio
import sys
import threading
import typing as t
from unittest.mock import MagicMock

import pytest

sys.modules.setdefault("celery", MagicMock())

from hexcore.infrastructure.task_queues import celery_adapter  # noqa: E402
from hexcore.infrastructure.task_queues.celery_adapter import (  # noqa: E402
    register_hexcore_celery_tasks,
    run_in_worker_loop,
    shutdown_worker_loop,
)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _fresh_loop():
    shutdown_worker_loop()
    yield
    shutdown_worker_loop()


class CollectingApp:
    """App de Celery mínima que guarda las funciones registradas."""

    def __init__(self) -> None:
        self.tasks: dict[str, t.Callable[..., t.Any]] = {}

    def task(self, *args: t.Any, **kwargs: t.Any):
        name = kwargs["name"]

        def decorator(func):
            self.tasks[name] = func
            return func

        return decorator


def test_the_same_loop_is_reused_across_calls():
    loops: list[int] = []

    async def record() -> None:
        loops.append(id(asyncio.get_running_loop()))

    for _ in range(5):
        run_in_worker_loop(record())

    assert len(set(loops)) == 1, "se creó un event loop distinto por llamada"


def test_the_loop_stays_open_between_calls():
    """El síntoma exacto: la segunda llamada veía el loop de la primera ya cerrado."""
    captured: list[asyncio.AbstractEventLoop] = []

    async def capture() -> None:
        captured.append(asyncio.get_running_loop())

    run_in_worker_loop(capture())
    first = captured[0]

    assert first.is_closed() is False

    run_in_worker_loop(capture())

    assert captured[1] is first


def test_an_asyncio_primitive_survives_between_tasks():
    """
    Lo que rompía de verdad: un objeto atado al loop (aquí un Event; en producción el
    pool del AsyncEngine) creado en una tarea y usado en la siguiente.
    """
    holder: dict[str, asyncio.Event] = {}

    async def create() -> None:
        holder["event"] = asyncio.Event()

    async def use() -> None:
        holder["event"].set()
        await holder["event"].wait()

    run_in_worker_loop(create())
    run_in_worker_loop(use())  # con asyncio.run() esto lanza

    assert holder["event"].is_set()


def test_results_are_returned():
    async def compute() -> int:
        return 42

    assert run_in_worker_loop(compute()) == 42


def test_exceptions_propagate_to_the_caller():
    async def boom() -> None:
        raise ValueError("falló el handler")

    with pytest.raises(ValueError, match="falló el handler"):
        run_in_worker_loop(boom())


def test_the_loop_runs_in_a_dedicated_named_thread():
    names: list[str] = []

    async def record() -> None:
        names.append(threading.current_thread().name)

    run_in_worker_loop(record())

    assert names == ["hexcore-celery-loop"]
    assert threading.current_thread().name != "hexcore-celery-loop"


def test_shutdown_closes_the_loop_and_a_new_one_is_created():
    captured: list[asyncio.AbstractEventLoop] = []

    async def capture() -> None:
        captured.append(asyncio.get_running_loop())

    run_in_worker_loop(capture())
    first = captured[0]

    shutdown_worker_loop()
    assert first.is_closed() is True

    run_in_worker_loop(capture())
    assert captured[1] is not first


def test_shutdown_is_idempotent():
    shutdown_worker_loop()
    shutdown_worker_loop()


def test_a_fork_gets_a_fresh_loop():
    """
    Con el pool prefork de Celery, un loop creado antes del fork no sirve en el hijo. El
    adaptador lo detecta comparando el PID.
    """
    async def noop() -> None:
        return None

    run_in_worker_loop(noop())
    loop_before = celery_adapter._LOOP._loop
    pid_before = celery_adapter._LOOP._pid

    # Se simula el fork falseando el PID registrado.
    celery_adapter._LOOP._pid = (pid_before or 0) + 1
    run_in_worker_loop(noop())

    assert celery_adapter._LOOP._loop is not loop_before


def test_registered_tasks_use_the_persistent_loop():
    app = CollectingApp()
    seen: list[int] = []

    class Consumer:
        async def process_command(self, payload: dict) -> None:
            seen.append(id(asyncio.get_running_loop()))

        async def process_handler(self, handler_name: str, payload: dict) -> None:
            seen.append(id(asyncio.get_running_loop()))

        async def process_task(self, task_name: str, payload: dict) -> None:
            seen.append(id(asyncio.get_running_loop()))

    register_hexcore_celery_tasks(t.cast(t.Any, app), t.cast(t.Any, Consumer()))

    app.tasks["hexcore.process_command"](None, {"a": 1})
    app.tasks["hexcore.process_handler"](None, "h", {"a": 1})
    app.tasks["hexcore.process_task"](None, "t", {"a": 1})

    assert len(seen) == 3
    assert len(set(seen)) == 1, "las tareas no comparten el event loop"


def test_concurrent_task_submissions_share_the_loop():
    loops: list[int] = []
    lock = threading.Lock()

    async def record() -> None:
        with lock:
            loops.append(id(asyncio.get_running_loop()))

    def submit() -> None:
        run_in_worker_loop(record())

    threads = [threading.Thread(target=submit) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(loops) == 6
    assert len(set(loops)) == 1
