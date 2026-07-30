"""
Registro central de handlers CQRS.
Mapea tipos de Command/Query a sus handlers correspondientes.
"""
from __future__ import annotations

import threading
import typing as t

from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.queries import Query
from hexcore.domain.cqrs.handlers import AbstractCommandHandler, AbstractQueryHandler
from hexcore.domain.cqrs.exceptions import HandlerNotFoundError, DuplicateHandlerError


# Tipos para factories de handlers (para DI/lazy instantiation)
CommandHandlerFactory = t.Callable[[], AbstractCommandHandler[t.Any, t.Any]]
QueryHandlerFactory = t.Callable[[], AbstractQueryHandler[t.Any, t.Any]]

TFactory = t.TypeVar("TFactory")


class HandlerFactory(t.Generic[TFactory]):
    """
    Marcador explícito de "esto es un factory, no un handler".

    `callable(entry) and not isinstance(entry, AbstractCommandHandler)` es ambiguo: un handler
    que implemente `__call__` se confundiría con un factory, y un factory que herede de
    la interfaz se confundiría con un handler. Envolver el callable elimina la
    heurística.

    No es obligatorio: registrar un `lambda` sigue funcionando (se detecta por
    heurística, igual que antes) y `HandlerRegistry.factory()` construye el marcador
    por ti.
    """

    __slots__ = ("build",)

    def __init__(self, build: t.Callable[[], TFactory]) -> None:
        self.build = build

    def __call__(self) -> TFactory:
        return self.build()


class HandlerRegistry:
    """
    Registro de handlers para Commands y Queries.

    Soporta dos modos de registro:

    1. Registro directo de instancias (eager)
    2. Registro de factories (lazy, para DI containers)

    **Thread-safety.** El registro y la resolución están protegidos por un
    `threading.RLock`. Hace falta porque `resolve_*` hace lazy-init con escritura en el
    dict: sin lock, dos hilos (o dos hilos reales bajo el free-threading de Python
    3.14) pueden instanciar el mismo handler dos veces y quedarse cada uno con la suya.

    Uso::

        registry = HandlerRegistry()

        # Registro directo
        registry.register_command_handler(CreateUserCommand, CreateUserHandler())

        # Registro con factory (lazy) — marcador explícito
        registry.register_command_handler(
            CreateUserCommand, HandlerRegistry.factory(lambda: container.get(CreateUserHandler))
        )

        # Resolución
        handler = registry.resolve_command_handler(CreateUserCommand)
    """

    def __init__(self, *, allow_override: bool = False) -> None:
        self._command_handlers: dict[
            type[Command], AbstractCommandHandler[t.Any, t.Any] | CommandHandlerFactory
        ] = {}
        self._query_handlers: dict[
            type[Query[t.Any]], AbstractQueryHandler[t.Any, t.Any] | QueryHandlerFactory
        ] = {}
        self._allow_override = allow_override
        # Reentrante: un factory puede resolver otro handler del mismo registry.
        self._lock = threading.RLock()

    # ── Factories ─────────────────────────────────────────────────────────────

    @staticmethod
    def factory(build: t.Callable[[], t.Any]) -> HandlerFactory[t.Any]:
        """
        Envuelve un callable para registrarlo como factory sin ambigüedad.

        Preferí esto a pasar el callable pelado cuando tu handler implementa
        `__call__`, porque entonces la heurística no puede distinguirlos.
        """
        return HandlerFactory(build)

    # ── Command Handlers ──────────────────────────────────────────

    def register_command_handler(
        self,
        command_type: type[Command],
        handler: AbstractCommandHandler[t.Any, t.Any] | CommandHandlerFactory,
    ) -> "HandlerRegistry":
        """Registra un handler (o factory) para un tipo de command. Retorna self para fluent API."""
        with self._lock:
            if not self._allow_override and command_type in self._command_handlers:
                raise DuplicateHandlerError(command_type)
            self._command_handlers[command_type] = handler
        return self

    def resolve_command_handler(
        self, command_type: type[Command]
    ) -> AbstractCommandHandler[t.Any, t.Any]:
        """Resuelve el handler para el tipo de command dado."""
        with self._lock:
            entry = self._command_handlers.get(command_type)
            if entry is None:
                raise HandlerNotFoundError(command_type)
            if _is_factory(entry, AbstractCommandHandler):
                # Es un factory, invocar y cachear la instancia
                handler = t.cast(CommandHandlerFactory, entry)()
                self._command_handlers[command_type] = handler
                return handler
            return t.cast(AbstractCommandHandler[t.Any, t.Any], entry)

    # ── Query Handlers ────────────────────────────────────────────

    def register_query_handler(
        self,
        query_type: type[Query[t.Any]],
        handler: AbstractQueryHandler[t.Any, t.Any] | QueryHandlerFactory,
    ) -> "HandlerRegistry":
        """Registra un handler (o factory) para un tipo de query. Retorna self para fluent API."""
        with self._lock:
            if not self._allow_override and query_type in self._query_handlers:
                raise DuplicateHandlerError(query_type)
            self._query_handlers[query_type] = handler
        return self

    def resolve_query_handler(
        self, query_type: type[Query[t.Any]]
    ) -> AbstractQueryHandler[t.Any, t.Any]:
        """Resuelve el handler para el tipo de query dado."""
        with self._lock:
            entry = self._query_handlers.get(query_type)
            if entry is None:
                raise HandlerNotFoundError(query_type)
            if _is_factory(entry, AbstractQueryHandler):
                handler = t.cast(QueryHandlerFactory, entry)()
                self._query_handlers[query_type] = handler
                return handler
            return t.cast(AbstractQueryHandler[t.Any, t.Any], entry)

    # ── Introspección ─────────────────────────────────────────────

    @property
    def registered_commands(self) -> frozenset[type[Command]]:
        """Retorna los tipos de command registrados."""
        with self._lock:
            return frozenset(self._command_handlers.keys())

    @property
    def registered_queries(self) -> frozenset[type[Query[t.Any]]]:
        """Retorna los tipos de query registrados."""
        with self._lock:
            return frozenset(self._query_handlers.keys())


def _is_factory(entry: t.Any, handler_interface: type) -> bool:
    """
    Decide si `entry` es un factory de handlers o el handler mismo.

    El marcador `HandlerFactory` es inequívoco. Para los callables pelados se mantiene
    la heurística anterior por retrocompatibilidad, con una comprobación extra: un
    objeto con método `handle` es un handler aunque además sea callable.
    """
    if isinstance(entry, HandlerFactory):
        return True
    if isinstance(entry, handler_interface):
        return False
    if hasattr(entry, "handle"):
        return False
    return callable(entry)
