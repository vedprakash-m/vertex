from __future__ import annotations

import json
import os
import portalocker
from src.core.jsonl_utils import parse_jsonl_line
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.core.models_v2 import ReviewPolicy, Signal, SignalReviewDecision
from src.core.config_loader import PROGRAMS_ROOT


@dataclass(frozen=True, slots=True)
class ReviewPolicyAuditEntry:
    signal_id: str
    new_policy: ReviewPolicy
    recorded_at: datetime
    source: str


def default_review_policy(signal: Signal) -> ReviewPolicy:
    if signal.review_policy is not None:
        return signal.review_policy
    return ReviewPolicy.PENDING


def signal_can_be_auto_approved(signal: Signal) -> bool:
    if signal.source.startswith("ado/") or signal.source in {"manual", "icm", "vertex/freshness"}:
        return True
    if signal.source.startswith("plane1/"):  # §22 E3: Plane 1 authored config changes
        return True
    if signal.source in {"kusto", "kusto_kpi"}:
        metadata = signal.metadata or {}
        return bool(metadata.get("validated", False))
    return False


def effective_review_policy(signal: Signal) -> ReviewPolicy:
    return default_review_policy(signal)


def signal_is_approved_for_evidence(
    signal: Signal,
    review_states: dict[str, SignalReviewDecision],
) -> bool:
    decision = review_states.get(signal.id)
    if decision is not None:
        return decision.decision == "approved"
    effective = effective_review_policy(signal)
    # AUTO_APPROVED (§22 E3): Plane 1 authored changes are operator-sourced and deterministic
    return effective in (ReviewPolicy.APPROVED, ReviewPolicy.AUTO_APPROVED)


def signal_needs_review(
    signal: Signal,
    review_states: dict[str, SignalReviewDecision],
) -> bool:
    decision = review_states.get(signal.id)
    if decision is not None:
        return decision.decision == "deferred"
    return effective_review_policy(signal) == ReviewPolicy.PENDING


# ── FR-SG-38: approval auto-enforcement ─────────────────────────────────────


def compute_auto_approval_policies(
    signals: tuple[Signal, ...] | list[Signal],
    review_states: dict[str, SignalReviewDecision],
    *,
    floor_rate: float = 0.2,
    ceiling_rate: float = 0.8,
    min_sample: int = 10,
) -> dict[str, ReviewPolicy]:
    """Return recommended ReviewPolicy overrides for signals that exceed auto-approval thresholds.

    For each source type with ≥min_sample reviewed signals:
    - If approval_rate ≥ ceiling_rate: PENDING signals → AUTO_APPROVED recommendation
    - If approval_rate ≤ floor_rate:   AUTO_APPROVED signals → PENDING recommendation
    Returns a dict of signal_id → recommended ReviewPolicy (only for signals that should change).
    """
    from collections import defaultdict

    approvals_by_source: dict[str, int] = defaultdict(int)
    total_by_source: dict[str, int] = defaultdict(int)
    for sig in signals:
        decision = review_states.get(sig.id)
        if decision is not None:
            total_by_source[sig.source] += 1
            if decision.decision == "approved":
                approvals_by_source[sig.source] += 1

    recommendations: dict[str, ReviewPolicy] = {}
    for sig in signals:
        source = sig.source
        total = total_by_source.get(source, 0)
        if total < min_sample:
            continue
        rate = approvals_by_source.get(source, 0) / total
        current = effective_review_policy(sig)
        if rate >= ceiling_rate and current == ReviewPolicy.PENDING:
            recommendations[sig.id] = ReviewPolicy.AUTO_APPROVED
        elif rate <= floor_rate and current == ReviewPolicy.AUTO_APPROVED:
            recommendations[sig.id] = ReviewPolicy.PENDING
    return recommendations


def write_autonomy_audit_entries(
    program_id: str,
    policy_changes: dict[str, ReviewPolicy],
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> None:
    """Append policy change events to programs/<prog>/journal/review_policy_audit.jsonl (FR-SG-38)."""
    if not policy_changes:
        return
    path = get_review_policy_audit_path(program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    with path.open("a", encoding="utf-8") as fh:
        portalocker.lock(fh, portalocker.LOCK_EX)
        try:
            for signal_id, new_policy in policy_changes.items():
                record = {
                    "signal_id": signal_id,
                    "new_policy": new_policy.value,
                    "recorded_at": now,
                    "source": "auto_enforcement",
                }
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        finally:
            portalocker.unlock(fh)


def get_review_policy_audit_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "journal" / "review_policy_audit.jsonl"


def load_review_policy_audit_entries(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[ReviewPolicyAuditEntry, ...]:
    path = get_review_policy_audit_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()

    entries: list[ReviewPolicyAuditEntry] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        payload = parse_jsonl_line(raw_line)
        entries.append(
            ReviewPolicyAuditEntry(
                signal_id=str(payload["signal_id"]),
                new_policy=ReviewPolicy(str(payload["new_policy"])),
                recorded_at=_parse_recorded_at(payload["recorded_at"]),
                source=str(payload.get("source") or "auto_enforcement"),
            )
        )
    return tuple(entries)


def _parse_recorded_at(value: object) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)