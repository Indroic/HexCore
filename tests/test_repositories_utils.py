import abc
import asyncio
import warnings
from collections.abc import Iterator, Mapping
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import create_engine, text

from hexcore.domain.base import BaseEntity
from hexcore.infrastructure.repositories.base import (
    BaseBeanieRepository,
    BaseSQLAlchemyRepository,
)
from hexcore.infrastructure.repositories.utils import (
    clear_discovery_cache,
    discover_nosql_repositories,
    discover_sql_repositories,
    to_entity_from_model_or_document,
)
from hexcore.infrastructure.repositories import utils as repo_utils
from hexcore.types import FieldResolversType


class _TestEntity(BaseEntity):
    name: str | None = None
    score: int | None = None


class _DummyModel:
    name: str

    def __init__(self, **values: object) -> None:
        self.__dict__.update(values)


class _RowLikeWithoutDict:
    def __init__(self, **values: object) -> None:
        self._mapping = values

    def __getattribute__(self, name: str):
        if name == "__dict__":
            raise AttributeError("Could not locate column in row for column '__dict__'")
        return super().__getattribute__(name)


class _MappingWithoutDict(Mapping[str, object]):
    __slots__ = ("_data",)

    def __init__(self, **values: object) -> None:
        self._data = values

    def __getitem__(self, key: str) -> object:
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


class _SqlRepoA(BaseSQLAlchemyRepository[_TestEntity]):
    async def get_by_id(self, entity_id):
        raise NotImplementedError

    async def list_all(self, limit=None, offset=0):
        raise NotImplementedError

    async def save(self, entity):
        raise NotImplementedError

    async def delete(self, entity):
        raise NotImplementedError


class _SqlRepoB(BaseSQLAlchemyRepository[_TestEntity]):
    async def get_by_id(self, entity_id):
        raise NotImplementedError

    async def list_all(self, limit=None, offset=0):
        raise NotImplementedError

    async def save(self, entity):
        raise NotImplementedError

    async def delete(self, entity):
        raise NotImplementedError


class LiveLinesRepo(BaseBeanieRepository[_TestEntity]):
    async def get_by_id(self, entity_id):
        raise NotImplementedError

    async def list_all(self, limit=None, offset=0):
        raise NotImplementedError

    async def save(self, entity):
        raise NotImplementedError

    async def delete(self, entity):
        raise NotImplementedError


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

        field_resolvers: FieldResolversType[_DummyModel] = {
            "score": ("name", score_resolver)
        }

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


def test_to_entity_from_mapping_without_dict() -> None:
    async def _run() -> None:
        mapping_like = _MappingWithoutDict(name="mapping-like")

        entity = await to_entity_from_model_or_document(mapping_like, _TestEntity)

        assert entity.name == "mapping-like"

    asyncio.run(_run())


def test_discover_sql_repositories_detects_key_collisions() -> None:
    RepoX = type("UsersRepository", (_SqlRepoA,), {})
    RepoY = type("UsersRepo", (_SqlRepoB,), {})

    with (
        patch("hexcore.infrastructure.repositories.utils._autoload_repository_modules"),
        patch(
            "hexcore.infrastructure.repositories.utils._get_all_subclasses",
            return_value={RepoX, RepoY},
        ),
    ):
        raised = None
        try:
            discover_sql_repositories()
        except ValueError as exc:
            raised = exc

    assert raised is not None
    assert "colision" in str(raised)
    assert "users" in str(raised)


def test_discover_nosql_repositories_accepts_repo_suffix() -> None:
    with (
        patch("hexcore.infrastructure.repositories.utils._autoload_repository_modules"),
        patch(
            "hexcore.infrastructure.repositories.utils._get_all_subclasses",
            return_value={LiveLinesRepo},
        ),
    ):
        discovered = discover_nosql_repositories()

    assert "livelines" in discovered
    assert discovered["livelines"] is LiveLinesRepo


def test_discover_sql_repositories_warns_for_abstract_repositories() -> None:
    class _AbstractWarningRepo(_SqlRepoA):
        @abc.abstractmethod
        async def custom_required(self):
            raise NotImplementedError

    class _ConcreteWarningRepo(_SqlRepoA):
        pass

    with (
        patch("hexcore.infrastructure.repositories.utils._autoload_repository_modules"),
        patch(
            "hexcore.infrastructure.repositories.utils._get_all_subclasses",
            return_value={_AbstractWarningRepo, _ConcreteWarningRepo},
        ),
        warnings.catch_warnings(record=True) as caught,
    ):
        warnings.simplefilter("always")
        discovered = discover_sql_repositories()

    assert "concretewarning" in discovered
    assert discovered["concretewarning"] is _ConcreteWarningRepo
    assert caught
    warning_message = str(caught[0].message)
    assert "_AbstractWarningRepo" in warning_message
    assert "custom_required" in warning_message


def test_discover_sql_repositories_ignores_alias_duplicates() -> None:
    RepoBase = type("AccountingSnapshotRepository", (_SqlRepoA,), {})
    RepoAlias = type("AccountingSnapshotRepository", (_SqlRepoA,), {})

    RepoBase.__module__ = "infrastructure.repositories.accounting_snapshot_repository"
    RepoAlias.__module__ = (
        "src.infrastructure.repositories.accounting_snapshot_repository"
    )
    RepoBase.__qualname__ = "AccountingSnapshotRepository"
    RepoAlias.__qualname__ = "AccountingSnapshotRepository"

    with (
        patch("hexcore.infrastructure.repositories.utils._autoload_repository_modules"),
        patch(
            "hexcore.infrastructure.repositories.utils._get_all_subclasses",
            return_value={RepoBase, RepoAlias},
        ),
        patch(
            "hexcore.infrastructure.repositories.utils._get_repository_class_source_path",
            return_value="c:/repo/src/infrastructure/repositories/accounting_snapshot_repository.py",
        ),
        warnings.catch_warnings(record=True) as caught,
    ):
        warnings.simplefilter("always")
        discovered = discover_sql_repositories()

    assert "accountingsnapshot" in discovered
    assert discovered["accountingsnapshot"] in {RepoBase, RepoAlias}
    assert any("alias de import" in str(item.message) for item in caught)


def test_discover_sql_repositories_raises_on_duplicate_key_without_priority() -> None:
    InfraRepo = type("PaymentsRepository", (_SqlRepoA,), {})
    SrcRepo = type("PaymentsRepository", (_SqlRepoB,), {})

    InfraRepo.__module__ = "infrastructure.repositories.payments_repository"
    SrcRepo.__module__ = "src.infrastructure.repositories.payments_repository"

    with (
        patch("hexcore.infrastructure.repositories.utils._autoload_repository_modules"),
        patch(
            "hexcore.infrastructure.repositories.utils._get_all_subclasses",
            return_value={InfraRepo, SrcRepo},
        ),
    ):
        raised = None
        try:
            discover_sql_repositories()
        except ValueError as exc:
            raised = exc

    assert raised is not None
    assert "colision" in str(raised)
    assert "payments" in str(raised)


def test_iter_candidate_repository_packages_uses_configured_paths_first() -> None:
    with patch(
        "hexcore.infrastructure.repositories.utils._get_configured_repository_paths",
        return_value={"myapp.slices.billing.repositories"},
    ):
        packages = repo_utils._iter_candidate_repository_packages()

    assert packages == {"myapp.slices.billing.repositories"}


def test_iter_candidate_repository_packages_without_config_returns_empty_set() -> None:
    with patch(
        "hexcore.infrastructure.repositories.utils._get_configured_repository_paths",
        return_value=set(),
    ):
        packages = repo_utils._iter_candidate_repository_packages()

    assert packages == set()


def test_clear_discovery_cache_empties_autoloaded_packages() -> None:
    repo_utils._AUTOLOADED_REPOSITORY_PACKAGES.add("dummy.repositories")

    clear_discovery_cache()

    assert repo_utils._AUTOLOADED_REPOSITORY_PACKAGES == set()
