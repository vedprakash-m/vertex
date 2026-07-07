from __future__ import annotations

import re


def candidate_sentences(text: str) -> tuple[str, ...]:
    candidates: list[str] = []
    for chunk in re.split(r"(?<=[.!?])\s+|\n+", text):
        normalized = " ".join(chunk.split()).strip(" -\t")
        if normalized:
            candidates.append(normalized)
    return tuple(candidates)
