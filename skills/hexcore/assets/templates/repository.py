"""
A SQL repository: entity, ORM model, repository.

The point of this template is the property count. `SqlAlchemyRepository` needs THREE
properties -- entity_cls, model_cls, not_found_exception -- and the other two are optional.
Declaring `fields_resolvers` and `fields_serializers` just to return None is noise that older
documentation taught as mandatory.
"""
from __future__ import annotations

from sqlalchemy.orm import Mapped, mapped_column

from hexcore.domain.base import BaseEntity
from hexcore.domain.events import EntityCreatedEvent
from hexcore.domain.repositories import IBaseRepository
from hexcore.infrastructure.repositories.implementations import SqlAlchemyRepository
from hexcore.sql import BaseModel
from hexcore.types import FieldResolversType, FieldSerializersType


# ---- Domain ------------------------------------------------------------------


class Ticket(BaseEntity):
    """
    `BaseEntity` already provides `id` (UUID), `created_at`, `updated_at` (both UTC) and
    `is_active`. Redeclaring any of them shadows the base field -- do not.
    """

    title: str
    closed: bool = False


class TicketCreated(EntityCreatedEvent[Ticket]):
    pass


class TicketNotFound(Exception):
    pass


class ITicketRepository(IBaseRepository[Ticket]):
    """
    Declare only the specialised queries. The base CRUD -- get_by_id, get_active_by_id,
    list_all, query_all, query_cursor, save, delete -- is inherited.
    """

    async def find_open_by_customer(self, customer_id: str) -> list[Ticket]: ...


# ---- Infrastructure ----------------------------------------------------------


class TicketModel(BaseModel["Ticket"]):
    """`BaseModel` ships id, is_active, created_at, updated_at, plus set_domain_entity() /
    get_domain_entity() -- what lets the repository return the entity without a second query.

    Never name a column `metadata`: it shadows Base.metadata.
    """

    __tablename__ = "tickets"

    title: Mapped[str] = mapped_column()
    closed: Mapped[bool] = mapped_column(default=False)


class TicketRepository(SqlAlchemyRepository[Ticket, TicketModel], ITicketRepository):
    # --- the three required properties ---

    @property
    def entity_cls(self) -> type[Ticket]:
        return Ticket

    @property
    def model_cls(self) -> type[TicketModel]:
        return TicketModel

    @property
    def not_found_exception(self) -> type[Exception]:
        return TicketNotFound

    # --- the two optional ones: declare them ONLY when you need them ---
    #
    # They are {"field": callable} maps, and they exist because the automatic conversion --
    # to_entity_from_model_or_document -- covers scalars and simple relations, not a value
    # object serialized to JSON or a list stored in a separate table. Apply
    # @cycle_protection_resolver to a resolver that walks a circular relation.
    #
    # @property
    # def fields_resolvers(self) -> FieldResolversType | None:
    #     return {"attachments": resolve_attachments}
    #
    # @property
    # def fields_serializers(self) -> FieldSerializersType | None:
    #     return {"price": lambda money: money.as_decimal()}

    # --- what you actually add: specialised queries ---

    async def find_open_by_customer(self, customer_id: str) -> list[Ticket]:
        ...


# Register this module in ServerConfig.repository_discovery_paths, or the Unit of Work will
# never instantiate it -- discovery is explicit and does not guess by folder convention.
#
# Usage, with the UoW exposing it by attribute name:
#
#     async with sql.uow_scope() as uow:
#         async with uow:
#             ticket = Ticket(title="Something broke")
#             ticket.register_event(TicketCreated(entity_id=ticket.id, entity_data=ticket))
#             await uow.tickets.save(ticket)
#             await uow.commit()        # collects the events, commits, then publishes
#
# `delete()` is a SOFT delete: it deactivates the row. `get_active_by_id()` is the read that
# ignores deactivated ones.
