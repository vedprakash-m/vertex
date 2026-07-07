from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from datetime import datetime, timezone

from src.core.engms_content import fetch_engms_page_summary
from src.core.integration_types import ExtractionResult
from src.core.models import Confidence, WorkItem
from src.core.models_v2 import Signal


_ENGMS_URL_PATTERN = re.compile(r"https://eng\.ms/[^\s\"'<>]+")
_HASH_KEY_PREFIX = "engms_hash:"
_TRAILING_PUNCT = re.compile(r"[.,;:)\]]+$")


class EngMsSignalExtractor:
    """Detects ADO work item descriptions that reference eng.ms pages updated since the
    last confirmed issue.  Produces ``Signal(source="engms", confidence=LOW)`` for each
    page whose content hash has changed (or that has never been seen before)."""

    def extract(
        self,
        work_items: Sequence[WorkItem],
        program_id: str,
        *,
        previous_hashes: dict[str, str] | None = None,
        max_urls: int = 50,
    ) -> ExtractionResult:
        prev: dict[str, str] = previous_hashes or {}
        url_to_item_ids: dict[str, list[int]] = _collect_urls(work_items)

        signals: list[Signal] = []
        current_hashes: dict[str, str] = {}

        # Bound per-run network fetches to keep gather latency predictable.
        for url, item_ids in list(url_to_item_ids.items())[:max_urls]:
            summary = fetch_engms_page_summary(url)
            if summary is None:
                continue
            content_hash = _short_hash(summary)
            current_hashes[url] = content_hash

            prev_hash = prev.get(url)
            if prev_hash is not None and prev_hash == content_hash:
                continue  # content unchanged — no signal

            changed = prev_hash is not None
            label = "updated" if changed else "new reference"
            text = f"eng.ms page {label}: {url}"
            sig_id = f"engms/{_short_hash(url)}"
            signals.append(
                Signal(
                    id=sig_id,
                    timestamp=datetime.now(tz=timezone.utc),
                    source="engms",
                    program_id=program_id,
                    workstream_id=None,
                    entity_refs=_entity_refs_for_items(item_ids),
                    text=text,
                    raw_ref=url,
                    confidence=Confidence.LOW,
                    metadata={
                        "url": url,
                        "hash": content_hash,
                        "changed": changed,
                    },
                )
            )

        side_artifacts: dict[str, str | int | float | bool | None] = {
            f"{_HASH_KEY_PREFIX}{url}": h for url, h in current_hashes.items()
        }
        return ExtractionResult(
            channel="engms",
            signals=tuple(signals),
            trajectory_points=(),
            side_artifacts=side_artifacts,
            errors=(),
        )


def hashes_from_artifacts(side_artifacts: dict[str, str | int | float | bool | None]) -> dict[str, str]:
    """Reconstruct previous_hashes from a prior run's ``ExtractionResult.side_artifacts``."""
    result: dict[str, str] = {}
    for key, value in side_artifacts.items():
        if key.startswith(_HASH_KEY_PREFIX) and isinstance(value, str):
            url = key[len(_HASH_KEY_PREFIX):]
            result[url] = value
    return result


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _collect_urls(work_items: Sequence[WorkItem]) -> dict[str, list[int]]:
    """Return mapping of eng.ms URL → list of ADO work item IDs that reference it."""
    url_to_ids: dict[str, list[int]] = {}
    for item in work_items:
        for field_key in ("System.Description", "description"):
            value = item.custom_fields.get(field_key)
            if not isinstance(value, str) or not value.strip():
                continue
            for raw_url in _ENGMS_URL_PATTERN.findall(value):
                url = _TRAILING_PUNCT.sub("", raw_url)
                if url not in url_to_ids:
                    url_to_ids[url] = []
                if item.id not in url_to_ids[url]:
                    url_to_ids[url].append(item.id)
            break  # description found in first matching key — skip remaining aliases
    return url_to_ids


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _entity_refs_for_items(item_ids: Sequence[int]) -> tuple[str, ...]:
    refs: list[str] = []
    for item_id in item_ids[:5]:
        refs.extend((f"ado:{item_id}", f"WI:{item_id}"))
    return tuple(refs)
