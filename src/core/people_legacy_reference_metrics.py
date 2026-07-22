"""specs/backlog.md WO-6 (BL-J1, schema-3.0 horizon): measures how often
`find_person`/`find_team` (src/core/people_query.py) resolve a `--person`/
`--team` reference via the legacy alias-keyed compatibility path
(`resolve_ref_to_canonical_entity_id`'s `resolved_via="alias_match"`)
rather than an already-canonical `entity_id`.

Warn-only, never-blocking measurement: this module intentionally does not
pick a horizon threshold ("zero legacy reads across N weeks") -- that is a
human decision (see WO-6's own "stop and ask" note in specs/backlog.md).
It ships the raw count only, surfaced by `registry_legacy_reference_check`
in src/commands/doctor_checks/kb_checks.py.

An append-only JSONL log (the platform's established low-risk counter
idiom -- see jsonl_utils.append_jsonl_line) rather than a single mutable
counter file, so concurrent readers never race on a read-modify-write.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path

from src.core.jsonl_utils import append_jsonl_line

_LEGACY_REFERENCE_LOG_FILENAME = "_legacy_reference_log.jsonl"
_MAX_LOG_BYTES = 10_000_000


def get_legacy_reference_log_path(knowledge_root: Path) -> Path:
    return knowledge_root / _LEGACY_REFERENCE_LOG_FILENAME


def record_legacy_alias_reference(knowledge_root: Path, *, entity_type: str, ref: str) -> None:
    """Append one entry for a single legacy-alias-keyed resolution.

    Best-effort: a failure to record must never break the caller's actual
    lookup, so this never raises.
    """
    entry = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "entity_type": entity_type,
        "ref": ref,
    }
    try:
        append_jsonl_line(get_legacy_reference_log_path(knowledge_root), json.dumps(entry, sort_keys=True) + "\n", max_bytes=_MAX_LOG_BYTES)
    except OSError:
        pass


@dataclass(frozen=True, slots=True)
class LegacyReferenceSummary:
    legacy_reference_count: int
    sample_refs: tuple[str, ...]


def summarize_legacy_reference_log(knowledge_root: Path) -> LegacyReferenceSummary:
    path = get_legacy_reference_log_path(knowledge_root)
    if not path.exists():
        return LegacyReferenceSummary(legacy_reference_count=0, sample_refs=())
    count = 0
    sample_refs: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        count += 1
        if len(sample_refs) < 5:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            ref = entry.get("ref")
            if isinstance(ref, str):
                sample_refs.append(ref)
    return LegacyReferenceSummary(legacy_reference_count=count, sample_refs=tuple(sample_refs))
