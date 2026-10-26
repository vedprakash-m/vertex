"""Imported pre-history chain baseline (§10.8 companion).

A program's event log may begin with a bulk-ingest/bootstrap segment whose
``prev_event_hash`` links were never sealed consistently — events were chained
against tail hashes that did not end up persisted.  Those events' *payloads*
remain provably authentic because every ``content_hash`` still verifies; only
the linkage is unrecoverable.

Rather than re-sealing the chain (which would rewrite history and cascade new
envelope hashes through every later event, invalidating recorded snapshot head
hashes), a chain baseline declares an explicit boundary: events before the
baseline are *imported pre-history* and are verified by content hash only, while
the baseline event onward is verified as a fully sealed chain.

Zone boundary: Zone A (no ai/m365 imports).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from src.core.config_loader import PROGRAMS_ROOT


CHAIN_BASELINE_FILENAME = ".chain_baseline.json"


@dataclass(frozen=True, slots=True)
class ChainBaseline:
    """Declares where a program's verified hash chain starts."""

    program_id: str
    baseline_event_id: str
    imported_event_count: int
    declared_at: datetime
    actor: str
    reason: str


def get_chain_baseline_path(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    return programs_root / program_id / "ledger" / "events" / CHAIN_BASELINE_FILENAME


def load_chain_baseline(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> ChainBaseline | None:
    path = get_chain_baseline_path(program_id, programs_root=programs_root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"chain baseline at {path} is not valid JSON") from exc
    return _baseline_from_payload(payload)


def _baseline_from_payload(payload: dict[str, Any]) -> ChainBaseline:
    program_id = payload["program_id"]
    baseline_event_id = payload["baseline_event_id"]
    imported_event_count = payload["imported_event_count"]
    declared_at_raw = payload["declared_at"]
    actor = payload["actor"]
    reason = payload["reason"]
    if not isinstance(program_id, str) or not program_id:
        raise ValueError("program_id must be a non-empty string")
    if not isinstance(baseline_event_id, str) or not baseline_event_id:
        raise ValueError("baseline_event_id must be a non-empty string")
    if not isinstance(imported_event_count, int) or imported_event_count < 0:
        raise ValueError("imported_event_count must be a non-negative integer")
    if not isinstance(actor, str) or not actor:
        raise ValueError("actor must be a non-empty string")
    if not isinstance(reason, str) or not reason:
        raise ValueError("reason must be a non-empty string")
    if not isinstance(declared_at_raw, str):
        raise ValueError("declared_at must be a string")
    declared_at = datetime.fromisoformat(declared_at_raw.replace("Z", "+00:00")).astimezone(timezone.utc)
    return ChainBaseline(
        program_id=program_id,
        baseline_event_id=baseline_event_id,
        imported_event_count=imported_event_count,
        declared_at=declared_at,
        actor=actor,
        reason=reason,
    )


def write_chain_baseline(
    baseline: ChainBaseline,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    """Write the baseline declaration. Refuses to silently replace an existing one."""
    path = get_chain_baseline_path(baseline.program_id, programs_root=programs_root)
    if path.exists():
        raise ValueError(
            f"chain baseline already declared for {baseline.program_id} at {path}; "
            "remove it deliberately before declaring a new one"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "program_id": baseline.program_id,
        "baseline_event_id": baseline.baseline_event_id,
        "imported_event_count": baseline.imported_event_count,
        "declared_at": baseline.declared_at.isoformat().replace("+00:00", "Z"),
        "actor": baseline.actor,
        "reason": baseline.reason,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path
