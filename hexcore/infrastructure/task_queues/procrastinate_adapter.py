"""
Adaptador para Procrastinate.
Permite encolar comandos y tareas usando Procrastinate, y proporciona 
utilidades para auto-registrar las tareas asíncronas en el Worker.
"""
from __future__ import annotations

import logging
import typing as t
import weakref

from hexcore.domain.cqrs.task_queues import ITaskEnqueuer

logger = logging.getLogger("hexcore.task_queues.procrastinate")

if t.TYPE_CHECKING:
    # El import va pelado, sin `try/except ImportError: procrastinate = t.Any`, que es como
    # estaba. Ese respaldo no protegía nada: el bloque entero está bajo `TYPE_CHECKING`, así
    # que en runtime no se ejecuta y no hay `ImportError` posible. Lo único que lograba era
    # que Pyright —que analiza las dos ramas y se queda con la última definición— resolviera
    # `procrastinate` a `Any`, y con eso **toda** firma que lo mencione se degradaba a `Any`
    # incluso con el extra instalado. Justo al revés de para qué existe el bloque.
    import procrastinate

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
        raise NotImplementedError(
            "ProcrastinateEnqueuer no encola eventos completos: Procrastinate es una "
            "cola de tareas, no un bus de fan-out, así que un evento encolado aquí no "
            "llegaría a los suscriptores. Para ejecutar un suscriptor concreto en "
            "background, decoralo con @background_handler (el EventBus llamará a "
            "enqueue_handler). Para fan-out real, usá RedisEventBus o PostgresEventBus."
        )

    async def enqueue_handler(self, handler_name: str, payload: dict[str, t.Any], queue: str) -> None:
        task = self.app.configure_task(name="hexcore.process_handler", queue=queue)
        await task.defer_async(handler_name=handler_name, payload=payload)

    async def enqueue_task(self, task_name: str, payload: dict[str, t.Any], queue: str) -> None:
        task = self.app.configure_task(name="hexcore.process_task", queue=queue)
        await task.defer_async(task_name=task_name, payload=payload)


HEXCORE_TASK_NAMES = (
    "hexcore.process_command",
    "hexcore.process_handler",
    "hexcore.process_task",
)

# Apps en las que ya se registraron las tareas. Se guarda por `id()` en un
# WeakValueDictionary para no mantener viva la app y para tolerar varias apps en el
# mismo proceso (tests, scripts que construyen y descartan apps).
_registered_apps: "weakref.WeakValueDictionary[int, t.Any]" = weakref.WeakValueDictionary()


def is_registered(app: "procrastinate.App") -> bool:
    """Indica si `register_hexcore_procrastinate_tasks` ya corrió sobre esta app."""
    return id(app) in _registered_apps


def register_hexcore_procrastinate_tasks(
    app: "procrastinate.App",
    consumer: "CQRSConsumer",
    *,
    force: bool = False,
) -> bool:
    """
    Auto-registra las tareas base de HexCore en una aplicación Procrastinate.

    Es **idempotente**: llamarla dos veces sobre la misma app no hace nada la segunda
    vez, en vez de reventar porque Procrastinate rechaza nombres duplicados. Antes cada
    aplicación tenía que protegerla con un flag de módulo.

    Args:
        app: La aplicación de Procrastinate.
        consumer: El `CQRSConsumer` que ejecutará los mensajes.
        force: Registrar aunque ya se hubiera registrado. Útil para rebindear el
            consumer; puede fallar si Procrastinate rechaza el nombre duplicado.

    Returns:
        True si se registraron las tareas, False si ya estaban registradas.
    """
    if not force and is_registered(app):
        logger.debug(
            "Las tareas de HexCore ya estaban registradas en esta app de Procrastinate; "
            "no se vuelven a registrar."
        )
        return False

    # Los tres se registran por **efecto de lado** del decorador: `app.task(...)` las mete en
    # el registro de la app y el nombre local no lo lee nadie.
    @app.task(name="hexcore.process_command")
    async def _process_command(payload: dict[str, t.Any]) -> None:
        await consumer.process_command(payload)

    @app.task(name="hexcore.process_handler")
    async def _process_handler(handler_name: str, payload: dict[str, t.Any]) -> None:
        await consumer.process_handler(handler_name, payload)

    @app.task(name="hexcore.process_task")
    async def _process_task(task_name: str, payload: dict[str, t.Any]) -> None:
        await consumer.process_task(task_name, payload)

    # Nombrarlas acá es lo que las separa de código muerto, para el checker y para quien lee.
    # El prefijo con guion bajo no alcanza: `reportUnusedFunction` no exime los nombres
    # privados como sí hace `reportUnusedVariable`.
    _registradas = (_process_command, _process_handler, _process_task)

    try:
        _registered_apps[id(app)] = app
    except TypeError:
        # Un objeto no referenciable débilmente (p. ej. un mock exótico): la
        # idempotencia no se puede memorizar, pero el registro ya ocurrió.
        logger.debug("No se pudo memorizar el registro para esta app de Procrastinate.")
    return True
