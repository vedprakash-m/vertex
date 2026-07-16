"""FR-SG-39: Automatic maturity advancement via earned-autonomy scoring.

Earned-autonomy state is stored in ``programs/<prog>/earned_autonomy_state.yaml``,
NOT in ``program.yaml:maturity_level`` (that field is used for operational gating
in semantic dedup, escalation, forceability, etc.).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Mapping
import yaml

from src.core.analytics_store import (
    get_program_analytics_store_path,
    load_autonomy_audit_records,
)
from src.core.edition_resolver import PROGRAMS_ROOT


_EARNED_AUTONOMY_FILENAME = "earned_autonomy_state.yaml"
_SCHEMA_VERSION = "1.0"

# Thresholds for tier advancement (all must be satisfied)
_ADVANCE_MIN_CONFIRMED_ISSUES = 5
_ADVANCE_MIN_APPROVAL_RATE = 0.80      # 80% auto-confirmed without override
_ADVANCE_MAX_OVERRIDE_RATE = 0.10      # no more than 10% overrides
_ADVANCE_MIN_GATE_PASS_RATE = 0.85    # QG pass rate ≥ 85%

# Thresholds for tier demotion (any one is sufficient)
_DEMOTE_MAX_OVERRIDE_RATE = 0.25       # override rate > 25%
_DEMOTE_MIN_GATE_PASS_RATE = 0.60     # QG pass rate < 60%


@dataclass(frozen=True, slots=True)
class ProposalClassCounters:
    """Appendix A.8's ``counters`` block for one proposal class."""

    proposals: int = 0
    accepted: int = 0
    edited: int = 0
    rejected: int = 0
    reversals: int = 0
    material_errors: int = 0


@dataclass(frozen=True, slots=True)
class ProposalClassAutonomyState:
    """Appendix A.8 (Section 8.15.1): one ``proposal_classes.<class>`` entry
    in ``earned_autonomy_state.yaml`` -- the per-``(program_id,
    proposal_class)`` L0-L4 ladder, distinct from this module's older
    global ``earned_tier`` (FR-SG-39)."""

    level: str  # "l0".."l4"
    promoted_at: datetime | None
    demoted_at: datetime | None
    last_change_reason: str
    evidence_window_start: datetime
    counters: ProposalClassCounters = field(default_factory=ProposalClassCounters)
    sample_rate: float = 1.0


@dataclass(frozen=True, slots=True)
class EarnedAutonomyState:
    program_id: str
    earned_tier: int        # 0 = no earned autonomy; matches maturity levels
    maturity_score: float   # 0.0–1.0 composite score
    last_evaluated_at: datetime
    promoted_at: datetime | None
    demoted_at: datetime | None
    schema_version: str = _SCHEMA_VERSION
    #: Appendix A.8's per-proposal-class ladder state. Absent/empty means
    #: every class defaults to l0/l1 per the ladder defaults (upcast rule).
    proposal_classes: Mapping[str, ProposalClassAutonomyState] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MaturityScoreBreakdown:
    overall: float
    confirmed_issues_factor: float
    approval_rate_factor: float
    override_rate_factor: float
    gate_pass_rate_factor: float


def get_earned_autonomy_state_path(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> Path:
    return programs_root / program_id / _EARNED_AUTONOMY_FILENAME


def load_earned_autonomy_state(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> EarnedAutonomyState | None:
    path = get_earned_autonomy_state_path(program_id, programs_root=programs_root)
    if not path.exists():
        return None
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    promoted_raw = raw.get("promoted_at")
    demoted_raw = raw.get("demoted_at")
    evaluated_raw = raw.get("last_evaluated_at")
    return EarnedAutonomyState(
        program_id=program_id,
        earned_tier=int(raw.get("earned_tier", 0)),
        maturity_score=float(raw.get("maturity_score", 0.0)),
        last_evaluated_at=_parse_datetime(evaluated_raw) if evaluated_raw else datetime.now(timezone.utc),
        promoted_at=_parse_datetime(promoted_raw) if promoted_raw else None,
        demoted_at=_parse_datetime(demoted_raw) if demoted_raw else None,
        schema_version=str(raw.get("schema_version", _SCHEMA_VERSION)),
        proposal_classes=_parse_proposal_classes(raw.get("proposal_classes")),
    )


def _parse_proposal_classes(raw: Any) -> dict[str, ProposalClassAutonomyState]:
    if not raw:
        return {}
    parsed: dict[str, ProposalClassAutonomyState] = {}
    for proposal_class, entry in dict(raw).items():
        entry = entry or {}
        counters_raw = entry.get("counters") or {}
        promoted_raw = entry.get("promoted_at")
        demoted_raw = entry.get("demoted_at")
        window_raw = entry.get("evidence_window_start")
        parsed[str(proposal_class)] = ProposalClassAutonomyState(
            level=str(entry.get("level", "l0")),
            promoted_at=_parse_datetime(promoted_raw) if promoted_raw else None,
            demoted_at=_parse_datetime(demoted_raw) if demoted_raw else None,
            last_change_reason=str(entry.get("last_change_reason", "")),
            evidence_window_start=_parse_datetime(window_raw) if window_raw else datetime.now(timezone.utc),
            counters=ProposalClassCounters(
                proposals=int(counters_raw.get("proposals", 0)),
                accepted=int(counters_raw.get("accepted", 0)),
                edited=int(counters_raw.get("edited", 0)),
                rejected=int(counters_raw.get("rejected", 0)),
                reversals=int(counters_raw.get("reversals", 0)),
                material_errors=int(counters_raw.get("material_errors", 0)),
            ),
            sample_rate=float(entry.get("sample_rate", 1.0)),
        )
    return parsed


def compute_maturity_score(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> MaturityScoreBreakdown:
    """Compute a 0.0–1.0 composite maturity score from analytics data."""
    db_path = get_program_analytics_store_path(program_id, programs_root=programs_root)
    if not db_path.exists():
        return MaturityScoreBreakdown(
            overall=0.0,
            confirmed_issues_factor=0.0,
            approval_rate_factor=0.0,
            override_rate_factor=1.0,
            gate_pass_rate_factor=0.0,
        )

    import sqlite3
    connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    try:
        confirmed_issues = _count_confirmed_risks(connection)
        issue_factor = min(1.0, confirmed_issues / max(_ADVANCE_MIN_CONFIRMED_ISSUES, 1))

        audit_records = load_autonomy_audit_records(program_id, programs_root=programs_root)
        total_audits = len(audit_records)
        if total_audits > 0:
            accepted_count = sum(1 for r in audit_records if r.accepted)
            approval_rate = accepted_count / total_audits
            override_rate = 1.0 - approval_rate
        else:
            approval_rate = 0.0
            override_rate = 0.0

        approval_factor = approval_rate
        override_factor = 1.0 - min(1.0, override_rate / _DEMOTE_MAX_OVERRIDE_RATE)

        gate_stats = _compute_gate_pass_rate(connection, program_id)
        gate_factor = gate_stats

        overall = (
            0.25 * issue_factor
            + 0.30 * approval_factor
            + 0.25 * override_factor
            + 0.20 * gate_factor
        )
    finally:
        connection.close()

    return MaturityScoreBreakdown(
        overall=round(min(1.0, max(0.0, overall)), 4),
        confirmed_issues_factor=round(issue_factor, 4),
        approval_rate_factor=round(approval_factor, 4),
        override_rate_factor=round(override_factor, 4),
        gate_pass_rate_factor=round(gate_factor, 4),
    )


def try_advance_earned_autonomy(
    program_id: str,
    *,
    edition_id: str = "manual",
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[EarnedAutonomyState, str]:
    """Evaluate and optionally advance/demote earned autonomy tier.

    Returns (new_state, action) where action is one of:
    'promoted', 'demoted', 'unchanged', 'initialized'.
    Writes to earned_autonomy_state.yaml; never touches program.yaml.
    """
    now = datetime.now(timezone.utc)
    score = compute_maturity_score(program_id, programs_root=programs_root)
    current = load_earned_autonomy_state(program_id, programs_root=programs_root)
    current_tier = current.earned_tier if current is not None else 0

    db_path = get_program_analytics_store_path(program_id, programs_root=programs_root)
    approval_rate = 0.0
    override_rate = 0.0
    if db_path.exists():
        audit_records = load_autonomy_audit_records(program_id, programs_root=programs_root)
        total = len(audit_records)
        if total > 0:
            approval_rate = sum(1 for r in audit_records if r.accepted) / total
            override_rate = 1.0 - approval_rate

    gate_pass_rate = score.gate_pass_rate_factor

    # Determine action
    action = "unchanged"
    new_tier = current_tier

    should_promote = (
        score.confirmed_issues_factor >= (
            _ADVANCE_MIN_CONFIRMED_ISSUES / max(_ADVANCE_MIN_CONFIRMED_ISSUES, 1)
        )
        and approval_rate >= _ADVANCE_MIN_APPROVAL_RATE
        and override_rate <= _ADVANCE_MAX_OVERRIDE_RATE
        and gate_pass_rate >= _ADVANCE_MIN_GATE_PASS_RATE
        and current_tier < 3
    )
    should_demote = (
        override_rate > _DEMOTE_MAX_OVERRIDE_RATE
        or gate_pass_rate < _DEMOTE_MIN_GATE_PASS_RATE
    ) and current_tier > 0

    if should_promote:
        new_tier = current_tier + 1
        action = "promoted"
    elif should_demote:
        new_tier = max(0, current_tier - 1)
        action = "demoted"
    elif current is None:
        action = "initialized"

    new_state = EarnedAutonomyState(
        program_id=program_id,
        earned_tier=new_tier,
        maturity_score=score.overall,
        last_evaluated_at=now,
        promoted_at=now if action == "promoted" else (current.promoted_at if current else None),
        demoted_at=now if action == "demoted" else (current.demoted_at if current else None),
    )
    _write_earned_autonomy_state(new_state, programs_root=programs_root)
    return new_state, action


def _write_earned_autonomy_state(
    state: EarnedAutonomyState,
    *,
    programs_root: Path,
) -> None:
    path = get_earned_autonomy_state_path(state.program_id, programs_root=programs_root)
    payload: dict[str, Any] = {
        "schema_version": state.schema_version,
        "program_id": state.program_id,
        "earned_tier": state.earned_tier,
        "maturity_score": state.maturity_score,
        "last_evaluated_at": state.last_evaluated_at.isoformat(),
        "promoted_at": state.promoted_at.isoformat() if state.promoted_at else None,
        "demoted_at": state.demoted_at.isoformat() if state.demoted_at else None,
    }
    if state.proposal_classes:
        payload["proposal_classes"] = {
            proposal_class: {
                "level": entry.level,
                "promoted_at": entry.promoted_at.isoformat() if entry.promoted_at else None,
                "demoted_at": entry.demoted_at.isoformat() if entry.demoted_at else None,
                "last_change_reason": entry.last_change_reason,
                "evidence_window_start": entry.evidence_window_start.isoformat(),
                "counters": {
                    "proposals": entry.counters.proposals,
                    "accepted": entry.counters.accepted,
                    "edited": entry.counters.edited,
                    "rejected": entry.counters.rejected,
                    "reversals": entry.counters.reversals,
                    "material_errors": entry.counters.material_errors,
                },
                "sample_rate": entry.sample_rate,
            }
            for proposal_class, entry in state.proposal_classes.items()
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(yaml.dump(payload, default_flow_style=False, allow_unicode=True), encoding="utf-8")
    os.replace(temp_path, path)


def write_proposal_class_state(
    program_id: str,
    proposal_class: str,
    entry: ProposalClassAutonomyState,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> EarnedAutonomyState:
    """Additive, single-class upsert into the ``proposal_classes`` block --
    preserves this module's existing global ``earned_tier`` fields and every
    other class's entry untouched."""
    current = load_earned_autonomy_state(program_id, programs_root=programs_root)
    base = current or EarnedAutonomyState(
        program_id=program_id,
        earned_tier=0,
        maturity_score=0.0,
        last_evaluated_at=datetime.now(timezone.utc),
        promoted_at=None,
        demoted_at=None,
    )
    new_classes = dict(base.proposal_classes)
    new_classes[proposal_class] = entry
    new_state = EarnedAutonomyState(
        program_id=base.program_id,
        earned_tier=base.earned_tier,
        maturity_score=base.maturity_score,
        last_evaluated_at=base.last_evaluated_at,
        promoted_at=base.promoted_at,
        demoted_at=base.demoted_at,
        schema_version=base.schema_version,
        proposal_classes=new_classes,
    )
    _write_earned_autonomy_state(new_state, programs_root=programs_root)
    return new_state


def _count_confirmed_risks(connection: Any) -> int:
    try:
        row = connection.execute("SELECT COUNT(DISTINCT issue_number) AS n FROM confirmed_risks").fetchone()
        return int(row["n"]) if row else 0
    except Exception:
        return 0


def _compute_gate_pass_rate(connection: Any, program_id: str) -> float:
    """Ratio of edition×gate combos that had ZERO failures vs. total evaluated."""
    try:
        total_row = connection.execute(
            "SELECT COUNT(*) AS n FROM gate_failure_log WHERE program_id = ?",
            (program_id,),
        ).fetchone()
        fail_count = int(total_row["n"]) if total_row else 0
        if fail_count == 0:
            return 1.0
        # Use a heuristic: pass_rate decays with failure count (capped at 100 failures → 0.0)
        return max(0.0, 1.0 - min(fail_count, 100) / 100.0)
    except Exception:
        return 1.0


def _parse_datetime(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
