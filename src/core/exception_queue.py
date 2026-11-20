"""RELO-W4.12: deterministic, read-only exception queue for the cockpit.

Ranks existing signals only; computes nothing new and writes nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from src.core.actuation_outbox import outbox_db_path
from src.core._db import open_program_db
from src.core.action_tracker import assess_action_staleness
from src.core.models_v2 import ActionItem, RiskEntry, RiskImpact, RiskProbability, RiskStatus
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
) -> tuple[ExceptionItem, ...]:
    """Rank: needs_input (0) > overdue_action (1) > stale_risk (2); ties by oldest, then id."""
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


__all__ = [
    "DispatchException",
    "ExceptionItem",
    "build_exception_queue",
    "load_dispatch_exceptions",
]
