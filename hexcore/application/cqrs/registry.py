"""
Registro central de handlers CQRS.
Mapea tipos de Command/Query a sus handlers correspondientes.
"""
from __future__ import annotations

import typing as t

from hexcore.domain.cqrs.commands import Command
from hexcore.domain.cqrs.queries import Query
from hexcore.domain.cqrs.handlers import ICommandHandler, IQueryHandler
from hexcore.domain.cqrs.exceptions import HandlerNotFoundError, DuplicateHandlerError


# Tipos para factories de handlers (para DI/lazy instantiation)
CommandHandlerFactory = t.Callable[[], ICommandHandler[t.Any, t.Any]]
QueryHandlerFactory = t.Callable[[], IQueryHandler[t.Any, t.Any]]


class HandlerRegistry:
    """
    Registro thread-safe de handlers para Commands y Queries.

    Soporta dos modos de registro:

    1. Registro directo de instancias (eager)
    2. Registro de factories (lazy, para DI containers)

    Uso::

        registry = HandlerRegistry()

        # Registro directo
        registry.register_command_handler(CreateUserCommand, CreateUserHandler())

        # Registro con factory (lazy)
        registry.register_command_handler(CreateUserCommand, lambda: container.get(CreateUserHandler))

        # Resolución
        handler = registry.resolve_command_handler(CreateUserCommand)
    """

    def __init__(self, *, allow_override: bool = False) -> None:
        self._command_handlers: dict[
            type[Command], ICommandHandler[t.Any, t.Any] | CommandHandlerFactory
        ] = {}
        self._query_handlers: dict[
            type[Query[t.Any]], IQueryHandler[t.Any, t.Any] | QueryHandlerFactory
        ] = {}
        self._allow_override = allow_override

    # ── Command Handlers ──────────────────────────────────────────

    def register_command_handler(
        self,
        command_type: type[Command],
        handler: ICommandHandler[t.Any, t.Any] | CommandHandlerFactory,
    ) -> "HandlerRegistry":
        """Registra un handler (o factory) para un tipo de command. Retorna self para fluent API."""
        if not self._allow_override and command_type in self._command_handlers:
            raise DuplicateHandlerError(command_type)
        self._command_handlers[command_type] = handler
        return self

    def resolve_command_handler(
        self, command_type: type[Command]
    ) -> ICommandHandler[t.Any, t.Any]:
        """Resuelve el handler para el tipo de command dado."""
        entry = self._command_handlers.get(command_type)
        if entry is None:
            raise HandlerNotFoundError(command_type)
        if callable(entry) and not isinstance(entry, ICommandHandler):
            # Es un factory, invocar y cachear la instancia
            handler = entry()
            self._command_handlers[command_type] = handler
            return handler
        return t.cast(ICommandHandler[t.Any, t.Any], entry)

    # ── Query Handlers ────────────────────────────────────────────

    def register_query_handler(
        self,
        query_type: type[Query[t.Any]],
        handler: IQueryHandler[t.Any, t.Any] | QueryHandlerFactory,
    ) -> "HandlerRegistry":
        """Registra un handler (o factory) para un tipo de query. Retorna self para fluent API."""
        if not self._allow_override and query_type in self._query_handlers:
            raise DuplicateHandlerError(query_type)
        self._query_handlers[query_type] = handler
        return self

    def resolve_query_handler(
        self, query_type: type[Query[t.Any]]
    ) -> IQueryHandler[t.Any, t.Any]:
        """Resuelve el handler para el tipo de query dado."""
        entry = self._query_handlers.get(query_type)
        if entry is None:
            raise HandlerNotFoundError(query_type)
        if callable(entry) and not isinstance(entry, IQueryHandler):
            handler = entry()
            self._query_handlers[query_type] = handler
            return handler
        return t.cast(IQueryHandler[t.Any, t.Any], entry)

    # ── Introspección ─────────────────────────────────────────────

    @property
    def registered_commands(self) -> frozenset[type[Command]]:
        """Retorna los tipos de command registrados."""
        return frozenset(self._command_handlers.keys())

    @property
    def registered_queries(self) -> frozenset[type[Query[t.Any]]]:
        """Retorna los tipos de query registrados."""
        return frozenset(self._query_handlers.keys())
