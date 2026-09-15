import enum
import typing as t

from pydantic import Field

from .base import DTO


class FilterOperator(str, enum.Enum):
    EQ = "eq"
    NE = "ne"
    GT = "gt"
    GTE = "gte"
    LT = "lt"
    LTE = "lte"
    IN = "in"
    NOT_IN = "not_in"
    CONTAINS = "contains"
    STARTSWITH = "startswith"
    ENDSWITH = "endswith"
    IS_NULL = "is_null"


class SortDirection(str, enum.Enum):
    ASC = "asc"
    DESC = "desc"


class FilterConditionDTO(DTO):
    field: str = Field(min_length=1)
    operator: FilterOperator = FilterOperator.EQ
    value: t.Any = None


class SortConditionDTO(DTO):
    field: str = Field(min_length=1)
    direction: SortDirection = SortDirection.ASC


def _empty_search_fields() -> list[str]:
    return []


def _empty_filters() -> list[FilterConditionDTO]:
    return []


def _empty_sort() -> list[SortConditionDTO]:
    return []


class QueryRequestDTO(DTO):
    limit: int = Field(default=50, ge=1)
    offset: int = Field(default=0, ge=0)
    search: str | None = None
    search_fields: list[str] = Field(default_factory=_empty_search_fields)
    filters: list[FilterConditionDTO] = Field(default_factory=_empty_filters)
    sort: list[SortConditionDTO] = Field(default_factory=_empty_sort)


class QueryResponseDTO(DTO):
    items: list[t.Any] = Field(default_factory=list)
    total: int = 0
    limit: int = 0
    offset: int = 0
    has_next: bool = False
