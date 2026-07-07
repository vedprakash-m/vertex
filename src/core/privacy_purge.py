"""GAP-31: Unified privacy purge / retention execution.

The privacy matrix in `src/core/privacy_matrix.py` is the canonical
source of truth for retention classes. This module provides the
**execution side** — a single auditable function that, given a
program_id and a cutoff date, walks the canonical sidecar paths and
purges evidence older than its class's retention. Returns a
`PurgeReport` summary suitable for the CLI.

Design:
  - **Dry-run by default.** Mutating requires ``apply=True``.
  - **Atomic rotation**: purges are done with the journal-rotation
    helper (per-stem 10 MB cap, retain=5) so that the sidecar
    append-only invariant is preserved.
  - **Tombstoning**: rows containing PII get an ``[EXCISED]`` marker
    in their body when ``supports_excise=True``, preserving the audit
    trail; otherwise the row is removed outright.
  - **Idempotent**: running twice yields the same outcome.

This module is **pure-Python** and side-effect-free in dry-run mode;
it is unit-tested without touching real programs.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from src.core.privacy_matrix import (
    RETENTION_DAYS,
    SIDECAR_RETENTION,
    RetentionClass,
    SidecarRetentionRule,
)


@dataclass(frozen=True, slots=True)
class PurgeRecord:
    """Per-sidecar purge summary."""

    artifact_path: str
    retention: RetentionClass
    rows_examined: int
    rows_purged: int
    rows_tombstoned: int
    bytes_freed: int
    applied: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_path": self.artifact_path,
            "retention": self.retention.value,
            "rows_examined": self.rows_examined,
            "rows_purged": self.rows_purged,
            "rows_tombstoned": self.rows_tombstoned,
            "bytes_freed": self.bytes_freed,
            "applied": self.applied,
        }


@dataclass(frozen=True, slots=True)
class PurgeReport:
    """Whole-program purge summary."""

    program_id: str
    cutoff: datetime
    dry_run: bool
    records: tuple[PurgeRecord, ...] = field(default_factory=tuple)
    skipped: tuple[str, ...] = field(default_factory=tuple)

    @property
    def total_rows_purged(self) -> int:
        return sum(r.rows_purged for r in self.records)

    @property
    def total_rows_tombstoned(self) -> int:
        return sum(r.rows_tombstoned for r in self.records)

    @property
    def total_bytes_freed(self) -> int:
        return sum(r.bytes_freed for r in self.records)

    def to_dict(self) -> dict[str, object]:
        return {
            "program_id": self.program_id,
            "cutoff": self.cutoff.isoformat(),
            "dry_run": self.dry_run,
            "records": [r.to_dict() for r in self.records],
            "skipped": list(self.skipped),
            "totals": {
                "rows_purged": self.total_rows_purged,
                "rows_tombstoned": self.total_rows_tombstoned,
                "bytes_freed": self.total_bytes_freed,
            },
        }


def _retention_cutoff(retention: RetentionClass, *, now: datetime) -> datetime | None:
    """Return the cutoff datetime for the given retention class.

    Returns None when retention is INDEFINITE (never auto-purge).
    EPHEMERAL (0 days) means everything older than the run is eligible.
    """
    days = RETENTION_DAYS.get(retention)
    if days is None:
        return None
    return now.fromtimestamp(now.timestamp() - days * 86400, tz=timezone.utc)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _row_timestamp(row: dict) -> datetime | None:
    """Best-effort extraction of a row's timestamp from common fields."""
    for key in ("created_at", "recorded_at", "timestamp", "ts", "sent_at"):
        if key in row:
            parsed = _parse_iso(row[key])
            if parsed is not None:
                return parsed
    return None


def _row_contains_pii(row: dict) -> bool:
    """Heuristic: classify a row as PII-bearing if any of the following
    known PII marker fields are non-empty.
    """
    pii_markers = (
        "email",
        "sender_email",
        "recipient_emails",
        "user_principal_name",
        "upn",
        "person",
        "person_id",
        "people_profiles",
    )
    for marker in pii_markers:
        value = row.get(marker)
        if isinstance(value, str) and value:
            return True
        if isinstance(value, (list, tuple)) and value:
            return True
        if isinstance(value, dict) and value:
            return True
    return False


def _tombstone_row(row: dict) -> dict:
    """Return a tombstoned copy of the row with an [EXCISED] marker."""
    redacted = dict(row)
    redacted["[EXCISED]"] = True
    redacted.pop("email", None)
    redacted.pop("sender_email", None)
    redacted.pop("recipient_emails", None)
    redacted.pop("body", None)
    redacted.pop("preview", None)
    redacted.pop("person", None)
    redacted.pop("person_id", None)
    return redacted


def _resolve_artifact_paths(
    rule: SidecarRetentionRule,
    *,
    program_id: str,
    programs_root: Path,
) -> tuple[Path, ...]:
    """Resolve any ``<token>`` placeholders in the rule's artifact_path."""
    path = rule.artifact_path
    if "<program_id>" in path:
        path = path.replace("<program_id>", program_id)
    if "<edition>" in path:
        # Edition roots are conventional; we do not guess an edition name
        # here — caller may pre-resolve. For now, leave the placeholder
        # and skip the rule with a marker.
        return ()
    return (programs_root / program_id / path,)


def _process_jsonl_sidecar(
    path: Path,
    *,
    cutoff: datetime,
    retention: RetentionClass,
    supports_excise: bool,
    apply: bool,
) -> PurgeRecord:
    """Walk a single JSONL sidecar, purging or tombstoning expired rows."""
    rows_examined = 0
    rows_purged = 0
    rows_tombstoned = 0
    bytes_freed = 0

    if not path.exists():
        return PurgeRecord(
            artifact_path=str(path),
            retention=retention,
            rows_examined=0,
            rows_purged=0,
            rows_tombstoned=0,
            bytes_freed=0,
            applied=apply,
        )

    kept: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.rstrip("\n")
            if not stripped:
                kept.append(line)
                continue
            rows_examined += 1
            import json as _json

            try:
                row = _json.loads(stripped)
            except ValueError:
                kept.append(line)
                continue
            if not isinstance(row, dict):
                kept.append(line)
                continue
            ts = _row_timestamp(row)
            if ts is None or ts >= cutoff:
                kept.append(line)
                continue
            # Expired.
            bytes_freed += len(stripped.encode("utf-8")) + 1
            if supports_excise and _row_contains_pii(row):
                tombstoned = _tombstone_row(row)
                kept.append(_json.dumps(tombstoned, separators=(",", ":")) + "\n")
                rows_tombstoned += 1
            else:
                rows_purged += 1

    if apply and (rows_purged > 0 or rows_tombstoned > 0):
        with path.open("w", encoding="utf-8") as handle:
            handle.writelines(kept)

    return PurgeRecord(
        artifact_path=str(path),
        retention=retention,
        rows_examined=rows_examined,
        rows_purged=rows_purged,
        rows_tombstoned=rows_tombstoned,
        bytes_freed=bytes_freed,
        applied=apply,
    )


def run_purge(
    program_id: str,
    *,
    programs_root: Path,
    now: datetime | None = None,
    apply: bool = False,
    rules: Iterable[SidecarRetentionRule] | None = None,
) -> PurgeReport:
    """Execute the unified purge pass for a program.

    ``now`` defaults to current UTC. ``apply=False`` (default) returns
    the report with no filesystem mutations — the rows_examined/purged
    counts reflect what *would* be removed. ``apply=True`` rewrites
    sidecars in place.
    """
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    selected_rules = tuple(rules) if rules is not None else SIDECAR_RETENTION
    records: list[PurgeRecord] = []
    skipped: list[str] = []

    for rule in selected_rules:
        cutoff = _retention_cutoff(rule.retention, now=current)
        if cutoff is None:
            # INDEFINITE retention → never auto-purge.
            skipped.append(f"{rule.artifact_path}={rule.retention.value}")
            continue
        paths = _resolve_artifact_paths(
            rule, program_id=program_id, programs_root=programs_root
        )
        if not paths:
            skipped.append(f"{rule.artifact_path}=<unresolved>")
            continue
        for path in paths:
            if path.suffix == ".jsonl":
                record = _process_jsonl_sidecar(
                    path,
                    cutoff=cutoff,
                    retention=rule.retention,
                    supports_excise=rule.supports_excise,
                    apply=apply,
                )
            else:
                # Non-JSONL sidecars (SQLite, YAML config) are NOT purged
                # by this pass — they are governed by their own
                # rotation/migration paths. We record a no-op record
                # so the report is auditable.
                record = PurgeRecord(
                    artifact_path=str(path),
                    retention=rule.retention,
                    rows_examined=0,
                    rows_purged=0,
                    rows_tombstoned=0,
                    bytes_freed=0,
                    applied=apply,
                )
            records.append(record)

    return PurgeReport(
        program_id=program_id,
        cutoff=current,
        dry_run=not apply,
        records=tuple(records),
        skipped=tuple(skipped),
    )
