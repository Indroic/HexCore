from __future__ import annotations
import typing as t
from datetime import datetime
from uuid import UUID
from beanie import Document, Indexed, after_event, Save  # type: ignore


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
