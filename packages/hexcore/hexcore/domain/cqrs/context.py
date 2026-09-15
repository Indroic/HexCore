"""
Contexto de ejecución de mensajes CQRS.

Los buses con Smart Routing necesitan saber si el mensaje que están despachando
*viene* de una cola de tareas o si lo está originando el proceso actual. Sin esa
distinción, un worker que recibe un ``@background_command`` lo vuelve a encolar
en lugar de ejecutarlo, y el comando nunca corre (bucle infinito silencioso).

Semántica del flag:

- ``IN_WORKER`` sólo está activo alrededor de la **resolución** del mensaje entrante.
- El primer bus que lo lee lo **consume**: lo pone en ``False`` antes de invocar
  middlewares y handler. Así, un handler que despacha a propósito otro
  ``@background_command`` sigue encolándolo, que es la semántica esperada.
"""
from __future__ import annotations

import typing as t
from contextlib import contextmanager
from contextvars import ContextVar

__all__ = ["IN_WORKER", "worker_execution", "local_execution", "is_worker_execution"]


IN_WORKER: ContextVar[bool] = ContextVar("hexcore_in_worker", default=False)


def is_worker_execution() -> bool:
    """Indica si el mensaje en curso proviene de un worker de background."""
    return IN_WORKER.get()


@contextmanager
def worker_execution() -> t.Iterator[None]:
    """
    Marca el bloque como "ejecución dentro de un worker".

    Lo usan los consumidores (``CQRSConsumer``) alrededor del despacho del mensaje
    que acaban de sacar de la cola.
    """
    token = IN_WORKER.set(True)
    try:
        yield
    finally:
        IN_WORKER.reset(token)


@contextmanager
def local_execution() -> t.Iterator[None]:
    """
    Consume el flag de worker: dentro de este bloque ``IN_WORKER`` es ``False``.

    Lo usan los buses antes de entrar al pipeline/handler, para que el despacho
    explícito de otro mensaje de background desde dentro del handler se encole
    en vez de ejecutarse inline.
    """
    token = IN_WORKER.set(False)
    try:
        yield
    finally:
        IN_WORKER.reset(token)
