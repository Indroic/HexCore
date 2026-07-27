"""
Interfaces fundamentales para delegar mensajes a Task Queues (Celery, Procrastinate, RQ, ARQ).
"""
from __future__ import annotations

import abc
import typing as t

if t.TYPE_CHECKING:
    pass


class ITaskEnqueuer(abc.ABC):
    """
    Puerto (Puerto de Salida) genérico para encolar tareas asíncronas
    o en paralelo hacia un backend de colas de mensajes (Task Queue).
    
    Un adaptador concreto (ej. `CeleryTaskEnqueuer`) usará la SDK
    nativa del worker para empujar el `payload`.
    """

    @abc.abstractmethod
    async def enqueue_command(self, command_name: str, payload: dict[str, t.Any], queue: str) -> None:
        """
        Encola un comando de dominio serializado para ser ejecutado en background.
        
        Args:
            command_name: Nombre calificado del comando (e.g. 'CreateUserCommand').
            payload: Payload pre-serializado.
            queue: Nombre de la cola de destino.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def enqueue_event(self, event_name: str, payload: dict[str, t.Any], queue: str) -> None:
        """
        Encola un evento de dominio serializado para ser distribuido genéricamente.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def enqueue_handler(self, handler_name: str, payload: dict[str, t.Any], queue: str) -> None:
        """
        Encola la ejecución de un *Event Handler Específico* para un evento.
        Esto permite que un Evento dispare handlers asíncronos individuales.
        
        Args:
            handler_name: Nombre calificado del handler (func.__cqrs_handler_name__).
            payload: Payload pre-serializado del evento.
            queue: Nombre de la cola.
        """
        raise NotImplementedError

    @abc.abstractmethod
    async def enqueue_task(self, task_name: str, payload: dict[str, t.Any], queue: str) -> None:
        """
        Encola una tarea genérica de background (independiente de CQRS).
        
        Args:
            task_name: Nombre calificado de la tarea (func.__cqrs_task_name__).
            payload: Argumentos serializados para la tarea.
            queue: Nombre de la cola.
        """
        raise NotImplementedError
