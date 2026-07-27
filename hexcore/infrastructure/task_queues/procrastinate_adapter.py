"""
Adaptador para Procrastinate.
Permite encolar comandos y tareas usando Procrastinate, y proporciona 
utilidades para auto-registrar las tareas asíncronas en el Worker.
"""
from __future__ import annotations

import typing as t

from hexcore.domain.cqrs.task_queues import ITaskEnqueuer

if t.TYPE_CHECKING:
    try:
        import procrastinate
    except ImportError:
        procrastinate = t.Any
        
    from hexcore.infrastructure.workers.consumer import CQRSConsumer


class ProcrastinateEnqueuer(ITaskEnqueuer):
    """
    Adaptador que utiliza Procrastinate (PostgreSQL) para encolar comandos y tareas.
    Totalmente asíncrono y nativo.
    """

    def __init__(self, app: "procrastinate.App") -> None:
        self.app = app

    async def enqueue_command(self, command_name: str, payload: dict[str, t.Any], queue: str) -> None:
        task = self.app.configure_task(name="hexcore.process_command", queue=queue)
        await task.defer_async(payload=payload)

    async def enqueue_event(self, event_name: str, payload: dict[str, t.Any], queue: str) -> None:
        pass

    async def enqueue_handler(self, handler_name: str, payload: dict[str, t.Any], queue: str) -> None:
        task = self.app.configure_task(name="hexcore.process_handler", queue=queue)
        await task.defer_async(handler_name=handler_name, payload=payload)

    async def enqueue_task(self, task_name: str, payload: dict[str, t.Any], queue: str) -> None:
        task = self.app.configure_task(name="hexcore.process_task", queue=queue)
        await task.defer_async(task_name=task_name, payload=payload)


def register_hexcore_procrastinate_tasks(app: "procrastinate.App", consumer: "CQRSConsumer") -> None:
    """
    Auto-registra las tareas base de HexCore en una aplicación Procrastinate.
    """

    @app.task(name="hexcore.process_command")
    async def process_command(payload: dict[str, t.Any]) -> None:
        await consumer.process_command(payload)

    @app.task(name="hexcore.process_handler")
    async def process_handler(handler_name: str, payload: dict[str, t.Any]) -> None:
        await consumer.process_handler(handler_name, payload)

    @app.task(name="hexcore.process_task")
    async def process_task(task_name: str, payload: dict[str, t.Any]) -> None:
        await consumer.process_task(task_name, payload)
