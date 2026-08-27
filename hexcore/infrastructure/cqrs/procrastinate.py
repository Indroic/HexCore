"""
Adaptador de Procrastinate como CommandBus asíncrono.
Procrastinate es una librería de task queue sobre PostgreSQL.

IMPORTANTE: Este módulo es un adaptador OPCIONAL.
Solo se importa si 'procrastinate' está instalado.
La dependencia se valida en tiempo de instanciación, no en import-time.
"""
from __future__ import annotations

import typing as t

from hexcore.domain.cqrs.buses import AbstractCommandBus
from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.envelope import restored_envelope_scope
from hexcore.domain.cqrs.serializer import AbstractSerializer
from hexcore.application.cqrs.pipeline import MiddlewarePipeline
from hexcore.application.cqrs.registry import HandlerRegistry


def _ensure_procrastinate() -> None:
    """
    Valida que Procrastinate esté instalado.

    Delega en `require_extra`, que es el mismo error para los ocho extras. Este módulo era
    el único que se acordaba de decir qué había que instalar, y esa asimetría era el
    problema: el consumidor recibía un mensaje distinto según por dónde entrara.

    De paso desaparece el `import procrastinate` que sólo existía para probar que se podía
    importar — con su `noqa` al lado, porque el import quedaba sin usar.
    """
    from hexcore.capabilities import require_extra

    require_extra("procrastinate", para="`ProcrastinateCommandBus`")


class ProcrastinateCommandBus(AbstractCommandBus):
    """
    Bus de commands que encola la ejecución en Procrastinate (PostgreSQL task queue).

    Flujo:

    1. ``dispatch(command)`` serializa el command y lo encola como tarea de Procrastinate.
    2. Un worker de Procrastinate deserializa el command y ejecuta el handler
       correspondiente (con su pipeline de middlewares).

    Configuración::

        from hexcore.infrastructure.cqrs.procrastinate import ProcrastinateCommandBus

        bus = ProcrastinateCommandBus(
            app=procrastinate_app,          # Instancia de procrastinate.App
            registry=handler_registry,       # Registry con handlers registrados
            serializer=PydanticSerializer(), # Serializer para commands
        )
    """

    def __init__(
        self,
        app: t.Any,  # procrastinate.App (no tipado para evitar import-time dependency)
        registry: HandlerRegistry,
        serializer: AbstractSerializer,
        pipeline: MiddlewarePipeline | None = None,
        *,
        queue_name: str = "hexcore_commands",
    ) -> None:
        _ensure_procrastinate()
        self._app = app
        self._registry = registry
        self._serializer = serializer
        self._pipeline = pipeline or MiddlewarePipeline()
        self._queue_name = queue_name
        self._task: t.Any = None
        self._register_task()

    def _register_task(self) -> None:
        """Registra la tarea genérica de procesamiento de commands en Procrastinate."""

        @self._app.task(
            name="hexcore.cqrs.process_command",
            queue=self._queue_name,
        )
        async def process_command(payload: dict[str, t.Any]) -> t.Any:
            """Worker-side: deserializa, restaura el contexto ambiental y ejecuta."""
            command, metadata = self._serializer.deserialize_envelope(payload)
            handler = self._registry.resolve_command_handler(type(command))

            async def final_handler(cmd: t.Any) -> t.Any:
                return await handler.handle(cmd)

            # El scope envuelve al pipeline entero y no sólo al handler: un middleware que
            # audita o que autoriza necesita el mismo contexto que el handler, y dejarlo
            # afuera haría que el middleware viera "sin autenticar" y el handler no.
            async with restored_envelope_scope(metadata, command):
                return await self._pipeline.execute(command, final_handler)

        self._task = process_command

    async def dispatch(self, command: Command) -> t.Any:
        """
        Serializa el command y lo encola en Procrastinate.

        Returns:
            El job_id del task encolado (no el resultado del handler).
            El resultado real se obtiene del worker.
        """
        payload = self._serializer.serialize_envelope(command)
        job = await self._task.defer_async(payload=payload)
        return job
