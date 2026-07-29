"""
Adaptador para Celery.
Permite encolar comandos y tareas usando Celery, y proporciona 
utilidades para auto-registrar las tareas asíncronas en el Worker.
"""
from __future__ import annotations

import asyncio
import logging
import typing as t
import weakref

from hexcore.domain.cqrs.task_queues import ITaskEnqueuer

logger = logging.getLogger("hexcore.task_queues.celery")

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


HEXCORE_TASK_NAMES = (
    "hexcore.process_command",
    "hexcore.process_handler",
    "hexcore.process_task",
)

_registered_apps: "weakref.WeakValueDictionary[int, t.Any]" = weakref.WeakValueDictionary()


def is_registered(app: "Celery") -> bool:
    """Indica si `register_hexcore_celery_tasks` ya corrió sobre esta app."""
    return id(app) in _registered_apps


def register_hexcore_celery_tasks(
    app: "Celery",
    consumer: "CQRSConsumer",
    *,
    force: bool = False,
) -> bool:
    """
    Auto-registra las tareas base de HexCore en una aplicación Celery.
    Esto permite que un worker de Celery esté listo para recibir mensajes
    generados por el Smart Routing de HexCore.

    Es **idempotente**: llamarla dos veces sobre la misma app no vuelve a registrar.
    Antes cada aplicación tenía que protegerla con un flag de módulo.

    Dado que las tareas de Celery son síncronas y el Consumer de HexCore es
    asíncrono, se envuelven en `asyncio.run()`.

    Returns:
        True si se registraron las tareas, False si ya estaban registradas.
    """
    if not force and is_registered(app):
        logger.debug(
            "Las tareas de HexCore ya estaban registradas en esta app de Celery; "
            "no se vuelven a registrar."
        )
        return False

    @app.task(name="hexcore.process_command", bind=True)
    def process_command(self: t.Any, payload: dict[str, t.Any]) -> None:
        asyncio.run(consumer.process_command(payload))

    @app.task(name="hexcore.process_handler", bind=True)
    def process_handler(self: t.Any, handler_name: str, payload: dict[str, t.Any]) -> None:
        asyncio.run(consumer.process_handler(handler_name, payload))

    @app.task(name="hexcore.process_task", bind=True)
    def process_task(self: t.Any, task_name: str, payload: dict[str, t.Any]) -> None:
        asyncio.run(consumer.process_task(task_name, payload))

    try:
        _registered_apps[id(app)] = app
    except TypeError:
        logger.debug("No se pudo memorizar el registro para esta app de Celery.")
    return True
