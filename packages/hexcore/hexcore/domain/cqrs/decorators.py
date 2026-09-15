"""
Decoradores para habilitar el enrutamiento inteligente (Smart Routing) de tareas, comandos
y manejadores de eventos hacia Task Queues (Celery, Procrastinate, etc.) de forma transparente.
"""
from __future__ import annotations

import typing as t

from .resolution import build_fqn, ensure_resolvable_qualname


def _unwrap(func: t.Any) -> t.Any:
    """Descarta envoltorios (`staticmethod`/`classmethod`) para leer el objeto real."""
    return getattr(func, "__func__", func)


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
        # El worker tiene que poder importar la clase para deserializar el payload.
        ensure_resolvable_qualname(cls, decorator="background_command")
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
        target = _unwrap(func)
        ensure_resolvable_qualname(target, decorator="background_handler")

        target.__cqrs_background_handler__ = True
        target.__cqrs_queue__ = queue

        # Guardamos el nombre calificado del handler para poder resolverlo luego en
        # el Worker. `build_fqn` conserva el __qualname__ completo (métodos incluidos)
        # y `resolve_dotted` sabe resolverlo del otro lado.
        target.__cqrs_handler_name__ = build_fqn(target)

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
        target = _unwrap(func)
        ensure_resolvable_qualname(target, decorator="background_task")

        target.__cqrs_background_task__ = True
        target.__cqrs_queue__ = queue
        target.__cqrs_task_name__ = build_fqn(target)

        return func

    return wrapper
