"""
Consumidor Universal para colas de tareas (Celery, Procrastinate).
"""
from __future__ import annotations

import logging
import typing as t

from hexcore.domain.cqrs.buses import AbstractCommandBus, AbstractEventBus
from hexcore.domain.cqrs.context import worker_execution
from hexcore.domain.cqrs.resolution import resolve_dotted
from hexcore.domain.cqrs.serializer import AbstractSerializer
from hexcore.domain.events import DomainEvent

if t.TYPE_CHECKING:
    from hexcore.domain.cqrs.commands import Command

logger = logging.getLogger("hexcore.workers.consumer")


def _resolve_callable(qualname: str) -> t.Callable[..., t.Any]:
    """
    Resuelve un import por su nombre calificado (ej. 'myapp.events.on_user_created').

    Soporta nombres anidados (``myapp.jobs.Jobs.cleanup``) delegando en
    ``resolve_dotted``.
    """
    try:
        return t.cast(t.Callable[..., t.Any], resolve_dotted(qualname))
    except LookupError as exc:
        raise RuntimeError(f"No se pudo resolver el handler/tarea '{qualname}': {exc}") from exc


class CQRSConsumer:
    """
    Ayudante universal para recibir mensajes serializados desde un Worker 
    y despacharlos a los handlers locales usando buses en memoria.
    
    Uso (ej. Procrastinate)::

        consumer = CQRSConsumer(command_bus, event_bus)
        register_hexcore_procrastinate_tasks(app, consumer)

    Un worker sólo-comandos puede omitir el event bus::

        consumer = CQRSConsumer(command_bus)
    """

    def __init__(
        self,
        command_bus: AbstractCommandBus,
        event_bus: AbstractEventBus | None = None,
        serializer: AbstractSerializer | None = None,
    ) -> None:
        """
        Args:
            command_bus: El bus de comandos *local* (usualmente InMemoryCommandBus)
                         que tiene registrados los handlers.
            event_bus: El bus de eventos *local* (usualmente InMemoryEventBus).
                       Opcional: un worker sólo-comandos no lo necesita, y antes había
                       que pasar `cast(Any, None)` para satisfacer el tipo.
            serializer: El serializador usado. Por defecto `PydanticSerializer`.
        """
        if serializer is None:
            from hexcore.infrastructure.cqrs.pydantic_serializer import PydanticSerializer

            serializer = PydanticSerializer()

        self._command_bus = command_bus
        self._event_bus = event_bus
        self._serializer = serializer

    @property
    def event_bus(self) -> AbstractEventBus:
        """
        El event bus configurado.

        Raises:
            RuntimeError: Si el consumer se construyó sin event bus y llega un evento.
        """
        if self._event_bus is None:
            raise RuntimeError(
                "Llegó un evento pero este CQRSConsumer se construyó sin 'event_bus'. "
                "Pasá un AbstractEventBus al construirlo si el worker tiene que "
                "procesar eventos, o enrutá los eventos a otra cola."
            )
        return self._event_bus

    async def process_command(self, payload: dict[str, t.Any]) -> None:
        """
        Deserializa un payload de comando y lo despacha al bus local.

        El despacho ocurre dentro de ``worker_execution()``, así que un bus con
        Smart Routing **ejecuta** el comando en vez de reencolarlo. Esto permite
        compartir el mismo bus entre el proceso web y el worker.
        """
        try:
            command = self._serializer.deserialize(payload)
            cmd = t.cast("Command", command)

            logger.info("[CQRSConsumer] Procesando comando en background: %s", type(cmd).__qualname__)
            with worker_execution():
                await self._command_bus.dispatch(cmd)

        except Exception as exc:
            logger.exception("[CQRSConsumer] Error procesando comando: %s", exc)
            raise

    async def process_event(self, payload: dict[str, t.Any]) -> None:
        """
        Deserializa un payload de evento y lo publica en el bus local
        para que todos los handlers (suscriptores) locales lo consuman.

        Igual que ``process_command``: se publica dentro de ``worker_execution()``,
        así que los handlers marcados con ``@background_handler`` se ejecutan aquí
        en vez de reencolarse.
        """
        try:
            event = self._serializer.deserialize(payload)
            evt = t.cast(DomainEvent, event)

            logger.info("[CQRSConsumer] Procesando evento en background: %s", evt.event_name)
            with worker_execution():
                await self.event_bus.publish(evt)

        except Exception as exc:
            logger.exception("[CQRSConsumer] Error procesando evento: %s", exc)
            raise

    async def process_handler(self, handler_name: str, payload: dict[str, t.Any]) -> None:
        """
        Deserializa el evento y ejecuta *específicamente* un handler decorado con `@background_handler`.
        Esto es inyectado por el Smart Routing del EventBus.

        Nota: aquí **no** se activa ``worker_execution()``. El handler se invoca
        directamente (no hay bus que consuma el flag), así que dejarlo activo haría
        que los comandos que el handler despache a propósito se ejecutaran inline.
        """
        try:
            event = self._serializer.deserialize(payload)
            evt = t.cast(DomainEvent, event)
            
            logger.info("[CQRSConsumer] Ejecutando EventHandler individual en background: %s", handler_name)
            
            handler_func = _resolve_callable(handler_name)
            await handler_func(evt)
            
        except Exception as exc:
            logger.exception("[CQRSConsumer] Error procesando EventHandler %s: %s", handler_name, exc)
            raise

    async def process_task(self, task_name: str, payload: dict[str, t.Any]) -> None:
        """
        Ejecuta una tarea genérica de background decorada con `@background_task`.

        Igual que ``process_handler``: la tarea se invoca directamente, sin activar
        ``worker_execution()``.
        """
        try:
            logger.info("[CQRSConsumer] Ejecutando Task genérica en background: %s", task_name)
            
            task_func = _resolve_callable(task_name)
            # El payload en tareas genéricas son los kwargs
            await task_func(**payload)
            
        except Exception as exc:
            logger.exception("[CQRSConsumer] Error ejecutando Task %s: %s", task_name, exc)
            raise
