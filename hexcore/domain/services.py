from __future__ import annotations

import datetime as dt
import typing as t

from hexcore.application.dtos.query import (
    FilterConditionDTO,
    FilterOperator,
    QueryRequestDTO,
    QueryResponseDTO,
    SortConditionDTO,
    SortDirection,
)
from hexcore.domain.base import BaseEntity
from hexcore.domain.events import IEventDispatcher
from hexcore.domain.repositories import IBaseRepository
from hexcore.config import LazyConfig


T = t.TypeVar("T", bound=BaseEntity)


class BaseDomainService:
    def __init__(
        self,
        event_dispatcher: IEventDispatcher = LazyConfig().get_config().event_dispatcher,
    ) -> None:
        self.config = LazyConfig.get_config()
        self.event_dispatcher = event_dispatcher

    async def list_entities(
        self,
        repository: IBaseRepository[T],
        query: QueryRequestDTO,
    ) -> QueryResponseDTO:
        query_all = getattr(repository, "query_all", None)
        if callable(query_all):
            entities, total = await t.cast(
                t.Callable[[QueryRequestDTO], t.Awaitable[tuple[list[T], int]]],
                query_all,
            )(query)
            has_next = query.offset + len(entities) < total
            return QueryResponseDTO(
                items=entities,
                total=total,
                limit=query.limit,
                offset=query.offset,
                has_next=has_next,
            )

        entities = await repository.list_all(limit=None, offset=0)
        return self.query_entities(entities, query)

    def query_entities(
        self,
        entities: t.Sequence[T],
        query: QueryRequestDTO,
    ) -> QueryResponseDTO:
        results: list[T] = list(entities)
        results = self._apply_search(results, query.search, query.search_fields)
        results = self._apply_filters(results, query.filters)
        results = self._apply_sort(results, query.sort)

        total = len(results)
        paginated = results[query.offset : query.offset + query.limit]
        has_next = query.offset + len(paginated) < total

        return QueryResponseDTO(
            items=paginated,
            total=total,
            limit=query.limit,
            offset=query.offset,
            has_next=has_next,
        )

    def _apply_search(
        self,
        entities: list[T],
        search_text: str | None,
        search_fields: list[str],
    ) -> list[T]:
        if not search_text:
            return entities

        normalized_query = search_text.lower().strip()
        if not normalized_query:
            return entities

        fields = search_fields or self._infer_search_fields(entities)
        if not fields:
            return entities

        matched: list[T] = []
        for entity in entities:
            for field_name in fields:
                value = getattr(entity, field_name, None)
                if value is None:
                    continue
                if normalized_query in str(value).lower():
                    matched.append(entity)
                    break
        return matched

    def _infer_search_fields(self, entities: list[T]) -> list[str]:
        if not entities:
            return []

        sample = entities[0]
        sample_dict = sample.model_dump()
        return [
            field_name
            for field_name, value in sample_dict.items()
            if isinstance(value, (str, int, float, bool, dt.date, dt.datetime))
        ]

    def _apply_filters(
        self,
        entities: list[T],
        filters: list[FilterConditionDTO],
    ) -> list[T]:
        if not filters:
            return entities

        return [
            entity
            for entity in entities
            if all(self._matches_filter(entity, condition) for condition in filters)
        ]

    def _matches_filter(
        self,
        entity: BaseEntity,
        condition: FilterConditionDTO,
    ) -> bool:
        value = getattr(entity, condition.field, None)
        operator = condition.operator
        expected = condition.value

        if operator == FilterOperator.IS_NULL:
            return value is None

        if operator == FilterOperator.EQ:
            return value == expected
        if operator == FilterOperator.NE:
            return value != expected
        if operator == FilterOperator.GT:
            return self._safe_compare(value, expected, lambda a, b: a > b)
        if operator == FilterOperator.GTE:
            return self._safe_compare(value, expected, lambda a, b: a >= b)
        if operator == FilterOperator.LT:
            return self._safe_compare(value, expected, lambda a, b: a < b)
        if operator == FilterOperator.LTE:
            return self._safe_compare(value, expected, lambda a, b: a <= b)
        if operator == FilterOperator.IN:
            return self._in_operator(value, expected)
        if operator == FilterOperator.NOT_IN:
            return not self._in_operator(value, expected)
        if operator == FilterOperator.CONTAINS:
            return self._text_operator(value, expected, mode="contains")
        if operator == FilterOperator.STARTSWITH:
            return self._text_operator(value, expected, mode="startswith")
        if operator == FilterOperator.ENDSWITH:
            return self._text_operator(value, expected, mode="endswith")

        return False

    def _safe_compare(
        self,
        left: t.Any,
        right: t.Any,
        comparator: t.Callable[[t.Any, t.Any], bool],
    ) -> bool:
        if left is None or right is None:
            return False
        try:
            return comparator(left, right)
        except TypeError:
            return False

    def _in_operator(self, value: t.Any, expected: t.Any) -> bool:
        if expected is None:
            return False
        if isinstance(expected, (str, bytes)):
            return value == expected
        if not isinstance(expected, (list, tuple, set, frozenset)):
            return False
        return value in expected

    def _text_operator(self, value: t.Any, expected: t.Any, mode: str) -> bool:
        if value is None or expected is None:
            return False
        left = str(value).lower()
        right = str(expected).lower()
        if mode == "contains":
            return right in left
        if mode == "startswith":
            return left.startswith(right)
        if mode == "endswith":
            return left.endswith(right)
        return False

    def _apply_sort(
        self,
        entities: list[T],
        sort_conditions: list[SortConditionDTO],
    ) -> list[T]:
        if not sort_conditions:
            return entities

        ordered = list(entities)
        for condition in reversed(sort_conditions):
            reverse = condition.direction == SortDirection.DESC
            ordered.sort(
                key=lambda item: self._safe_sort_value(getattr(item, condition.field, None)),
                reverse=reverse,
            )
        return ordered

    def _safe_sort_value(self, value: t.Any) -> tuple[int, int, t.Any]:
        if value is None:
            return (1, 0, None)
        if isinstance(value, bool):
            return (0, 0, int(value))
        if isinstance(value, (int, float)):
            return (0, 1, float(value))
        if isinstance(value, dt.datetime):
            return (0, 2, value.timestamp())
        if isinstance(value, dt.date):
            return (0, 3, value.toordinal())
        if isinstance(value, str):
            return (0, 4, value.casefold())
        return (0, 5, str(value))
