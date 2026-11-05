from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
import json
from typing import Any, TypeVar
from uuid import UUID

from src.core.exceptions import ConfigError, QueryError

_Row = TypeVar("_Row", bound=Mapping[str, Any])


@dataclass(frozen=True, slots=True)
class KustoResultLimits:
    projected_columns: tuple[str, ...]
    max_rows: int
    max_bytes: int

    def __post_init__(self) -> None:
        if (
            not isinstance(self.projected_columns, tuple) or not self.projected_columns
            or any(not isinstance(name, str) or not name.strip() for name in self.projected_columns)
            or len(set(self.projected_columns)) != len(self.projected_columns)
        ):
            raise ConfigError("Kusto result_limits.projected_columns must be distinct nonblank column names.")
        if type(self.max_rows) is not int or self.max_rows < 1:
            raise ConfigError("Kusto result_limits.max_rows must be a positive integer.")
        if type(self.max_bytes) is not int or self.max_bytes < 2:
            raise ConfigError("Kusto result_limits.max_bytes must be an integer of at least 2 (JSON array bytes).")


def parse_kusto_result_limits(value: object) -> KustoResultLimits | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {"projected_columns", "max_rows", "max_bytes"}:
        raise ConfigError("Kusto result_limits requires only projected_columns, max_rows and max_bytes.")
    columns = value["projected_columns"]
    if not isinstance(columns, (list, tuple)) or any(not isinstance(name, str) for name in columns):
        raise ConfigError("Kusto result_limits.projected_columns must be a list of column names.")
    return KustoResultLimits(
        projected_columns=tuple(columns), max_rows=value["max_rows"], max_bytes=value["max_bytes"],
    )


def admit_kusto_rows(rows: Iterable[_Row], result_limits: KustoResultLimits | None) -> list[_Row]:
    if result_limits is None:
        return list(rows)
    admitted: list[_Row] = []
    decoded_bytes = 2
    for row in rows:
        if len(admitted) >= result_limits.max_rows:
            raise QueryError("Kusto result exceeds the declared row budget.")
        if not isinstance(row, Mapping) or tuple(row) != result_limits.projected_columns:
            raise QueryError("Kusto result schema does not match the declared projected columns.")
        decoded_bytes += decoded_kusto_row_bytes(row) + bool(admitted)
        if decoded_bytes > result_limits.max_bytes:
            raise QueryError("Kusto result exceeds the declared decoded-byte budget.")
        admitted.append(row)
    return admitted


def decoded_kusto_row_bytes(row: Mapping[str, Any]) -> int:
    try:
        return len(json.dumps(
            dict(row), ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=_encode_value,
        ).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError) as error:
        raise QueryError("Kusto result cannot be measured against the declared decoded-byte budget.") from error


def _encode_value(value: object) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (UUID, timedelta)):
        return str(value)
    if isinstance(value, Decimal) and value.is_finite():
        return str(value)
    raise TypeError("Unsupported Kusto result value.")
