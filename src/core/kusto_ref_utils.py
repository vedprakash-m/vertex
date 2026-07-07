from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from src.core.signal_ref_utils import extract_work_item_refs


def extract_kusto_entity_refs(rows: Sequence[Mapping[str, Any]]) -> tuple[str, ...]:
    refs: set[str] = set()
    for row in rows:
        for key, value in row.items():
            normalized_key = key.strip().lower()
            if "workitem" in normalized_key or normalized_key.endswith("wi"):
                work_item_id = _extract_digits(value)
                if work_item_id is not None:
                    refs.add(f"WI:{work_item_id}")
                    continue
            if "incident" in normalized_key:
                incident_id = _extract_digits(value)
                if incident_id is not None:
                    refs.add(f"ICM:{incident_id}")
                    continue
            text_value = _optional_string(value)
            if text_value is None:
                continue
            refs.update(extract_work_item_refs(text_value))
            incident_match = re.search(r"\bICM[-:# ]?(\d{4,})\b", text_value, flags=re.IGNORECASE)
            if incident_match is not None:
                refs.add(f"ICM:{incident_match.group(1)}")
    return tuple(sorted(refs))


def _extract_digits(value: Any) -> str | None:
    text = _optional_string(value)
    if text is None:
        return None
    match = re.search(r"(\d{4,})", text)
    if match is None:
        return None
    return match.group(1)


def _optional_string(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)
