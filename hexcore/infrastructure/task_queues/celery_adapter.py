"""
Adaptador para Celery.
Permite encolar comandos y tareas usando Celery, y proporciona 
utilidades para auto-registrar las tareas asíncronas en el Worker.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import typing as t
import weakref

from hexcore.domain.cqrs.task_queues import ITaskEnqueuer

logger = logging.getLogger("hexcore.task_queues.celery")


class _PersistentLoop:
    """
    Un event loop por proceso worker, en un hilo dedicado.

    `asyncio.run()` por tarea crea y **cierra** un loop nuevo cada vez. Con un
    `AsyncEngine` de SQLAlchemy compartido eso produce `Event loop is closed` y
    `Future attached to a different loop`: el pool guarda conexiones atadas al loop de la
    tarea anterior, que ya no existe.

    Detalle imprescindible con el pool *prefork* de Celery: el loop se crea de forma
    perezosa y se comprueba el PID en cada uso. Un loop y un hilo creados antes del fork
    no son utilizables en el hijo, así que al detectar un PID distinto se crean de nuevo.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._pid: int | None = None
        self._lock = threading.RLock()

    def run(self, coro: t.Coroutine[t.Any, t.Any, t.Any]) -> t.Any:
        """Ejecuta la corutina en el loop del proceso y espera su resultado."""
        loop = self._ensure_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    def _ensure_loop(self) -> asyncio.AbstractEventLoop:
        with self._lock:
            current_pid = os.getpid()
            if (
                self._loop is not None
                and self._pid == current_pid
                and self._thread is not None
                and self._thread.is_alive()
            ):
                return self._loop

            if self._loop is not None and self._pid != current_pid:
                logger.debug(
                    "Fork detectado (pid %s → %s): se crea un event loop nuevo.",
                    self._pid,
                    current_pid,
                )

            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                name="hexcore-celery-loop",
                daemon=True,
            )
            thread.start()
            self._loop, self._thread, self._pid = loop, thread, current_pid
            return loop

    def shutdown(self, timeout: float = 5.0) -> None:
        """Para el loop y su hilo. Idempotente."""
        with self._lock:
            loop, thread = self._loop, self._thread
            self._loop = self._thread = self._pid = None

        if loop is None:
            return
        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=timeout)
        loop.close()


_LOOP = _PersistentLoop()


def run_in_worker_loop(coro: t.Coroutine[t.Any, t.Any, t.Any]) -> t.Any:
    """
    Ejecuta una corutina en el event loop persistente del proceso worker.

    Útil para tareas de Celery propias que también toquen el `AsyncEngine`: si usás
    `asyncio.run()` en ellas, vuelve el problema que este módulo resuelve.
    """
    return _LOOP.run(coro)


def shutdown_worker_loop(timeout: float = 5.0) -> None:
    """
    Para el event loop persistente.

    Llamalo desde la señal `worker_shutdown` de Celery si querés un apagado limpio; no es
    obligatorio, porque el hilo es daemon.
    """
    _LOOP.shutdown(timeout)

if t.TYPE_CHECKING:
    # Sin el `try/except ImportError: Celery = t.Any` que estaba acá. Ver el comentario
    # equivalente en `procrastinate_adapter.py`: bajo `TYPE_CHECKING` el import nunca corre,
    # así que el respaldo no evitaba ningún error de runtime — sólo hacía que Pyright
    # resolviera `Celery` a `Any` y degradara las firmas con el extra instalado.
    from celery import Celery

    from hexcore.infrastructure.workers.consumer import CQRSConsumer


class _CeleryApp(t.Protocol):
    """
    Lo único que HexCore le pide a una app de Celery: mandar tareas y registrarlas.

    Celery viene con anotaciones parciales — `send_task` y `task` tienen los parámetros sin
    tipo, así que cada llamada nuestra se reportaba como `Unknown` aunque el extra estuviera
    instalado, y `@app.task` como un decorador que "oscurece el tipo de la función". No es
    algo que se arregle de nuestro lado del import: los tipos que faltan son los de ellos.

    Lo que sí podemos hacer es declarar **qué forma usamos**, que es esto. El protocolo es
    estructural, así que una `Celery` de verdad lo satisface sin registrarse en ningún lado,
    y si mañana cambian la firma de `send_task` el `cast` deja de ser cierto en un solo lugar
    —acá— en vez de en cada llamada.

    Deliberadamente angosto: dos métodos, y sólo los argumentos que pasamos. Un protocolo que
    copiara la firma entera de Celery sería otra copia que mantener, y volvería a traer los
    `Unknown` que existe para sacar.
    """

    def send_task(
        self,
        name: str,
        *,
        kwargs: dict[str, t.Any] | None = ...,
        queue: str | None = ...,
    ) -> object: ...

    def task(
        self, **opciones: t.Any
    ) -> t.Callable[[t.Callable[..., t.Any]], t.Any]: ...


class CeleryEnqueuer(ITaskEnqueuer):
    """
    Adaptador que utiliza Celery para encolar comandos, eventos y tareas.
    Dado que `ITaskEnqueuer` tiene métodos asíncronos y Celery `.send_task` 
    es síncrono (bloqueante), utiliza `asyncio.to_thread` para no congelar
    el Event Loop principal (ej. FastAPI).
    """

    def __init__(self, app: "Celery") -> None:
        self.app = app
        # `self.app` queda como la app de Celery, que es lo que el consumidor pasó y lo que
        # espera encontrar. `_send` es la misma app vista por el protocolo, y es lo que usan
        # los tres `enqueue_*`: la vista angosta va adentro, la ancha queda en la API.
        self._send: "_CeleryApp" = t.cast("_CeleryApp", app)

    async def enqueue_command(self, command_name: str, payload: dict[str, t.Any], queue: str) -> None:
        await asyncio.to_thread(
            self._send.send_task,
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
            self._send.send_task,
            "hexcore.process_handler",
            kwargs={"handler_name": handler_name, "payload": payload},
            queue=queue,
        )

    async def enqueue_task(self, task_name: str, payload: dict[str, t.Any], queue: str) -> None:
        await asyncio.to_thread(
            self._send.send_task,
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

    Las tareas de Celery son síncronas y el Consumer de HexCore es asíncrono, así que se
    ejecutan en un **event loop persistente por proceso** (ver `_PersistentLoop`), no con
    `asyncio.run()` por tarea. Con `asyncio.run()` y un `AsyncEngine` compartido aparecen
    `Event loop is closed` y `attached to a different loop`.

    Limitación conocida: las tareas se ejecutan en un hilo distinto al que Celery usa para
    la tarea. Si tu código depende de estado thread-local puesto por Celery (algunos
    plugins de tracing lo hacen), pásalo explícitamente al handler.

    Returns:
        True si se registraron las tareas, False si ya estaban registradas.
    """
    registrable: "_CeleryApp" = t.cast("_CeleryApp", app)

    if not force and is_registered(app):
        logger.debug(
            "Las tareas de HexCore ya estaban registradas en esta app de Celery; "
            "no se vuelven a registrar."
        )
        return False

    # Los tres se registran por **efecto de lado** del decorador: `app.task(...)` las mete en
    # el registro de la app y el nombre local no lo lee nadie.
    @registrable.task(name="hexcore.process_command", bind=True)
    def _process_command(self: t.Any, payload: dict[str, t.Any]) -> None:
        _LOOP.run(consumer.process_command(payload))

    @registrable.task(name="hexcore.process_handler", bind=True)
    def _process_handler(self: t.Any, handler_name: str, payload: dict[str, t.Any]) -> None:
        _LOOP.run(consumer.process_handler(handler_name, payload))

    @registrable.task(name="hexcore.process_task", bind=True)
    def _process_task(self: t.Any, task_name: str, payload: dict[str, t.Any]) -> None:
        _LOOP.run(consumer.process_task(task_name, payload))

    # Nombrarlas acá es lo que las separa de código muerto, para el checker y para quien lee.
    # El prefijo con guion bajo no alcanza: `reportUnusedFunction` no exime los nombres
    # privados como sí hace `reportUnusedVariable`.
    _registradas = (_process_command, _process_handler, _process_task)

    try:
        _registered_apps[id(app)] = app
    except TypeError:
        logger.debug("No se pudo memorizar el registro para esta app de Celery.")
    return True
