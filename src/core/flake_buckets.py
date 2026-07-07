"""PB-49: per-test flake-bucket tracking.

A "flake" is a test that intermittently fails AND passes (i.e. a test that
is non-deterministic under load, network, or timing). The simplest way to
measure a flake is to count how many times a given `test_id` shows up in
the test run's "failed this run, passed next run" history.

This module is the canonical place to record, read, and triage flakes. The
sidecar lives at `programs/<program>/_state/flake_buckets.jsonl` and is
portalocker-routed through `append_jsonl_line` (PB-37 compliant). The state
reader registry entry is added in `src/core/state_reader_registry.py`.

Design notes:
- We do NOT run test-suite instrumentation in this module — we just own the
  append + read API. A separate script (`scripts/record_flake.py`) consumes
  pytest's junitxml and calls `record_flake()` for each observed flake.
- The bucket status lifecycle is: `open` -> `quarantined` -> `fixed`. Any
  new occurrence of an already-known test id moves `flake_count` up and
  refreshes `last_seen_at`; the status is mutated explicitly via
  `quarantine_flake()` / `mark_flake_fixed()`.
- The recorded `owner` is the @pytest.mark.owner value if present, else
  the first dotted segment of the test path (e.g. `tests/unit` -> `unit`).
- "Programs" here are an abstraction: in CI, the "program" is the
  CI matrix axis (e.g. `py3.11-ubuntu`, `py3.13-windows`). For local dev
  it's just `local`. This keeps the sidecar location consistent with
  every other governance sidecar without inventing a new root.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from src.core.jsonl_utils import append_jsonl_line, parse_jsonl_line


class FlakeStatus(str, Enum):
    OPEN = "open"
    QUARANTINED = "quarantined"
    FIXED = "fixed"


@dataclass(frozen=True, slots=True)
class FlakeBucket:
    test_id: str
    flake_count: int
    first_seen_at: datetime
    last_seen_at: datetime
    status: FlakeStatus
    owner: str | None = None
    suggested_action: str | None = None
    # Internal monotonic write counter so a re-read can dedupe by row order
    # even if the same test_id appears multiple times (each occurrence
    # produces a new row; the latest row per test_id is the current bucket).
    seq: int = 0

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "FlakeBucket":
        return cls(
            test_id=str(record["test_id"]),
            flake_count=int(record.get("flake_count", 0)),
            first_seen_at=datetime.fromisoformat(record["first_seen_at"]),
            last_seen_at=datetime.fromisoformat(record["last_seen_at"]),
            status=FlakeStatus(record.get("status", "open")),
            owner=record.get("owner"),
            suggested_action=record.get("suggested_action"),
            seq=int(record.get("seq", 0)),
        )

    def to_record(self) -> dict[str, Any]:
        d = asdict(self)
        d["first_seen_at"] = self.first_seen_at.isoformat()
        d["last_seen_at"] = self.last_seen_at.isoformat()
        d["status"] = self.status.value
        return d


# Per-program (or per-CI-axis) sidecar path. Kept in a helper for test
# substitution and registry consistency.
def flake_buckets_path(program_id: str, *, programs_root: Path) -> Path:
    """Return the path to the per-program flake-buckets sidecar.

    The sidecar lives under ``_state/`` rather than ``journal/`` because
    it is NOT a journal of program events; it is a CI-quality sidecar
    that travels with the program's governance audit trail.
    """
    return programs_root / program_id / "_state" / "flake_buckets.jsonl"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _existing_buckets(path: Path) -> dict[str, FlakeBucket]:
    """Read all rows from the sidecar and collapse to the latest row per test_id."""
    if not path.exists():
        return {}
    latest: dict[str, FlakeBucket] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = parse_jsonl_line(line)
        except json.JSONDecodeError:
            # Corrupt row; skip per the "quarantine on next write" pattern
            # (we do NOT silently fail — a future script can list and clean).
            continue
        bucket = FlakeBucket.from_record(rec)
        existing = latest.get(bucket.test_id)
        if existing is None or bucket.seq > existing.seq:
            latest[bucket.test_id] = bucket
    return latest


def _next_seq(path: Path) -> int:
    if not path.exists():
        return 1
    max_seq = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = parse_jsonl_line(line)
        except json.JSONDecodeError:
            continue
        max_seq = max(max_seq, int(rec.get("seq", 0)))
    return max_seq + 1


def record_flake(
    test_id: str,
    *,
    program_id: str = "local",
    programs_root: Path,
    owner: str | None = None,
    suggested_action: str | None = None,
) -> FlakeBucket:
    """Record (or re-record) a flake occurrence for `test_id`.

    The new row carries:
    - `flake_count = previous_count + 1` (or 1 if first time)
    - `first_seen_at = previous_first_seen_at` (or now)
    - `last_seen_at = now`
    - `status = previous_status` (open|quarantined|fixed preserved)
    - `seq = max_seq + 1`
    """
    path = flake_buckets_path(program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = _existing_buckets(path)
    prev = existing.get(test_id)
    now = _now()
    bucket = FlakeBucket(
        test_id=test_id,
        flake_count=(prev.flake_count + 1) if prev else 1,
        first_seen_at=(prev.first_seen_at if prev else now),
        last_seen_at=now,
        status=(prev.status if prev else FlakeStatus.OPEN),
        owner=owner or (prev.owner if prev else None),
        suggested_action=suggested_action or (prev.suggested_action if prev else None),
        seq=_next_seq(path),
    )
    line = json.dumps(bucket.to_record(), sort_keys=True) + "\n"
    append_jsonl_line(path, line, max_bytes=_FLAKE_BUCKETS_MAX_BYTES)
    return bucket


def quarantine_flake(
    test_id: str,
    owner: str,
    *,
    program_id: str = "local",
    programs_root: Path,
    reason: str | None = None,
) -> FlakeBucket:
    """Move a flake bucket to `quarantined` with an owner.

    Quarantined = "known flaky, intentionally skipped via @pytest.mark.skip
    or marked.xfail in the test file; do NOT block on this in CI." A
    quarantined flake is a contract: someone has accepted the flakiness
    and promised to fix it on a date.
    """
    path = flake_buckets_path(program_id, programs_root=programs_root)
    existing = _existing_buckets(path)
    prev = existing.get(test_id)
    now = _now()
    bucket = FlakeBucket(
        test_id=test_id,
        flake_count=(prev.flake_count if prev else 1),
        first_seen_at=(prev.first_seen_at if prev else now),
        last_seen_at=now,
        status=FlakeStatus.QUARANTINED,
        owner=owner,
        suggested_action=reason,
        seq=_next_seq(path),
    )
    line = json.dumps(bucket.to_record(), sort_keys=True) + "\n"
    append_jsonl_line(path, line, max_bytes=_FLAKE_BUCKETS_MAX_BYTES)
    return bucket


def mark_flake_fixed(
    test_id: str,
    *,
    program_id: str = "local",
    programs_root: Path,
) -> FlakeBucket:
    """Move a flake bucket to `fixed`. Used after the test passes 30+ times in a row."""
    path = flake_buckets_path(program_id, programs_root=programs_root)
    existing = _existing_buckets(path)
    prev = existing.get(test_id)
    if prev is None:
        raise LookupError(f"no flake record for {test_id!r}")
    now = _now()
    bucket = FlakeBucket(
        test_id=test_id,
        flake_count=prev.flake_count,
        first_seen_at=prev.first_seen_at,
        last_seen_at=now,
        status=FlakeStatus.FIXED,
        owner=prev.owner,
        suggested_action=prev.suggested_action,
        seq=_next_seq(path),
    )
    line = json.dumps(bucket.to_record(), sort_keys=True) + "\n"
    append_jsonl_line(path, line, max_bytes=_FLAKE_BUCKETS_MAX_BYTES)
    return bucket


def read_flake_buckets(
    *, program_id: str = "local", programs_root: Path
) -> tuple[FlakeBucket, ...]:
    """Return the latest bucket per test_id."""
    path = flake_buckets_path(program_id, programs_root=programs_root)
    return tuple(_existing_buckets(path).values())


# 1 MB cap on the sidecar — enough for ~5k rows; rotation handles the rest.
_FLAKE_BUCKETS_MAX_BYTES = 1 * 1024 * 1024
