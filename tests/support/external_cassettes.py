from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.core.kusto_client import KustoColumn


CASSETTES_DIR = Path(__file__).resolve().parents[1] / "cassettes"


def load_external_cassette_payload(name: str) -> dict[str, Any]:
    path = CASSETTES_DIR / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"Cassette not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Cassette payload must be a JSON object: {path}")
    return payload


def load_kusto_cassette_rows_and_schema(name: str) -> tuple[list[dict[str, Any]], tuple[KustoColumn, ...]]:
    payload = load_external_cassette_payload(name)
    columns_raw = payload.get("columns")
    rows_raw = payload.get("rows")
    if not isinstance(columns_raw, list) or not isinstance(rows_raw, list):
        raise ValueError(f"Kusto cassette {name!r} must define list fields 'columns' and 'rows'.")

    columns: list[KustoColumn] = []
    column_names: list[str] = []
    for column in columns_raw:
        if not isinstance(column, dict):
            raise ValueError(f"Kusto cassette {name!r} columns must be JSON objects.")
        column_name = str(column.get("name", "")).strip()
        if not column_name:
            raise ValueError(f"Kusto cassette {name!r} column names must be non-empty.")
        column_names.append(column_name)
        type_name = column.get("type_name")
        columns.append(KustoColumn(name=column_name, type_name=str(type_name) if type_name is not None else None))

    rows: list[dict[str, Any]] = []
    for row in rows_raw:
        if not isinstance(row, dict):
            raise ValueError(f"Kusto cassette {name!r} rows must be JSON objects.")
        rows.append({column_name: row.get(column_name) for column_name in column_names})
    return rows, tuple(columns)


def load_aoai_cassette_response(name: str) -> Any:
    payload = load_external_cassette_payload(name)
    content = payload.get("content")
    usage = payload.get("usage")
    if not isinstance(content, str):
        raise ValueError(f"AOAI cassette {name!r} must define string field 'content'.")
    if not isinstance(usage, dict):
        raise ValueError(f"AOAI cassette {name!r} must define object field 'usage'.")

    prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = int(usage.get("completion_tokens", 0) or 0)
    total_tokens = int(usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
    return SimpleNamespace(
        usage=SimpleNamespace(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
        ),
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
    )
