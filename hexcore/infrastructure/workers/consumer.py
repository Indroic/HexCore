"""
Consumidor Universal para colas de tareas (Celery, Procrastinate).
"""
from __future__ import annotations

import importlib
import logging
import typing as t

from hexcore.domain.cqrs.buses import AbstractCommandBus, AbstractEventBus
from hexcore.domain.cqrs.serializer import ISerializer
from hexcore.domain.events import DomainEvent

if t.TYPE_CHECKING:
    from hexcore.domain.cqrs.commands import Command

logger = logging.getLogger("hexcore.workers.consumer")


def _resolve_callable(qualname: str) -> t.Callable[..., t.Any]:
    """Resuelve un import por su nombre calificado (ej. 'myapp.events.on_user_created')."""
    try:
        module_path, func_name = qualname.rsplit(".", 1)
        module = importlib.import_module(module_path)
        return getattr(module, func_name) # type: ignore
    except (ValueError, ModuleNotFoundError, AttributeError) as exc:
        raise RuntimeError(f"No se pudo resolver el handler/tarea '{qualname}': {exc}") from exc


class CQRSConsumer:
    """
    Ayudante universal para recibir mensajes serializados desde un Worker 
    y despacharlos a los handlers locales usando buses en memoria.
    
    Uso (ej. Procrastinate):
    
        consumer = CQRSConsumer(local_cmd_bus, local_evt_bus, serializer)
        
        @app.task(name="process_cqrs_command")
        async def process_cqrs_command(payload: dict):
            await consumer.process_command(payload)
    """

    def __init__(
        self,
        command_bus: AbstractCommandBus,
        event_bus: AbstractEventBus,
        serializer: ISerializer,
    ) -> None:
        """
        Args:
            command_bus: El bus de comandos *local* (usualmente InMemoryCommandBus)
                         que tiene registrados los handlers.
            event_bus: El bus de eventos *local* (usualmente InMemoryEventBus).
            serializer: El serializador usado (ej. PydanticSerializer).
        """
        self._command_bus = command_bus
        self._event_bus = event_bus
        self._serializer = serializer

    async def process_command(self, payload: dict[str, t.Any]) -> None:
        """
        Deserializa un payload de comando y lo despacha al bus local.
        """
        try:
            command = self._serializer.deserialize(payload)
            cmd = t.cast("Command", command)
            
            logger.info("[CQRSConsumer] Procesando comando en background: %s", type(cmd).__qualname__)
            await self._command_bus.dispatch(cmd)
            
        except Exception as exc:
            logger.exception("[CQRSConsumer] Error procesando comando: %s", exc)
            raise

    async def process_event(self, payload: dict[str, t.Any]) -> None:
        """
        Deserializa un payload de evento y lo publica en el bus local
        para que todos los handlers (suscriptores) locales lo consuman.
        """
        try:
            event = self._serializer.deserialize(payload)
            evt = t.cast(DomainEvent, event)
            
            logger.info("[CQRSConsumer] Procesando evento en background: %s", evt.event_name)
            await self._event_bus.publish(evt)
            
        except Exception as exc:
            logger.exception("[CQRSConsumer] Error procesando evento: %s", exc)
            raise

    async def process_handler(self, handler_name: str, payload: dict[str, t.Any]) -> None:
        """
        Deserializa el evento y ejecuta *específicamente* un handler decorado con `@background_handler`.
        Esto es inyectado por el Smart Routing del EventBus.
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
        """
        try:
            logger.info("[CQRSConsumer] Ejecutando Task genérica en background: %s", task_name)
            
            task_func = _resolve_callable(task_name)
            # El payload en tareas genéricas son los kwargs
            await task_func(**payload)
            
        except Exception as exc:
            logger.exception("[CQRSConsumer] Error ejecutando Task %s: %s", task_name, exc)
            raise
