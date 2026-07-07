from __future__ import annotations

import re
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.models_v2 import Signal, Workstream


_WORK_ITEM_REF_PATTERN = re.compile(
    r"\bWI[:#-]?\s*(\d{4,})\b|\bADO#(\d{4,})\b|\bwork item\s+(\d{4,})\b|\bbug\s*#?\s*(\d{4,})\b|\bpbi\s*#?\s*(\d{4,})\b|\buser story\s*#?\s*(\d{4,})\b|\btask\s*#?\s*(\d{4,})\b",
    flags=re.IGNORECASE,
)


def extract_work_item_refs(value: str) -> tuple[str, ...]:
    refs = {
        f"WI:{group}"
        for match in _WORK_ITEM_REF_PATTERN.finditer(value)
        for group in match.groups()
        if group is not None
    }
    if work_item_id := _parse_item_id_from_url(value):
        refs.add(f"WI:{work_item_id}")
    return tuple(sorted(refs))


def merge_entity_refs(
    *,
    provider_refs: tuple[str, ...],
    workstream_id: str | None,
    additional_refs: tuple[str, ...] = (),
) -> tuple[str, ...]:
    refs = list(provider_refs)
    if workstream_id:
        refs.append(f"WS:{workstream_id}")
    refs.extend(additional_refs)
    return tuple(refs)


def _parse_item_id_from_url(value: str) -> int | None:
    normalized = value.strip().rstrip("/")
    for marker in ("/_workitems/edit/", "/workitems/"):
        if marker not in normalized:
            continue
        tail = normalized.rsplit(marker, 1)[-1]
        digits = "".join(character for character in tail if character.isdigit())
        if digits:
            return int(digits)
    return None


def widen_ws_wi_refs(signal: Signal, workstreams: tuple[Workstream, ...]) -> Signal:
    """FR-SG-08 slice: add workstream-scoped WI: refs when no text-derived WI: refs exist.

    When a collaboration signal has a resolved workstream but no explicit WI: refs
    (the artifact mentioned no work items by text), fall back to the work_item_ids
    declared on that workstream's configured sources (teams_meeting_series, teams_chats,
    email_threads). This closes the exit bar for lower-confidence artifacts that arrive
    via keyword-discovery without explicit @WI text mentions.
    """
    if not signal.workstream_id:
        return signal
    if any(r.startswith("WI:") for r in signal.entity_refs):
        return signal
    ws = next((w for w in workstreams if w.id == signal.workstream_id), None)
    if ws is None or ws.signal_sources is None:
        return signal
    wi_ids: set[int] = set()
    for series in ws.signal_sources.teams_meeting_series:
        wi_ids.update(series.work_item_ids)
    for chat in ws.signal_sources.teams_chats:
        wi_ids.update(chat.work_item_ids)
    for thread in ws.signal_sources.email_threads:
        wi_ids.update(thread.work_item_ids)
    if not wi_ids:
        return signal
    wi_refs = tuple(f"WI:{wid}" for wid in sorted(wi_ids))
    return replace(signal, entity_refs=(*signal.entity_refs, *wi_refs))
