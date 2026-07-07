from __future__ import annotations

from datetime import date, datetime
from typing import Any


def is_completed_state(state: str) -> bool:
    return state.strip().lower() in {"closed", "done", "resolved", "completed"}


def parse_date_sk(value: Any) -> int | None:
    try:
        raw_value = int(str(value))
    except (TypeError, ValueError):
        return None
    return raw_value if 19000101 <= raw_value <= 29991231 else None


def date_from_sk(value: int) -> date | None:
    try:
        return datetime.strptime(str(value), "%Y%m%d").date()
    except ValueError:
        return None


def date_to_sk(value: date) -> int:
    return int(value.strftime("%Y%m%d"))
