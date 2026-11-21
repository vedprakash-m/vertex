"""RELO-W4.12: deterministic, read-only exception queue for the cockpit.

Ranks existing signals only; computes nothing new and writes nothing.
"""

from __future__ import annotations

from collections import defaultdict
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from src.core.actuation_outbox import outbox_db_path
from src.core._db import open_program_db
from src.core.action_tracker import assess_action_staleness
from src.core.context_gap_store import ContextGapRecord, load_context_gaps
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


def load_dispatch_exceptions(
    program_id: str,
    *,
    programs_root: Path,
    as_of: date,
) -> tuple[DispatchException, ...]:
    db_path = outbox_db_path(program_id, programs_root=programs_root)
    if not db_path.exists():
        return ()
    with open_program_db(db_path, read_only=True) as connection:
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
    grouped: dict[tuple[str, str | None, str], list[ContextGapRecord]] = defaultdict(list)
    for rec in records:
        grouped[(rec.feature, rec.lane, rec.field)].append(rec)
    items: list[ExceptionItem] = []
    for (feature, lane, field_name), group in sorted(grouped.items(), key=lambda entry: (entry[0][0], entry[0][1] or "", entry[0][2])):
        latest = max(group, key=lambda rec: rec.ts)
        age_days = max(0, (as_of - latest.ts.astimezone(timezone.utc).date()).days)
        severity_rank = 0 if latest.severity == "feature_blocked" else 1
        suffix = f" ({len(group)} signals)" if len(group) > 1 else ""
        items.append(ExceptionItem(
            kind="source_gap",
            subject_id=f"{feature}:{lane or 'global'}:{field_name}",
            severity_rank=severity_rank,
            age_days=age_days,
            reason=f"[{latest.severity}] {latest.message}{suffix}",
            evidence=f"_feedback/context_gaps.jsonl#{feature}:{field_name}",
        ))
    return tuple(items)


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
    wal_path = db_path.with_name(f"{db_path.name}-wal")
    use_immutable = not wal_path.exists() or wal_path.stat().st_size == 0
    with open_program_db(db_path, read_only=True, immutable=use_immutable) as connection:
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


def load_extended_exception_signals(
    program_id: str,
    *,
    programs_root: Path,
    as_of: date,
) -> tuple[ExceptionItem, ...]:
    """Read-only loader for RELO-W4.12 extended operational exception signals."""
    return (
        load_blocked_draft_exceptions(program_id, programs_root=programs_root, as_of=as_of)
        + load_context_gap_exceptions(program_id, programs_root=programs_root, as_of=as_of)
        + load_unvalidated_binding_exceptions(program_id, programs_root=programs_root, as_of=as_of)
    )


__all__ = [
    "DispatchException",
    "ExceptionItem",
    "build_exception_queue",
    "load_blocked_draft_exceptions",
    "load_context_gap_exceptions",
    "load_dispatch_exceptions",
    "load_extended_exception_signals",
    "load_unvalidated_binding_exceptions",
]
