from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

from src.core.exceptions import QueryError
from src.core.integration_types import (
    KustoResultColumn,
    KustoResultKind,
    KustoResultSchema,
    KustoResultSet,
)
from src.core.kusto_result_limits import KustoResultLimits, admit_kusto_rows

_TEMPORAL_TYPE_NAMES = frozenset({"date", "datetime", "timestamp"})
_NUMERIC_TYPE_NAMES = frozenset({
    "decimal",
    "double",
    "float",
    "int",
    "int64",
    "integer",
    "long",
    "real",
})


def build_kusto_result_schema(
    rows: Sequence[Mapping[str, Any]],
    *,
    columns: tuple[KustoResultColumn, ...] = (),
    expected_cardinality: str = "zero_ok",
    strict_rows: bool = True,
) -> KustoResultSchema:
    resolved_columns = columns or _infer_columns(rows)
    names = tuple(column.name for column in resolved_columns)
    if len(names) != len(set(names)):
        raise QueryError("Kusto result schema contains duplicate column names.")
    if strict_rows:
        for row in rows:
            if tuple(row) != names:
                raise QueryError("Kusto result rows do not match the admitted schema.")

    if expected_cardinality == "scalar_required":
        kind = KustoResultKind.SCALAR
    else:
        kind = KustoResultKind.TABLE
    time_column = next(
        (
            column.name
            for column in resolved_columns
            if _is_temporal_column(column)
        ),
        None,
    )
    value_columns = tuple(
        column.name
        for column in resolved_columns
        if column.name != time_column and _is_numeric_column(column)
    )
    if expected_cardinality != "scalar_required" and time_column and value_columns:
        kind = KustoResultKind.TIME_SERIES
    return KustoResultSchema(
        kind=kind,
        columns=resolved_columns,
        row_count=len(rows),
        time_column=time_column,
        value_columns=value_columns,
    )


def admit_kusto_execution(
    execution: list[dict[str, Any]] | tuple[dict[str, Any], ...] | KustoResultSet,
    *,
    query_id: str,
    expected_cardinality: str,
    result_limits: KustoResultLimits | None,
) -> tuple[list[dict[str, Any]], KustoResultSchema]:
    supplied_schema: KustoResultSchema | None = None
    if isinstance(execution, KustoResultSet):
        if execution.query_id != query_id:
            raise QueryError("Kusto typed result query identity does not match the admitted query.")
        if execution.is_partial:
            raise QueryError("Kusto typed result is partial and cannot be admitted.")
        if execution.row_count is not None and execution.row_count != len(execution.rows):
            raise QueryError("Kusto typed result row count does not match its admitted rows.")
        raw_rows: Sequence[Mapping[str, Any]] = execution.rows
        supplied_schema = execution.schema
    elif isinstance(execution, (list, tuple)):
        raw_rows = execution
    else:
        raise QueryError("Kusto executor returned an unsupported result type.")

    rows = admit_kusto_rows(raw_rows, result_limits)
    inferred_schema = build_kusto_result_schema(
        rows,
        columns=supplied_schema.columns if supplied_schema is not None else (),
        expected_cardinality=expected_cardinality,
        strict_rows=supplied_schema is not None,
    )
    if supplied_schema is not None:
        if supplied_schema.row_count != len(rows):
            raise QueryError("Kusto typed result schema row count does not match its rows.")
        if supplied_schema.kind is not inferred_schema.kind:
            raise QueryError("Kusto typed result schema kind does not match its rows.")
        if supplied_schema.time_column != inferred_schema.time_column:
            raise QueryError("Kusto typed result schema time column does not match its rows.")
        if supplied_schema.value_columns != inferred_schema.value_columns:
            raise QueryError("Kusto typed result schema value columns do not match its rows.")
    return [dict(row) for row in rows], inferred_schema


def _infer_columns(rows: Sequence[Mapping[str, Any]]) -> tuple[KustoResultColumn, ...]:
    if not rows:
        return ()
    return tuple(
        KustoResultColumn(name, _infer_type_name(tuple(row.get(name) for row in rows)))
        for name in rows[0]
    )


def _infer_type_name(values: tuple[Any, ...]) -> str | None:
    non_null = tuple(value for value in values if value is not None)
    if not non_null:
        return None
    if all(isinstance(value, bool) for value in non_null):
        return "bool"
    if all(_is_temporal_value(value) for value in non_null):
        return "datetime"
    if all(isinstance(value, int) and not isinstance(value, bool) for value in non_null):
        return "long"
    if all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in non_null):
        return "real"
    return "string"


def _is_temporal_value(value: Any) -> bool:
    if isinstance(value, datetime | date):
        return True
    if not isinstance(value, str):
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except (ValueError, OverflowError):
        return False


def _is_temporal_column(column: KustoResultColumn) -> bool:
    return (column.type_name or "").lower() in _TEMPORAL_TYPE_NAMES


def _is_numeric_column(column: KustoResultColumn) -> bool:
    return (column.type_name or "").lower() in _NUMERIC_TYPE_NAMES


__all__ = ["admit_kusto_execution", "build_kusto_result_schema"]
