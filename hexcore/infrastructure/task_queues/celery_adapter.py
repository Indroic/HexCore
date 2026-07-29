"""
Adaptador para Celery.
Permite encolar comandos y tareas usando Celery, y proporciona 
utilidades para auto-registrar las tareas asíncronas en el Worker.
"""
from __future__ import annotations

import asyncio
import typing as t

from hexcore.domain.cqrs.task_queues import ITaskEnqueuer

if t.TYPE_CHECKING:
    try:
        from celery import Celery
    except ImportError:
        Celery = t.Any
        
    from hexcore.infrastructure.workers.consumer import CQRSConsumer


class CeleryEnqueuer(ITaskEnqueuer):
    """
    Adaptador que utiliza Celery para encolar comandos, eventos y tareas.
    Dado que `ITaskEnqueuer` tiene métodos asíncronos y Celery `.send_task` 
    es síncrono (bloqueante), utiliza `asyncio.to_thread` para no congelar
    el Event Loop principal (ej. FastAPI).
    """

    def __init__(self, app: "Celery") -> None:
        self.app = app

    async def enqueue_command(self, command_name: str, payload: dict[str, t.Any], queue: str) -> None:
        await asyncio.to_thread(
            self.app.send_task,
            "hexcore.process_command",
            kwargs={"payload": payload},
            queue=queue,
        )

    async def enqueue_event(self, event_name: str, payload: dict[str, t.Any], queue: str) -> None:
        raise NotImplementedError(
            "CeleryEnqueuer no encola eventos completos: Celery es una cola de tareas, "
            "no un bus de fan-out, así que un evento encolado aquí no llegaría a los "
            "suscriptores. Para ejecutar un suscriptor concreto en background, "
            "decoralo con @background_handler (el EventBus llamará a enqueue_handler). "
            "Para fan-out real, usá RedisEventBus o PostgresEventBus."
        )

    async def enqueue_handler(self, handler_name: str, payload: dict[str, t.Any], queue: str) -> None:
        await asyncio.to_thread(
            self.app.send_task,
            "hexcore.process_handler",
            kwargs={"handler_name": handler_name, "payload": payload},
            queue=queue,
        )

    async def enqueue_task(self, task_name: str, payload: dict[str, t.Any], queue: str) -> None:
        await asyncio.to_thread(
            self.app.send_task,
            "hexcore.process_task",
            kwargs={"task_name": task_name, "payload": payload},
            queue=queue,
        )


def register_hexcore_celery_tasks(app: "Celery", consumer: "CQRSConsumer") -> None:
    """
    Auto-registra las tareas base de HexCore en una aplicación Celery.
    Esto permite que un worker de Celery esté listo para recibir mensajes
    generados por el Smart Routing de HexCore.
    
    Dado que las tareas de Celery son síncronas y el Consumer de HexCore es 
    asíncrono, se envuelven en `asyncio.run()`.
    """

    @app.task(name="hexcore.process_command", bind=True)
    def process_command(self: t.Any, payload: dict[str, t.Any]) -> None:
        asyncio.run(consumer.process_command(payload))

    @app.task(name="hexcore.process_handler", bind=True)
    def process_handler(self: t.Any, handler_name: str, payload: dict[str, t.Any]) -> None:
        asyncio.run(consumer.process_handler(handler_name, payload))

    @app.task(name="hexcore.process_task", bind=True)
    def process_task(self: t.Any, task_name: str, payload: dict[str, t.Any]) -> None:
        asyncio.run(consumer.process_task(task_name, payload))
