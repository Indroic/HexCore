"""
A command, its handler, and the wiring that reaches an endpoint.

Note that the handler takes the Unit of Work directly. That is idiomatic in HexCore's own
examples -- the domain-service indirection belongs to the `UseCase` path, not to CQRS
handlers.
"""
from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends

import hexcore.cqrs as cqrs
import hexcore.fastapi as hx
from hexcore.domain.cqrs import AbstractCommandHandler


# ---- The message -------------------------------------------------------------
#
# MODULE LEVEL, always. A Command defined inside a function carries `<locals>` in its
# __qualname__, and the worker resolves messages by fully qualified name -- it could never
# import it. All three background decorators reject this at decoration time, which is
# strictly better than what happened before 5.0: the message enqueued fine and failed in the
# worker, where it cannot be recovered.
#
# Commands are frozen: a message in flight is never mutated, which is what lets a middleware
# replace it without anybody observing an intermediate state.


class CreateTicket(cqrs.Command):
    title: str
    customer_id: UUID


# To run it in the background instead, decorate the class and give the factory an enqueuer:
#
#     @cqrs.background_command(queue="high_priority")
#     class CreateTicket(cqrs.Command):
#         ...
#
# There is no second "dispatch async" API: the same bus enqueues in the web process and
# executes inside the worker. That is Smart Routing, and it is why you share one bus.


# ---- The handler -------------------------------------------------------------


class CreateTicketHandler(AbstractCommandHandler[CreateTicket, UUID]):
    """
    Inheriting from the abstract class is not mandatory -- having `handle` is enough -- but
    it is what gives the type checker the type of the dispatch result.
    """

    def __init__(self, uow) -> None:
        self.uow = uow

    async def handle(self, command: CreateTicket) -> UUID:
        # The transaction belongs to the handler. Do not also add TransactionMiddleware:
        # it commits after the handler, so this would commit twice.
        async with self.uow:
            ticket = Ticket(title=command.title)
            ticket.register_event(TicketCreated(entity_id=ticket.id, entity_data=ticket))
            await self.uow.tickets.save(ticket)
            await self.uow.commit()   # collects the events, commits, then publishes them
        return ticket.id


# ---- Registration ------------------------------------------------------------

registry = cqrs.HandlerRegistry()

# A handler needing a fresh UoW per message cannot be registered as an instance.
# `.factory()` is an explicit marker: without it, a handler implementing __call__ would be
# indistinguishable from a factory and the registry would have to guess.
registry.register_command_handler(
    CreateTicket,
    cqrs.HandlerRegistry.factory(lambda: CreateTicketHandler(build_uow())),
)

# The method is register_command_handler, not register_command.
#
# The default raises DuplicateHandlerError on a second registration for the same type,
# because in production that is almost always a module imported twice. Pass
# HandlerRegistry(allow_override=True) when you mean it.


# ---- Wiring ------------------------------------------------------------------

# Once, at startup. `configure_cqrs` builds all three buses consistently and leaves the
# container reachable from the dependencies. It FAILS AT CONSTRUCTION if the registry holds
# @background_commands and no enqueuer was given -- rather than building a bus that raises
# RuntimeError on the first dispatch, with a user's request already in flight.
container = hx.configure_cqrs(registry, enqueuer=enqueuer)

router = APIRouter(prefix="/tickets", tags=["tickets"])


@router.post("")
async def create(cmd: CreateTicket, bus=Depends(hx.provide_command_bus)):
    # `provide_command_bus` exists as a FUNCTION for exactly one reason: so tests can
    # replace it through app.dependency_overrides. See override_cqrs in conftest.py.
    return {"id": await bus.dispatch(cmd)}
