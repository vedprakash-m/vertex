"""Read REV cycle history without repairing retained records."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.jsonl_utils import parse_jsonl_line


class InvalidCycleHistory(ValueError):
    """Retained cycle history cannot be read without repair."""


def read_cycle_history_without_repair(path: Path) -> list[dict[str, Any]] | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise InvalidCycleHistory from exc

    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            record = parse_jsonl_line(line)
        except (ValueError, RecursionError) as exc:
            raise InvalidCycleHistory from exc
        if not isinstance(record, dict):
            raise InvalidCycleHistory
        try:
            int(record.get("llm_fallback_count", 0) or 0)
        except (TypeError, ValueError, OverflowError) as exc:
            raise InvalidCycleHistory from exc
        records.append(record)
    return records
