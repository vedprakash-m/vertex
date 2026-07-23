"""specs/backlog.md WO-6 (BL-J1, schema-3.0 horizon): measures how often
`find_person`/`find_team` (src/core/people_query.py) resolve a `--person`/
`--team` reference via the legacy alias-keyed compatibility path
(`resolve_ref_to_canonical_entity_id`'s `resolved_via="alias_match"`)
rather than an already-canonical `entity_id`.

Warn-only, never-blocking measurement, surfaced by
`registry_legacy_reference_check` in src/commands/doctor_checks/kb_checks.py.

An append-only JSONL log (the platform's established low-risk counter
idiom -- see jsonl_utils.append_jsonl_line) rather than a single mutable
counter file, so concurrent readers never race on a read-modify-write.

**BL-J1 horizon decision (2026-07-22):** WO-6 deliberately shipped the raw
count only and deferred the numeric horizon threshold to a human decision
(see WO-6's "stop and ask" note in specs/bklg.md). The operator was asked
directly, given the real data at the time (zero legacy-alias reads ever
recorded on either live program) and three alternatives, and chose:
**zero legacy-alias reads across 8 consecutive weeks.** `evaluate_schema_3_0_horizon`
below implements that condition.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path

from src.core.jsonl_utils import append_jsonl_line

_LEGACY_REFERENCE_LOG_FILENAME = "_legacy_reference_log.jsonl"
_MAX_LOG_BYTES = 10_000_000

# BL-J1 decision, 2026-07-22: the horizon condition gating schema-3.0 hard
# removal of alias-keyed compatibility fields.
HORIZON_WINDOW_WEEKS = 8

# The date WO-6's instrumentation shipped (this module's own creation date).
# Required so "no data yet" (instrumentation hasn't run long enough to say
# anything) can never be mistaken for "confirmed zero usage" on day one.
INSTRUMENTATION_LIVE_SINCE = date(2026, 7, 22)


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


@dataclass(frozen=True, slots=True)
class HorizonStatus:
    met: bool
    reason: str
    weeks_since_instrumentation_live: float
    weeks_since_last_legacy_read: float | None  # None if never recorded


def evaluate_schema_3_0_horizon(knowledge_root: Path, *, now: datetime | None = None) -> HorizonStatus:
    """BL-J1: has the schema-3.0 horizon condition been met for this
    program's knowledge root -- zero legacy-alias reads across
    `HORIZON_WINDOW_WEEKS` consecutive weeks?

    Two guards, both required, so a brand-new or rarely-used instrumentation
    path can't trivially satisfy this on day one just because nothing has
    been recorded yet:
      1. At least `HORIZON_WINDOW_WEEKS` must have elapsed since the counter
         itself went live (`INSTRUMENTATION_LIVE_SINCE`) -- "no data yet" is
         not the same as "confirmed zero usage."
      2. No legacy-alias read is recorded within the trailing
         `HORIZON_WINDOW_WEEKS` window.
    """
    now = now or datetime.now(timezone.utc)
    weeks_live = (now.date() - INSTRUMENTATION_LIVE_SINCE).days / 7

    if weeks_live < HORIZON_WINDOW_WEEKS:
        return HorizonStatus(
            met=False,
            reason=(
                f"instrumentation has only been live {weeks_live:.1f} of the required "
                f"{HORIZON_WINDOW_WEEKS} weeks -- no data yet is not the same as confirmed zero usage"
            ),
            weeks_since_instrumentation_live=weeks_live,
            weeks_since_last_legacy_read=None,
        )

    path = get_legacy_reference_log_path(knowledge_root)
    last_recorded_at: datetime | None = None
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                recorded_at = datetime.fromisoformat(entry["recorded_at"])
            except (json.JSONDecodeError, KeyError, ValueError):
                continue
            if last_recorded_at is None or recorded_at > last_recorded_at:
                last_recorded_at = recorded_at

    if last_recorded_at is None:
        return HorizonStatus(
            met=True,
            reason=f"no legacy-alias reads ever recorded and instrumentation has been live {weeks_live:.1f} weeks",
            weeks_since_instrumentation_live=weeks_live,
            weeks_since_last_legacy_read=None,
        )

    weeks_since_last = (now - last_recorded_at).total_seconds() / (7 * 24 * 3600)
    if weeks_since_last >= HORIZON_WINDOW_WEEKS:
        return HorizonStatus(
            met=True,
            reason=(
                f"last legacy-alias read was {weeks_since_last:.1f} weeks ago, "
                f"past the {HORIZON_WINDOW_WEEKS}-week window"
            ),
            weeks_since_instrumentation_live=weeks_live,
            weeks_since_last_legacy_read=weeks_since_last,
        )
    return HorizonStatus(
        met=False,
        reason=(
            f"a legacy-alias read was recorded {weeks_since_last:.1f} weeks ago, "
            f"within the {HORIZON_WINDOW_WEEKS}-week window"
        ),
        weeks_since_instrumentation_live=weeks_live,
        weeks_since_last_legacy_read=weeks_since_last,
    )
