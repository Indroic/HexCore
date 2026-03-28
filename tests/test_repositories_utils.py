import asyncio
from uuid import uuid4

from sqlalchemy import create_engine, text

from hexcore.domain.base import BaseEntity
from hexcore.infrastructure.repositories.utils import to_entity_from_model_or_document


class _TestEntity(BaseEntity):
    name: str | None = None
    score: int | None = None


class _DummyModel:
    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class _RowLikeWithoutDict:
    def __init__(self, **values: object) -> None:
        self._mapping = values

    def __getattribute__(self, name: str):
        if name == "__dict__":
            raise AttributeError("Could not locate column in row for column '__dict__'")
        return super().__getattribute__(name)


def test_to_entity_from_plain_model_uses_model_dict_values() -> None:
    async def _run() -> None:
        entity_id = uuid4()
        model = _DummyModel(id=entity_id, name="sql-model")

        entity = await to_entity_from_model_or_document(model, _TestEntity)

        assert entity.id == entity_id
        assert entity.name == "sql-model"

    asyncio.run(_run())


def test_to_entity_from_nosql_remaps_entity_id_to_id() -> None:
    async def _run() -> None:
        entity_id = uuid4()
        model = _DummyModel(entity_id=entity_id, name="nosql-doc")

        entity = await to_entity_from_model_or_document(
            model, _TestEntity, is_nosql=True
        )

        assert entity.id == entity_id
        assert entity.name == "nosql-doc"

    asyncio.run(_run())


def test_to_entity_applies_async_field_resolvers() -> None:
    async def _run() -> None:
        model = _DummyModel(name="resolver-target")

        async def score_resolver(model_instance: _DummyModel) -> int:
            return len(model_instance.name)

        field_resolvers = {"score": ("name", score_resolver)}

        entity = await to_entity_from_model_or_document(
            model, _TestEntity, field_resolvers=field_resolvers
        )

        assert entity.score == len("resolver-target")

    asyncio.run(_run())


def test_to_entity_from_sqlalchemy_row_mapping() -> None:
    async def _run() -> None:
        engine = create_engine("sqlite+pysqlite:///:memory:")
        with engine.connect() as connection:
            row = connection.execute(text("SELECT 'row-value' AS name")).first()

        assert row is not None

        entity = await to_entity_from_model_or_document(row, _TestEntity)

        assert entity.name == "row-value"

    asyncio.run(_run())


def test_to_entity_from_row_like_object_uses_mapping() -> None:
    async def _run() -> None:
        row_like = _RowLikeWithoutDict(name="row-like")

        entity = await to_entity_from_model_or_document(row_like, _TestEntity)

        assert entity.name == "row-like"

    asyncio.run(_run())
