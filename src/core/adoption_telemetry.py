"""ADF-W5.14 (Section 3.4/ADF-OM15): privacy-safe cockpit/golden-workflow
adoption telemetry -- who ran a golden workflow each cadence, and why an
eligible operator did not.

ADF-OM15, verbatim: "Ratified percentage of eligible TPM/EM users run the
cockpit or golden workflow each cadence, with non-adoption reason captured."

Privacy-safe by construction: an operator identity, if supplied, is
one-way-hashed before it ever reaches disk (same "never persist a reverse
mapping" precedent as ADR-0015 / ``src.core.rev.privacy``); nothing here can
be used to recover the original operator string.

Zone A -- no AI or M365 imports.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.jsonl_utils import append_jsonl_line, read_jsonl_records

ADOPTION_TELEMETRY_SCHEMA_VERSION = "1"

#: Section 9.7's own retention target for "workflow/value events" (also used
#: by ``adf_config.RETENTION_FLOOR_DAYS["workflow_value_events_days"]``).
DEFAULT_QUERY_WINDOW_WEEKS = 57  # ~13 months


class GoldenWorkflow(str, Enum):
    """The golden workflows ADF-G13/ADF-OM15 name, plus the cockpit itself."""

    COCKPIT_SHOW = "cockpit_show"
    COCKPIT_BUILD = "cockpit_build"
    WEEKLY_REPORT = "weekly_report"
    MEETING_TO_ACTION = "meeting_to_action"
    RISK_DEPENDENCY_REVIEW = "risk_dependency_review"


class NonAdoptionReason(str, Enum):
    """A closed vocabulary, not free text -- keeps the ADF-OM15 dashboard's
    reason breakdown meaningful across operators and time."""

    NOT_APPLICABLE_THIS_CADENCE = "not_applicable_this_cadence"
    MANUAL_PROCESS_PREFERRED = "manual_process_preferred"
    TOOL_ISSUE = "tool_issue"
    UNAWARE = "unaware"
    OTHER = "other"


def pseudonymize_operator(operator_ref: str) -> str:
    """One-way hash of an operator identity string. No reverse mapping is
    ever persisted anywhere in this module -- matching ADR-0015's rejection
    of a ``PseudonymTable``-style reversible mapping for exactly this reason."""
    normalized = operator_ref.strip().lower()
    return "sha256:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _iso_cadence_period(when: date) -> str:
    iso_year, iso_week, _ = when.isocalendar()
    return f"{iso_year:04d}-W{iso_week:02d}"


@dataclass(frozen=True, slots=True)
class AdoptionEvent:
    schema_version: str
    program_id: str
    workflow: GoldenWorkflow
    cadence_period: str
    adopted: bool
    recorded_at: datetime
    operator_ref: str | None = None
    reason: NonAdoptionReason | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "program_id": self.program_id,
            "workflow": self.workflow.value,
            "cadence_period": self.cadence_period,
            "adopted": self.adopted,
            "recorded_at": self.recorded_at.astimezone(timezone.utc).isoformat(),
            "operator_ref": self.operator_ref,
            "reason": self.reason.value if self.reason else None,
        }

    @staticmethod
    def from_dict(payload: dict[str, Any]) -> "AdoptionEvent":
        raw_reason = payload.get("reason")
        return AdoptionEvent(
            schema_version=str(payload["schema_version"]),
            program_id=str(payload["program_id"]),
            workflow=GoldenWorkflow(payload["workflow"]),
            cadence_period=str(payload["cadence_period"]),
            adopted=bool(payload["adopted"]),
            recorded_at=datetime.fromisoformat(payload["recorded_at"]),
            operator_ref=payload.get("operator_ref"),
            reason=NonAdoptionReason(raw_reason) if raw_reason else None,
        )


def _adoption_telemetry_path(program_id: str, *, programs_root: Path) -> Path:
    return programs_root / program_id / "runtime" / "adoption_telemetry.jsonl"


def _append(event: AdoptionEvent, *, programs_root: Path) -> None:
    path = _adoption_telemetry_path(event.program_id, programs_root=programs_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event.to_dict(), sort_keys=True) + "\n"
    append_jsonl_line(path, line)


def record_adoption(
    program_id: str,
    workflow: GoldenWorkflow,
    *,
    operator_ref: str | None = None,
    now: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> AdoptionEvent:
    """Records that an eligible operator actually ran a golden workflow this
    cadence period. Callers should treat this as best-effort (never let a
    telemetry write failure break the workflow it's describing)."""
    resolved_now = now or datetime.now(timezone.utc)
    event = AdoptionEvent(
        schema_version=ADOPTION_TELEMETRY_SCHEMA_VERSION,
        program_id=program_id,
        workflow=workflow,
        cadence_period=_iso_cadence_period(resolved_now.date()),
        adopted=True,
        recorded_at=resolved_now,
        operator_ref=pseudonymize_operator(operator_ref) if operator_ref else None,
    )
    _append(event, programs_root=programs_root)
    return event


def record_non_adoption(
    program_id: str,
    workflow: GoldenWorkflow,
    reason: NonAdoptionReason,
    *,
    operator_ref: str | None = None,
    now: datetime | None = None,
    programs_root: Path = PROGRAMS_ROOT,
) -> AdoptionEvent:
    """Explicit non-adoption reason capture -- ADF-OM15's "with non-adoption
    reason captured" half. A CLI cannot observe a workflow that never ran, so
    this is a deliberate log-the-skip entry point (``vertex cockpit
    adoption-skip``), not an automatic inference from absence."""
    resolved_now = now or datetime.now(timezone.utc)
    event = AdoptionEvent(
        schema_version=ADOPTION_TELEMETRY_SCHEMA_VERSION,
        program_id=program_id,
        workflow=workflow,
        cadence_period=_iso_cadence_period(resolved_now.date()),
        adopted=False,
        recorded_at=resolved_now,
        operator_ref=pseudonymize_operator(operator_ref) if operator_ref else None,
        reason=reason,
    )
    _append(event, programs_root=programs_root)
    return event


def read_adoption_events(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
) -> tuple[AdoptionEvent, ...]:
    path = _adoption_telemetry_path(program_id, programs_root=programs_root)
    if not path.exists():
        return ()
    return tuple(AdoptionEvent.from_dict(raw) for raw in read_jsonl_records(path))


@dataclass(frozen=True, slots=True)
class AdoptionRateSummary:
    program_id: str
    workflow: GoldenWorkflow | None
    cadence_periods_covered: int
    adopted_count: int
    non_adopted_count: int
    adoption_rate: float | None  # None when no events at all are recorded yet
    reason_breakdown: dict[str, int]


def compute_adoption_rate(
    program_id: str,
    *,
    workflow: GoldenWorkflow | None = None,
    since_weeks: int = DEFAULT_QUERY_WINDOW_WEEKS,
    programs_root: Path = PROGRAMS_ROOT,
    now: datetime | None = None,
) -> AdoptionRateSummary:
    """ADF-OM15's own metric: the share of eligible cadence periods that
    actually saw an adoption event, over a recent window, plus a breakdown
    of the reasons behind the rest."""
    resolved_now = now or datetime.now(timezone.utc)
    cutoff_period = _iso_cadence_period((resolved_now - timedelta(weeks=since_weeks)).date())
    events = read_adoption_events(program_id, programs_root=programs_root)
    in_window = [
        e for e in events
        if e.cadence_period >= cutoff_period and (workflow is None or e.workflow == workflow)
    ]
    adopted = [e for e in in_window if e.adopted]
    non_adopted = [e for e in in_window if not e.adopted]
    reason_breakdown: dict[str, int] = {}
    for event in non_adopted:
        key = event.reason.value if event.reason else "unspecified"
        reason_breakdown[key] = reason_breakdown.get(key, 0) + 1
    total = len(adopted) + len(non_adopted)
    rate = (len(adopted) / total) if total else None
    periods_covered = len({e.cadence_period for e in in_window})
    return AdoptionRateSummary(
        program_id=program_id,
        workflow=workflow,
        cadence_periods_covered=periods_covered,
        adopted_count=len(adopted),
        non_adopted_count=len(non_adopted),
        adoption_rate=rate,
        reason_breakdown=reason_breakdown,
    )


__all__ = [
    "ADOPTION_TELEMETRY_SCHEMA_VERSION",
    "DEFAULT_QUERY_WINDOW_WEEKS",
    "AdoptionEvent",
    "AdoptionRateSummary",
    "GoldenWorkflow",
    "NonAdoptionReason",
    "compute_adoption_rate",
    "pseudonymize_operator",
    "read_adoption_events",
    "record_adoption",
    "record_non_adoption",
]
