"""RELO-W4.12: deterministic, read-only exception queue for the cockpit.

Ranks existing signals only; computes nothing new and writes nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from src.core.actuation_outbox import outbox_db_path
from src.core._db import open_program_db
from src.core.action_tracker import assess_action_staleness
from src.core.claim_tracker import load_open_claims, load_open_decision_asks
from src.core.context_gap_store import load_context_gaps, rank_context_gaps
from src.core.governed_feedback_lifecycle import (
    ProposalStatus,
    get_feedback_proposals_path,
    load_feedback_proposals,
)
from src.core.incident_journal_store import read_incident_entries
from src.core.models_v2 import ActionItem, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.reality_store import get_program_reality_db_path
from src.core.risk_register_engine import assess_risk_staleness

_ACTIVE_RISK_STATUSES = {RiskStatus.OPEN, RiskStatus.ESCALATED}


@dataclass(frozen=True, slots=True)
class ExceptionItem:
    kind: str
    subject_id: str
    severity_rank: int
    age_days: int
    reason: str
    evidence: str


@dataclass(frozen=True, slots=True)
class DecoratedException:
    kind: str
    subject_id: str
    severity_rank: int
    age_days: int
    reason: str
    evidence: str
    fingerprint: str
    authority_source: str
    authority_status: str
    diff_preview: str
    expected_effect: str
    mutation_scope: str
    remediation_command: str | None
    action_guidance: str


@dataclass(frozen=True, slots=True)
class DispatchException:
    outbox_id: str
    status: str
    age_days: int
    reason: str
    evidence: str


def build_exception_queue(
    risks: tuple[RiskEntry, ...],
    actions: tuple[ActionItem, ...],
    as_of: date,
    *,
    dispatch_exceptions: tuple[DispatchException, ...] = (),
    extended_exceptions: tuple[ExceptionItem, ...] = (),
) -> tuple[ExceptionItem, ...]:
    """Rank: severity 0 (needs_input/dispatch/blocked_draft) > 1 (action/gap/binding) > 2 (stale); ties by oldest, then id."""
    items: list[ExceptionItem] = []
    for risk in risks:
        review_date = risk.last_reviewed_date or risk.identified_date
        age = (as_of - review_date).days
        evidence = f"risk_register.yaml#{risk.id}"
        if risk.status in _ACTIVE_RISK_STATUSES and (
            risk.probability is RiskProbability.UNASSESSED or risk.impact is RiskImpact.UNASSESSED
        ):
            items.append(ExceptionItem("needs_input", risk.id, 0, age, "Risk level not assessed (Needs Input).", evidence))
        elif assess_risk_staleness(risk, as_of):
            reason = (
                f"Risk last reviewed {risk.last_reviewed_date.isoformat()}."
                if risk.last_reviewed_date is not None
                else f"Risk identified {risk.identified_date.isoformat()}; review date unavailable."
            )
            items.append(ExceptionItem(
                "stale_risk", risk.id, 2, age, reason, evidence
            ))
    for action in assess_action_staleness(actions, as_of):
        assert action.due_date is not None
        items.append(ExceptionItem(
            "overdue_action", action.id, 1, (as_of - action.due_date).days,
            f"Action due {action.due_date.isoformat()} is overdue.", f"actions.jsonl#{action.id}",
        ))
    for dispatch in dispatch_exceptions:
        items.append(ExceptionItem(
            "partial_dispatch" if dispatch.status == "partial" else "uncertain_dispatch",
            dispatch.outbox_id,
            0,
            dispatch.age_days,
            dispatch.reason,
            dispatch.evidence,
        ))
    items.extend(extended_exceptions)
    return tuple(sorted(items, key=lambda item: (item.severity_rank, -item.age_days, item.subject_id)))


_SAFE_CLI_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")
_AUTHORITY = {
    "needs_input": ("risk register", "available, primary-judgment divergence possible"),
    "stale_risk": ("risk register", "available, primary-judgment divergence possible"),
    "overdue_action": ("action journal", "available, retention-limited"),
    "partial_dispatch": ("actuation outbox", "available, provider state uncertain"),
    "uncertain_dispatch": ("actuation outbox", "available, provider state uncertain"),
    "blocked_draft": ("publication manifest", "available, current draft scope"),
    "approval_expiry": ("actuation approval", "available, expiry observed"),
    "runtime_incident": ("incident journal", "available, recent 30-day window"),
    "knowledge_proposal": ("feedback proposal journal", "available, lifecycle status"),
    "source_gap": ("context gap journal", "available, signal completeness unknown"),
    "unvalidated_binding": ("reality metric binding", "available, validation absent"),
    "open_claim": ("claim journal", "available, read-only"),
    "open_decision_ask": ("claim journal", "available, read-only"),
}


def decorate_exception_queue(
    items: tuple[ExceptionItem, ...],
    *,
    program_id: str,
    as_of: date,
    extended: bool,
) -> tuple[DecoratedException, ...]:
    decorated: list[DecoratedException] = []
    for item in items:
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "kind": item.kind,
                    "subject_id": item.subject_id,
                    "reason": item.reason,
                    "evidence": item.evidence,
                    "age_days": item.age_days,
                    "as_of": as_of.isoformat(),
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        source, status = _AUTHORITY.get(
            item.kind, ("unclassified exception source", "availability unknown")
        )
        safe = (
            bool(_SAFE_CLI_TOKEN.fullmatch(program_id))
            and bool(_SAFE_CLI_TOKEN.fullmatch(item.kind))
            and bool(_SAFE_CLI_TOKEN.fullmatch(item.subject_id))
        )
        command = (
            f"vertex cockpit exception --program {program_id} --kind {item.kind} "
            f"--subject {item.subject_id} --as-of {as_of.isoformat()} "
            f"--expect-fingerprint {fingerprint}"
            + (" --extended" if extended else "")
        ) if safe else None
        decorated.append(DecoratedException(
            kind=item.kind,
            subject_id=item.subject_id,
            severity_rank=item.severity_rank,
            age_days=item.age_days,
            reason=item.reason,
            evidence=item.evidence,
            fingerprint=fingerprint,
            authority_source=source,
            authority_status=status,
            diff_preview=(
                f"Observed {item.kind} for {item.subject_id}: {item.reason} "
                f"Evidence: {item.evidence}."
            ),
            expected_effect=(
                "Re-read and revalidate the same current exception without changing "
                "state, dispatching, or selecting a remediation."
            ),
            mutation_scope="none",
            remediation_command=command,
            action_guidance=(
                "Run the exact read-only command and compare its fingerprint before "
                "opening the owning specialized workflow."
                if command is not None
                else "Manual inspection required: this subject cannot be copied safely "
                "into a CLI token; inspect the evidence reference directly."
            ),
        ))
    return tuple(decorated)


def load_dispatch_exceptions(
    program_id: str,
    *,
    programs_root: Path,
    as_of: date,
) -> tuple[DispatchException, ...]:
    db_path = outbox_db_path(program_id, programs_root=programs_root)
    if not db_path.exists():
        return ()
    with open_program_db(db_path, read_only=True, immutable=_zero_write_immutable(db_path)) as connection:
        has_table = connection.execute(
            "SELECT 1 FROM sqlite_master "
            "WHERE type = 'table' AND name = 'outbox_reconciliation_receipts'"
        ).fetchone()
        if has_table is None:
            rows = connection.execute(
                "SELECT outbox_id, status, updated_at, NULL "
                "FROM outbox "
                "WHERE status IN ('uncertain_remote_state', 'compensation_required') "
                "ORDER BY updated_at DESC, outbox_id"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT o.outbox_id, o.status, o.updated_at, r.receipt_json "
                "FROM outbox AS o "
                "LEFT JOIN outbox_reconciliation_receipts AS r ON r.outbox_id = o.outbox_id "
                "WHERE o.status IN ('uncertain_remote_state', 'compensation_required') "
                "ORDER BY o.updated_at DESC, o.outbox_id"
            ).fetchall()

    exceptions: list[DispatchException] = []
    for outbox_id, db_status, updated_at_text, receipt_json in rows:
        if receipt_json is None:
            receipts: list[object] = []
            status = str(db_status)
        else:
            document = json.loads(str(receipt_json))
            if not isinstance(document, dict):
                raise ValueError("Stored actuation reconciliation receipt is not an object")
            parsed_receipts = document.get("receipts")
            if not isinstance(parsed_receipts, list):
                raise ValueError("Stored actuation reconciliation receipt is missing receipts")
            receipts = parsed_receipts
            status = str(document.get("status") or db_status)
        completed = sum(
            1
            for receipt in receipts
            if isinstance(receipt, dict) and receipt.get("terminal_status") == "completed"
        )
        required = max(1, len(receipts))
        updated_at = datetime.fromisoformat(str(updated_at_text).replace("Z", "+00:00"))
        if updated_at.utcoffset() is None:
            raise ValueError("Stored actuation reconciliation timestamp is not timezone-aware")
        age_days = max(0, (as_of - updated_at.astimezone(timezone.utc).date()).days)
        exceptions.append(DispatchException(
            outbox_id=str(outbox_id),
            status=status,
            age_days=age_days,
            reason=(
                f"{completed} of {required} required recipient effects are terminal; "
                f"workflow remains {status}."
            ),
            evidence=f"actuation/outbox.db#{outbox_id}",
        ))
    return tuple(exceptions)


def _confirmed_issue_keys(program_id: str, *, programs_root: Path) -> set[tuple[str, int]]:
    confirmed: set[tuple[str, int]] = set()
    for base in (programs_root / program_id / "archive", programs_root.parent / "archive" / program_id):
        if not base.is_dir():
            continue
        for index_path in sorted(base.glob("*/index.json")):
            edition = index_path.parent.name
            try:
                payload = json.loads(index_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            raw_issues = payload.get("issues") if isinstance(payload, dict) else None
            if not isinstance(raw_issues, list):
                continue
            for entry in raw_issues:
                if isinstance(entry, dict) and entry.get("kind") == "confirmed":
                    issue_number = entry.get("issue_number")
                    if isinstance(issue_number, int) and not isinstance(issue_number, bool):
                        confirmed.add((edition, issue_number))
    return confirmed


def load_blocked_draft_exceptions(
    program_id: str,
    *,
    programs_root: Path,
    as_of: date,
) -> tuple[ExceptionItem, ...]:
    """Read-only scan of unconfirmed draft manifests failing one or more quality gates."""
    publications_dir = programs_root / program_id / "publications"
    if not publications_dir.is_dir():
        return ()
    confirmed = _confirmed_issue_keys(program_id, programs_root=programs_root)
    items: list[ExceptionItem] = []
    for path in sorted(publications_dir.glob("*.manifest.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError(f"Publication manifest is not an object: {path.name}")
        qg_results = payload.get("qg_results")
        if not isinstance(qg_results, dict):
            continue
        failed_gates = sorted(str(gate) for gate, passed in qg_results.items() if passed is False)
        if not failed_gates:
            continue
        edition = str(payload.get("edition") or "unknown")
        issue_number = payload.get("issue_number")
        if isinstance(issue_number, int) and not isinstance(issue_number, bool) and (edition, issue_number) in confirmed:
            continue
        raw_started = payload.get("started_at")
        if isinstance(raw_started, str):
            started = datetime.fromisoformat(raw_started.replace("Z", "+00:00"))
            if started.utcoffset() is None:
                raise ValueError(f"Publication manifest timestamp is naive: {path.name}")
            age_days = max(0, (as_of - started.astimezone(timezone.utc).date()).days)
        else:
            age_days = 0
        manifest_id = str(payload.get("manifest_id") or path.stem)
        items.append(ExceptionItem(
            kind="blocked_draft",
            subject_id=manifest_id,
            severity_rank=0,
            age_days=age_days,
            reason=f"Draft {edition}#{issue_number} blocked by {', '.join(failed_gates)}.",
            evidence=f"publications/{path.name}",
        ))
    return tuple(items)


def load_context_gap_exceptions(
    program_id: str,
    *,
    programs_root: Path,
    as_of: date,
) -> tuple[ExceptionItem, ...]:
    """Read-only projection of context/source gaps in _feedback/context_gaps.jsonl."""
    records = load_context_gaps(program_id, programs_root=programs_root)
    items: list[ExceptionItem] = []
    for gap in rank_context_gaps(records, programs_root=programs_root):
        age_days = max(
            0,
            (as_of - gap.last_seen.astimezone(timezone.utc).date()).days,
        )
        severity_rank = 0 if gap.severity == "feature_blocked" else 1
        suffix = f" ({gap.count} signals)" if gap.count > 1 else ""
        items.append(ExceptionItem(
            kind="source_gap",
            subject_id=f"{gap.feature}:{gap.lane or 'global'}:{gap.field}",
            severity_rank=severity_rank,
            age_days=age_days,
            reason=f"[{gap.severity}] {gap.message}{suffix}",
            evidence=f"_feedback/context_gaps.jsonl#{gap.feature}:{gap.field}",
        ))
    return tuple(items)


def _zero_write_immutable(db_path: Path) -> bool:
    wal_path = db_path.with_name(f"{db_path.name}-wal")
    return not wal_path.exists() or wal_path.stat().st_size == 0


def load_unvalidated_binding_exceptions(
    program_id: str,
    *,
    programs_root: Path,
    as_of: date,
) -> tuple[ExceptionItem, ...]:
    """Read-only check for active unvalidated metric source bindings in the reality DB."""
    db_path = get_program_reality_db_path(program_id, programs_root=programs_root)
    if not db_path.is_file():
        return ()
    with open_program_db(db_path, read_only=True, immutable=_zero_write_immutable(db_path)) as connection:
        has_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reality_metric_source_bindings'"
        ).fetchone()
        if has_table is None:
            return ()
        rows = connection.execute(
            """
            SELECT binding_id, metric_id, source_kind, valid_from
            FROM reality_metric_source_bindings
            WHERE program_id = ? AND valid_until IS NULL AND validated = 0
            ORDER BY metric_id ASC, binding_id ASC
            """,
            (program_id,),
        ).fetchall()
    items: list[ExceptionItem] = []
    for binding_id, metric_id, source_kind, valid_from_text in rows:
        valid_from = datetime.fromisoformat(str(valid_from_text).replace("Z", "+00:00"))
        if valid_from.utcoffset() is None:
            raise ValueError("Stored metric source binding timestamp is not timezone-aware")
        age_days = max(0, (as_of - valid_from.astimezone(timezone.utc).date()).days)
        items.append(ExceptionItem(
            kind="unvalidated_binding",
            subject_id=str(binding_id),
            severity_rank=1,
            age_days=age_days,
            reason=f"Metric binding for {metric_id} ({source_kind}) is unvalidated.",
            evidence=f"vertex-db/{program_id}#binding:{binding_id}",
        ))
    return tuple(items)


def load_approval_expiry_exceptions(
    program_id: str,
    *,
    programs_root: Path,
    as_of: date,
) -> tuple[ExceptionItem, ...]:
    """Read-only check for expired unrevoked actuation approvals without completed dispatch."""
    db_path = outbox_db_path(program_id, programs_root=programs_root)
    if not db_path.is_file():
        return ()
    with open_program_db(db_path, read_only=True, immutable=_zero_write_immutable(db_path)) as connection:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not {"outbox_approvals", "outbox_current_approvals"}.issubset(tables):
            return ()
        has_outbox = {"outbox", "outbox_admissions"}.issubset(tables)
        outbox_filter = (
            "AND NOT EXISTS ("
            "SELECT 1 FROM outbox_admissions AS adm "
            "JOIN outbox AS o ON o.outbox_id = adm.outbox_id "
            "WHERE adm.intent_id = a.intent_id AND o.status = 'completed'"
            ")"
            if has_outbox
            else ""
        )
        rows = connection.execute(
            f"""
            SELECT a.approval_id, a.intent_id, a.revision, a.expires_at, a.evidence_expires_at
            FROM outbox_approvals AS a
            JOIN outbox_current_approvals AS c
              ON c.approval_id = a.approval_id
            WHERE a.revoked_at IS NULL
              {outbox_filter}
            ORDER BY a.expires_at ASC, a.approval_id ASC
            """
        ).fetchall()
    items: list[ExceptionItem] = []
    for approval_id, intent_id, revision, expires_at_text, evidence_expires_at_text in rows:
        exp_dt = datetime.fromisoformat(str(expires_at_text).replace("Z", "+00:00"))
        ev_dt = datetime.fromisoformat(str(evidence_expires_at_text).replace("Z", "+00:00"))
        if exp_dt.utcoffset() is None or ev_dt.utcoffset() is None:
            raise ValueError("Stored actuation approval expiry timestamp is not timezone-aware")
        earliest_date = min(
            exp_dt.astimezone(timezone.utc).date(),
            ev_dt.astimezone(timezone.utc).date(),
        )
        if earliest_date > as_of:
            continue
        age_days = max(0, (as_of - earliest_date).days)
        items.append(ExceptionItem(
            kind="approval_expiry",
            subject_id=str(approval_id),
            severity_rank=0,
            age_days=age_days,
            reason=f"Approval for {intent_id} (rev {revision}) expired {earliest_date.isoformat()}.",
            evidence=f"actuation/outbox.db#approval:{approval_id}",
        ))
    return tuple(items)


def load_runtime_incident_exceptions(
    program_id: str,
    *,
    programs_root: Path,
    as_of: date,
) -> tuple[ExceptionItem, ...]:
    """Read-only projection of recent severity-0/1/2 runtime incidents from incident_journal.jsonl."""
    entries = read_incident_entries(program_id, programs_root=programs_root)
    items: list[ExceptionItem] = []
    for entry in entries:
        if entry.severity is not None and entry.severity > 2:
            continue
        obs_date = entry.observed_at.astimezone(timezone.utc).date()
        age_days = (as_of - obs_date).days
        if age_days < 0 or age_days > 30:
            continue
        sev_label = f"Sev-{entry.severity}" if entry.severity is not None else "Incident"
        severity_rank = 0 if (entry.severity is not None and entry.severity <= 1) else 1
        items.append(ExceptionItem(
            kind="runtime_incident",
            subject_id=entry.incident_id,
            severity_rank=severity_rank,
            age_days=age_days,
            reason=f"{sev_label}: {entry.belief_change_summary}",
            evidence=f"journal/incident_journal.jsonl#{entry.signal_id}",
        ))
    return tuple(items)


def load_knowledge_proposal_exceptions(
    program_id: str,
    *,
    programs_root: Path,
    as_of: date,
) -> tuple[ExceptionItem, ...]:
    """Read-only projection of pending or expired-approval governed feedback proposals."""
    proposals_path = get_feedback_proposals_path(program_id, programs_root=programs_root)
    if not proposals_path.is_file():
        return ()
    pending_statuses = {
        ProposalStatus.PROPOSED,
        ProposalStatus.EVALUATING,
        ProposalStatus.AWAITING_APPROVAL,
        ProposalStatus.APPROVED,
        ProposalStatus.SHADOW,
    }
    items: list[ExceptionItem] = []
    for proposal in load_feedback_proposals(program_id, programs_root=programs_root):
        if proposal.status in pending_statuses:
            age_days = max(0, (as_of - proposal.updated_at.astimezone(timezone.utc).date()).days)
            items.append(ExceptionItem(
                kind="knowledge_proposal",
                subject_id=proposal.proposal_id,
                severity_rank=1,
                age_days=age_days,
                reason=f"Proposal for {proposal.subject} is {proposal.status.value}.",
                evidence=f"feedback_proposals.jsonl#{proposal.proposal_id}",
            ))
        elif proposal.status is ProposalStatus.ACTIVE and proposal.approval is not None:
            exp_date = proposal.approval.expires_at.astimezone(timezone.utc).date()
            if exp_date <= as_of:
                items.append(ExceptionItem(
                    kind="knowledge_proposal",
                    subject_id=proposal.proposal_id,
                    severity_rank=0,
                    age_days=(as_of - exp_date).days,
                    reason=f"Active proposal approval for {proposal.subject} expired {exp_date.isoformat()}.",
                    evidence=f"feedback_proposals.jsonl#{proposal.proposal_id}",
                ))
    return tuple(items)


def load_claim_exceptions(
    program_id: str,
    *,
    programs_root: Path,
    as_of: date,
) -> tuple[ExceptionItem, ...]:
    claims = load_open_claims(
        program_id, programs_root, repair_corruption=False,
    )
    asks = load_open_decision_asks(
        program_id, programs_root, repair_corruption=False,
    )
    items = [
        ExceptionItem(
            kind="open_claim",
            subject_id=claim.id,
            severity_rank=1,
            age_days=max(0, (as_of - claim.claim_date).days),
            reason=f"Claim remains open: {claim.text}",
            evidence=f"journal/claims.jsonl#{claim.id}",
        )
        for claim in claims
    ]
    items.extend(
        ExceptionItem(
            kind="open_decision_ask",
            subject_id=ask.id,
            severity_rank=0,
            age_days=max(0, (as_of - ask.ask_date).days),
            reason=f"Decision ask remains open: {ask.text}",
            evidence=f"journal/claims.jsonl#{ask.id}",
        )
        for ask in asks
    )
    return tuple(items)


def load_extended_exception_signals(
    program_id: str,
    *,
    programs_root: Path,
    as_of: date,
) -> tuple[ExceptionItem, ...]:
    """Read-only loader for RELO-W4.12 extended operational exception signals."""
    return (
        load_blocked_draft_exceptions(program_id, programs_root=programs_root, as_of=as_of)
        + load_approval_expiry_exceptions(program_id, programs_root=programs_root, as_of=as_of)
        + load_runtime_incident_exceptions(program_id, programs_root=programs_root, as_of=as_of)
        + load_knowledge_proposal_exceptions(program_id, programs_root=programs_root, as_of=as_of)
        + load_context_gap_exceptions(program_id, programs_root=programs_root, as_of=as_of)
        + load_unvalidated_binding_exceptions(program_id, programs_root=programs_root, as_of=as_of)
        + load_claim_exceptions(program_id, programs_root=programs_root, as_of=as_of)
    )


__all__ = [
    "DecoratedException",
    "DispatchException",
    "ExceptionItem",
    "build_exception_queue",
    "decorate_exception_queue",
    "load_approval_expiry_exceptions",
    "load_blocked_draft_exceptions",
    "load_claim_exceptions",
    "load_context_gap_exceptions",
    "load_dispatch_exceptions",
    "load_extended_exception_signals",
    "load_knowledge_proposal_exceptions",
    "load_runtime_incident_exceptions",
    "load_unvalidated_binding_exceptions",
]
