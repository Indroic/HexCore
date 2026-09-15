from __future__ import annotations
import typing as t
from datetime import datetime
from uuid import UUID
# Beanie exporta estos cuatro sin tipo, así que el import entero se reporta como
# `Unknown`. Es deuda de la librería y no hay nada que tipar de este lado.
from beanie import Document, Indexed, after_event, Save  # pyright: ignore[reportUnknownVariableType]


class BaseDocument(Document):
    entity_id: t.Annotated[UUID, Indexed(unique=True)]
    created_at: t.Optional[datetime] = datetime.now()
    updated_at: t.Optional[datetime] = datetime.now()
    is_active: t.Optional[bool] = True

    class Settings:
        is_root = True
        use_cache = True

    @after_event([Save])
    def update_updated_at(self):
        self.updated_at = datetime.now()
