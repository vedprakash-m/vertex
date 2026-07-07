from __future__ import annotations

import re

from src.core.signal_ref_utils import extract_work_item_refs

_SIGNAL_FRAGMENT_MIN_CHARS = 24


def split_signal_fragments(text: str) -> tuple[str, ...]:
    normalized_text = re.sub(r"[ \t]+", " ", text.replace("\r\n", "\n").replace("\r", "\n")).strip()
    if not normalized_text:
        return ()
    parts = [
        candidate.strip(" -*\t")
        for candidate in re.split(r"(?:\n+|(?<=;)\s+|^\s*[-*]\s+)", normalized_text, flags=re.MULTILINE)
        if candidate.strip(" -*\t")
    ]
    if len(parts) <= 1 and len(extract_work_item_refs(normalized_text)) > 1:
        parts = [
            candidate.strip()
            for candidate in re.split(
                r"(?<=[.!?])\s+(?=(?:WI|ADO#|work item|bug|pbi|user story|task)\b)",
                normalized_text,
                flags=re.IGNORECASE,
            )
            if candidate.strip()
        ]
    meaningful: list[str] = []
    seen: set[str] = set()
    for part in parts or [normalized_text]:
        candidate = " ".join(part.split())
        if len(candidate) < _SIGNAL_FRAGMENT_MIN_CHARS and not extract_work_item_refs(candidate):
            continue
        lowered = candidate.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        meaningful.append(candidate)
    if meaningful:
        return tuple(meaningful)
    fallback = " ".join(normalized_text.split())
    return (fallback,) if fallback else ()


def fragment_resource_id(*, resource_id: str, segment_index: int, segment_count: int) -> str:
    if segment_count <= 1:
        return resource_id
    return f"{resource_id}:seg:{segment_index}"
