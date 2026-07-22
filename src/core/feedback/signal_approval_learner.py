from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any
from uuid import uuid4

from src.core._db import open_program_db
from src.core.analytics_store import get_program_analytics_store_path
from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.feedback._advisory_yaml import load_advisory_yaml, write_advisory_yaml
from src.core.feedback.trust_profile_store import compute_autonomy_trust_level, humanize_trust_key


SCHEMA_VERSION = "1.0"
DEFAULT_REQUIRED_ACCEPTANCE_RATE = 0.7


@dataclass(frozen=True, slots=True)
class SignalApprovalRuleProposal:
    rule_id: str
    action_type: str
    label: str
    sample_count: int
    accepted_count: int
    acceptance_rate: float
    average_prior_acceptance_rate: float | None
    bootstrap: bool
    recommended_level: str
    recommended_mode: str
    rationale: str
    required_acceptance_rate: float = DEFAULT_REQUIRED_ACCEPTANCE_RATE


@dataclass(frozen=True, slots=True)
class PromotedSignalApprovalRule:
    proposal: SignalApprovalRuleProposal
    promoted_at: datetime
    promoted_by: str


@dataclass(frozen=True, slots=True)
class PausedSignalApprovalArtifacts:
    program_id: str
    action_type: str
    paused_rules: tuple[PromotedSignalApprovalRule, ...]
    path: Path | None


def get_signal_approval_rules_path(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "_feedback" / "signal_approval_rules.yaml"


def build_signal_approval_rule_proposals(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[SignalApprovalRuleProposal, ...]:
    database_path = get_program_analytics_store_path(program_id, programs_root=programs_root)
    if not database_path.exists():
        return ()

    with open_program_db(database_path, read_only=True) as connection:
        try:
            rows = connection.execute(
                """
                SELECT
                    COALESCE(NULLIF(TRIM(action_type), ''), NULLIF(TRIM(policy_rule), ''), 'unknown') AS resolved_action_type,
                    COUNT(*) AS sample_count,
                    SUM(CASE WHEN accepted = 1 THEN 1 ELSE 0 END) AS accepted_count,
                    AVG(prior_acceptance_rate) AS average_prior_acceptance_rate
                FROM autonomy_audit
                GROUP BY resolved_action_type
                ORDER BY resolved_action_type
                """
            ).fetchall()
        except sqlite3.OperationalError:
            return ()

    proposals: list[SignalApprovalRuleProposal] = []
    for row in rows:
        action_type = str(row["resolved_action_type"] or "unknown").strip() or "unknown"
        sample_count = int(row["sample_count"] or 0)
        accepted_count = int(row["accepted_count"] or 0)
        if sample_count <= 0:
            continue

        acceptance_rate = accepted_count / sample_count
        average_prior_acceptance_rate = row["average_prior_acceptance_rate"]
        bootstrap = sample_count < 3
        recommended_level = compute_autonomy_trust_level(
            sample_count=sample_count,
            acceptance_rate=acceptance_rate,
        )
        recommended_mode = _recommended_mode(
            sample_count=sample_count,
            acceptance_rate=acceptance_rate,
        )
        proposals.append(
            SignalApprovalRuleProposal(
                rule_id=f"approval:{action_type}",
                action_type=action_type,
                label=humanize_trust_key(action_type),
                sample_count=sample_count,
                accepted_count=accepted_count,
                acceptance_rate=round(acceptance_rate, 4),
                average_prior_acceptance_rate=(
                    None if average_prior_acceptance_rate is None else round(float(average_prior_acceptance_rate), 4)
                ),
                bootstrap=bootstrap,
                recommended_level=recommended_level,
                recommended_mode=recommended_mode,
                rationale=_build_rationale(
                    sample_count=sample_count,
                    accepted_count=accepted_count,
                    acceptance_rate=acceptance_rate,
                    recommended_mode=recommended_mode,
                    bootstrap=bootstrap,
                ),
            )
        )
    return tuple(proposals)


def refresh_signal_approval_rules(
    program_id: str,
    *,
    as_of: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    dry_run: bool = False,
) -> tuple[tuple[SignalApprovalRuleProposal, ...], Path | None]:
    proposals = build_signal_approval_rule_proposals(program_id, programs_root=programs_root)
    if dry_run:
        return proposals, None

    timestamp = _ensure_utc(as_of or _utc_now())
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": timestamp.isoformat(),
        "proposals": [
            {
                "rule_id": proposal.rule_id,
                "action_type": proposal.action_type,
                "label": proposal.label,
                "sample_count": proposal.sample_count,
                "accepted_count": proposal.accepted_count,
                "acceptance_rate": proposal.acceptance_rate,
                "average_prior_acceptance_rate": proposal.average_prior_acceptance_rate,
                "bootstrap": proposal.bootstrap,
                "recommended_level": proposal.recommended_level,
                "recommended_mode": proposal.recommended_mode,
                "rationale": proposal.rationale,
                "required_acceptance_rate": proposal.required_acceptance_rate,
            }
            for proposal in proposals
        ],
        "rules": [],
    }
    evidence_hash = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    path = write_advisory_yaml(
        get_signal_approval_rules_path(program_id, programs_root=programs_root),
        payload,
        module_name="signal_approval_learner",
        evidence_hash=evidence_hash,
        generation_run_id=str(uuid4()),
        timestamp=timestamp,
    )
    return proposals, path


def load_signal_approval_rule_proposals(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[SignalApprovalRuleProposal, ...]:
    payload = load_advisory_yaml(get_signal_approval_rules_path(program_id, programs_root=programs_root))
    if payload is None:
        return ()
    raw_proposals = payload.get("proposals")
    if not isinstance(raw_proposals, list):
        return ()

    proposals: list[SignalApprovalRuleProposal] = []
    for raw_proposal in raw_proposals:
        if not isinstance(raw_proposal, dict):
            continue
        sample_count = _coerce_int(raw_proposal.get("sample_count"))
        accepted_count = _coerce_int(raw_proposal.get("accepted_count"))
        acceptance_rate = _coerce_float(raw_proposal.get("acceptance_rate"))
        bootstrap = bool(raw_proposal.get("bootstrap"))
        recommended_mode = str(
            raw_proposal.get("recommended_mode")
            or _recommended_mode(sample_count=sample_count, acceptance_rate=acceptance_rate)
        )
        proposals.append(
            SignalApprovalRuleProposal(
                rule_id=str(raw_proposal.get("rule_id") or ""),
                action_type=str(raw_proposal.get("action_type") or "unknown"),
                label=str(
                    raw_proposal.get("label")
                    or humanize_trust_key(str(raw_proposal.get("action_type") or "unknown"))
                ),
                sample_count=sample_count,
                accepted_count=accepted_count,
                acceptance_rate=round(acceptance_rate, 4),
                average_prior_acceptance_rate=_coerce_optional_float(
                    raw_proposal.get("average_prior_acceptance_rate")
                ),
                bootstrap=bootstrap,
                recommended_level=str(
                    raw_proposal.get("recommended_level")
                    or compute_autonomy_trust_level(
                        sample_count=sample_count,
                        acceptance_rate=acceptance_rate,
                    )
                ),
                recommended_mode=recommended_mode,
                rationale=str(
                    raw_proposal.get("rationale")
                    or _build_rationale(
                        sample_count=sample_count,
                        accepted_count=accepted_count,
                        acceptance_rate=acceptance_rate,
                        recommended_mode=recommended_mode,
                        bootstrap=bootstrap,
                    )
                ),
                required_acceptance_rate=round(
                    _coerce_optional_float(raw_proposal.get("required_acceptance_rate"))
                    or DEFAULT_REQUIRED_ACCEPTANCE_RATE,
                    4,
                ),
            )
        )
    return tuple(proposals)


def load_promoted_signal_approval_rules(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[PromotedSignalApprovalRule, ...]:
    payload = load_advisory_yaml(get_signal_approval_rules_path(program_id, programs_root=programs_root))
    if payload is None:
        return ()
    raw_rules = payload.get("rules")
    if not isinstance(raw_rules, list):
        return ()

    promoted_rules: list[PromotedSignalApprovalRule] = []
    for raw_rule in raw_rules:
        if not isinstance(raw_rule, dict):
            continue
        proposal = _proposal_from_mapping(raw_rule)
        raw_promoted_at = raw_rule.get("promoted_at")
        raw_promoted_by = raw_rule.get("promoted_by")
        if not isinstance(raw_promoted_at, str) or not raw_promoted_at.strip():
            continue
        if not isinstance(raw_promoted_by, str) or not raw_promoted_by.strip():
            continue
        promoted_rules.append(
            PromotedSignalApprovalRule(
                proposal=proposal,
                promoted_at=_parse_datetime(raw_promoted_at),
                promoted_by=raw_promoted_by.strip(),
            )
        )
    return tuple(promoted_rules)


def promote_signal_approval_rule(
    program_id: str,
    *,
    rule_id: str,
    promoted_by: str,
    as_of: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    dry_run: bool = False,
) -> tuple[PromotedSignalApprovalRule, Path | None]:
    payload = load_advisory_yaml(get_signal_approval_rules_path(program_id, programs_root=programs_root))
    if payload is None:
        raise ValueError(f"No signal approval rules available for program '{program_id}'.")

    raw_proposals = payload.get("proposals")
    if not isinstance(raw_proposals, list):
        raise ValueError(f"Signal approval rules for program '{program_id}' are missing a proposals list.")
    raw_rules = payload.get("rules")
    if raw_rules is None:
        raw_rules = []
    if not isinstance(raw_rules, list):
        raise ValueError(f"Signal approval rules for program '{program_id}' are missing a rules list.")

    proposal_mapping = next(
        (entry for entry in raw_proposals if isinstance(entry, dict) and str(entry.get("rule_id") or "") == rule_id),
        None,
    )
    if proposal_mapping is None:
        raise ValueError(f"Unknown signal approval rule '{rule_id}' for program '{program_id}'.")
    if any(isinstance(entry, dict) and str(entry.get("rule_id") or "") == rule_id for entry in raw_rules):
        raise ValueError(f"Signal approval rule '{rule_id}' is already promoted for program '{program_id}'.")

    proposal = _proposal_from_mapping(proposal_mapping)
    timestamp = _ensure_utc(as_of or _utc_now())
    promoted_rule = PromotedSignalApprovalRule(
        proposal=proposal,
        promoted_at=timestamp,
        promoted_by=promoted_by.strip(),
    )
    if dry_run:
        return promoted_rule, None

    updated_payload = dict(payload)
    updated_rules = list(raw_rules)
    updated_rules.append(
        {
            **_proposal_to_mapping(proposal),
            "promoted_at": timestamp.isoformat(),
            "promoted_by": promoted_rule.promoted_by,
        }
    )
    updated_payload["rules"] = updated_rules
    updated_payload.setdefault("schema_version", SCHEMA_VERSION)
    updated_payload["updated_at"] = timestamp.isoformat()
    evidence_hash = hashlib.sha256(json.dumps(updated_payload, sort_keys=True).encode("utf-8")).hexdigest()
    path = write_advisory_yaml(
        get_signal_approval_rules_path(program_id, programs_root=programs_root),
        updated_payload,
        module_name="policy_command",
        evidence_hash=evidence_hash,
        generation_run_id=str(uuid4()),
        timestamp=timestamp,
    )
    return promoted_rule, path


def pause_signal_approval_rules(
    program_id: str,
    *,
    action_type: str,
    as_of: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
    dry_run: bool = False,
) -> PausedSignalApprovalArtifacts:
    normalized_action_type = action_type.strip().lower()
    if not normalized_action_type:
        raise ValueError("Action type is required.")

    payload = load_advisory_yaml(get_signal_approval_rules_path(program_id, programs_root=programs_root))
    if payload is None:
        raise ValueError(f"No signal approval rules available for program '{program_id}'.")

    raw_rules = payload.get("rules")
    if raw_rules is None:
        raw_rules = []
    if not isinstance(raw_rules, list):
        raise ValueError(f"Signal approval rules for program '{program_id}' are missing a rules list.")

    paused_rules = tuple(
        rule
        for rule in load_promoted_signal_approval_rules(program_id, programs_root=programs_root)
        if rule.proposal.action_type.strip().lower() == normalized_action_type
        and rule.proposal.recommended_mode.strip().lower() == "batch_approval"
    )
    if not paused_rules:
        raise ValueError(
            f"No promoted batch approval rule for action type '{normalized_action_type}' in program '{program_id}'."
        )

    if dry_run:
        return PausedSignalApprovalArtifacts(
            program_id=program_id,
            action_type=normalized_action_type,
            paused_rules=paused_rules,
            path=None,
        )

    paused_rule_ids = {rule.proposal.rule_id for rule in paused_rules}
    updated_payload = dict(payload)
    updated_payload["rules"] = [
        entry
        for entry in raw_rules
        if not isinstance(entry, dict) or str(entry.get("rule_id") or "") not in paused_rule_ids
    ]
    timestamp = _ensure_utc(as_of or _utc_now())
    updated_payload.setdefault("schema_version", SCHEMA_VERSION)
    updated_payload["updated_at"] = timestamp.isoformat()
    evidence_hash = hashlib.sha256(json.dumps(updated_payload, sort_keys=True).encode("utf-8")).hexdigest()
    path = write_advisory_yaml(
        get_signal_approval_rules_path(program_id, programs_root=programs_root),
        updated_payload,
        module_name="audit_pause_command",
        evidence_hash=evidence_hash,
        generation_run_id=str(uuid4()),
        timestamp=timestamp,
    )
    return PausedSignalApprovalArtifacts(
        program_id=program_id,
        action_type=normalized_action_type,
        paused_rules=paused_rules,
        path=path,
    )


def _recommended_mode(*, sample_count: int, acceptance_rate: float) -> str:
    if sample_count < 3:
        return "bootstrap"
    if sample_count >= 10 and acceptance_rate >= 0.9:
        return "batch_approval"
    if acceptance_rate >= DEFAULT_REQUIRED_ACCEPTANCE_RATE:
        return "proposal_staging"
    return "manual_review"


def _build_rationale(
    *,
    sample_count: int,
    accepted_count: int,
    acceptance_rate: float,
    recommended_mode: str,
    bootstrap: bool,
) -> str:
    percent = round(acceptance_rate * 100)
    if bootstrap:
        return f"Bootstrap mode: {accepted_count}/{sample_count} approvals collected; continue staged approvals until 3 samples."
    if recommended_mode == "batch_approval":
        return f"Eligible for batch approval: {accepted_count}/{sample_count} accepted ({percent}%)."
    if recommended_mode == "proposal_staging":
        return f"Keep per-proposal approval: {accepted_count}/{sample_count} accepted ({percent}%) meets the trust gate but not batch promotion."
    return f"Manual review required: {accepted_count}/{sample_count} accepted ({percent}%) is below the trust gate."


def _coerce_int(value: object) -> int:
    try:
        if isinstance(value, (int, float, str)):
            return int(value)
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _coerce_float(value: object) -> float:
    try:
        if isinstance(value, (int, float, str)):
            return float(value)
        return float(str(value))
    except (TypeError, ValueError):
        return 0.0


def _coerce_optional_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float, str)):
            return float(value)
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _proposal_from_mapping(raw_proposal: dict[str, Any]) -> SignalApprovalRuleProposal:
    sample_count = _coerce_int(raw_proposal.get("sample_count"))
    accepted_count = _coerce_int(raw_proposal.get("accepted_count"))
    acceptance_rate = _coerce_float(raw_proposal.get("acceptance_rate"))
    bootstrap = bool(raw_proposal.get("bootstrap"))
    recommended_mode = str(
        raw_proposal.get("recommended_mode")
        or _recommended_mode(sample_count=sample_count, acceptance_rate=acceptance_rate)
    )
    return SignalApprovalRuleProposal(
        rule_id=str(raw_proposal.get("rule_id") or ""),
        action_type=str(raw_proposal.get("action_type") or "unknown"),
        label=str(raw_proposal.get("label") or humanize_trust_key(str(raw_proposal.get("action_type") or "unknown"))),
        sample_count=sample_count,
        accepted_count=accepted_count,
        acceptance_rate=round(acceptance_rate, 4),
        average_prior_acceptance_rate=_coerce_optional_float(raw_proposal.get("average_prior_acceptance_rate")),
        bootstrap=bootstrap,
        recommended_level=str(
            raw_proposal.get("recommended_level")
            or compute_autonomy_trust_level(
                sample_count=sample_count,
                acceptance_rate=acceptance_rate,
            )
        ),
        recommended_mode=recommended_mode,
        rationale=str(
            raw_proposal.get("rationale")
            or _build_rationale(
                sample_count=sample_count,
                accepted_count=accepted_count,
                acceptance_rate=acceptance_rate,
                recommended_mode=recommended_mode,
                bootstrap=bootstrap,
            )
        ),
        required_acceptance_rate=round(
            _coerce_optional_float(raw_proposal.get("required_acceptance_rate"))
            or DEFAULT_REQUIRED_ACCEPTANCE_RATE,
            4,
        ),
    )


def _proposal_to_mapping(proposal: SignalApprovalRuleProposal) -> dict[str, Any]:
    return {
        "rule_id": proposal.rule_id,
        "action_type": proposal.action_type,
        "label": proposal.label,
        "sample_count": proposal.sample_count,
        "accepted_count": proposal.accepted_count,
        "acceptance_rate": proposal.acceptance_rate,
        "average_prior_acceptance_rate": proposal.average_prior_acceptance_rate,
        "bootstrap": proposal.bootstrap,
        "recommended_level": proposal.recommended_level,
        "recommended_mode": proposal.recommended_mode,
        "rationale": proposal.rationale,
        "required_acceptance_rate": proposal.required_acceptance_rate,
    }


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return _ensure_utc(parsed)