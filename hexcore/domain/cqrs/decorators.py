"""
Decoradores para habilitar el enrutamiento inteligente (Smart Routing) de tareas, comandos
y manejadores de eventos hacia Task Queues (Celery, Procrastinate, etc.) de forma transparente.
"""
from __future__ import annotations

import typing as t


def background_command(queue: str = "default") -> t.Callable[[t.Type[t.Any]], t.Type[t.Any]]:
    """
    Decora una clase `Command` para indicar que su ejecución debe enviarse siempre
    a un procesador en segundo plano (Task Queue).
    
    Cuando el CommandBus detecta este decorador, no ejecutará el comando localmente,
    sino que llamará a `ITaskEnqueuer.enqueue_command()`.
    
    Ejemplo:
        @background_command(queue="emails")
        class SendEmailCommand(Command):
            ...
    """
    def wrapper(cls: t.Type[t.Any]) -> t.Type[t.Any]:
        cls.__cqrs_background__ = True
        cls.__cqrs_queue__ = queue
        return cls

    return wrapper


def background_handler(queue: str = "default") -> t.Callable[[t.Callable[..., t.Any]], t.Callable[..., t.Any]]:
    """
    Decora un manejador de eventos (Event Handler) para indicar que su ejecución
    debe ocurrir de forma asíncrona en un Worker de background.
    
    Cuando el EventBus detecta este decorador en un handler suscrito, en lugar de
    ejecutarlo directamente, delegará la tarea a `ITaskEnqueuer.enqueue_handler()`.
    
    Ejemplo:
        @background_handler(queue="analytics")
        async def on_user_created(event: UserCreatedEvent):
            ...
    """
    def wrapper(func: t.Callable[..., t.Any]) -> t.Callable[..., t.Any]:
        func.__cqrs_background_handler__ = True
        func.__cqrs_queue__ = queue
        
        # Guardamos el nombre calificado del handler para poder resolverlo luego en el Worker
        if not hasattr(func, "__qualname__"):
            func.__cqrs_handler_name__ = func.__name__
        else:
            func.__cqrs_handler_name__ = f"{func.__module__}.{func.__qualname__}"
            
        return func

    return wrapper


def background_task(queue: str = "default") -> t.Callable[[t.Callable[..., t.Any]], t.Callable[..., t.Any]]:
    """
    Decora una función genérica para convertirla en una Tarea de Background
    (independiente de CQRS).
    
    La función original queda marcada, y puede ser registrada en el
    `TaskManager` de HexCore o ejecutada directamente usando el `ITaskEnqueuer`.
    """
    def wrapper(func: t.Callable[..., t.Any]) -> t.Callable[..., t.Any]:
        func.__cqrs_background_task__ = True
        func.__cqrs_queue__ = queue
        
        if not hasattr(func, "__qualname__"):
            func.__cqrs_task_name__ = func.__name__
        else:
            func.__cqrs_task_name__ = f"{func.__module__}.{func.__qualname__}"
            
        return func

    return wrapper
