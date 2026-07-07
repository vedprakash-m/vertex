from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path

from src.core.claim_tracker import ClaimStatusUpdate, assess_claim_entries, load_claim_entries, load_latest_claim_statuses
from src.core.models import WorkItem
from src.core.models import WorkItem
from src.core.models_v2 import ClaimEntry, WorkstreamCalibration
from src.core.snapshot_store import ARCHIVE_ROOT, get_archive_root


_TERMINAL_CLAIM_STATUSES = {"met", "contradicted", "stale"}
_TREND_WINDOW_WEEKS = 8


@dataclass(frozen=True, slots=True)
class CalibrationRollup:
    subject_id: str
    met: int
    contradicted: int
    stale: int

    @property
    def sample_size(self) -> int:
        return self.met + self.contradicted + self.stale

    @property
    def claim_accuracy(self) -> float | None:
        if self.sample_size < 5:
            return None
        return self.met / self.sample_size


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    program_id: str
    generated_at: datetime
    since: date | None
    first_claim_date: date | None
    last_claim_date: date | None
    total_terminal_claims: int
    met: int
    contradicted: int
    stale: int
    workstream_rows: tuple[WorkstreamCalibration, ...]
    dri_rows: tuple[CalibrationRollup, ...]
    trend_window_weeks: int
    trajectory_delta_points: int | None

    @property
    def overall_accuracy(self) -> float | None:
        if self.total_terminal_claims == 0:
            return None
        return self.met / self.total_terminal_claims

    @property
    def week_span(self) -> int:
        if self.first_claim_date is None or self.last_claim_date is None:
            return 0
        return max(1, ((self.last_claim_date - self.first_claim_date).days // 7) + 1)


def build_calibration_priors(
    claims: tuple[ClaimEntry, ...],
    assessments_by_id: dict[str, str],
) -> tuple[WorkstreamCalibration, ...]:
    """Compute per-workstream calibration from a dict of claim_id → effective_status.

    Only terminal statuses (met, contradicted, stale) count toward the accuracy.
    """
    buckets: dict[str, dict[str, int]] = {}
    for claim in claims:
        ws = claim.workstream_id or "__none__"
        status = assessments_by_id.get(claim.id, claim.status)
        if status not in ("met", "contradicted", "stale"):
            continue
        if ws not in buckets:
            buckets[ws] = {"met": 0, "contradicted": 0, "stale": 0}
        buckets[ws][status] += 1

    result: list[WorkstreamCalibration] = []
    for ws_id, counts in sorted(buckets.items()):
        if ws_id == "__none__":
            continue
        result.append(WorkstreamCalibration(
            workstream_id=ws_id,
            met=counts["met"],
            contradicted=counts["contradicted"],
            stale=counts["stale"],
        ))
    return tuple(result)


def build_dri_calibration_priors(
    claims: tuple[ClaimEntry, ...],
    assessments_by_id: dict[str, str],
) -> tuple[CalibrationRollup, ...]:
    return _build_calibration_rollups(
        claims,
        assessments_by_id,
        subject_resolver=lambda claim: claim.owner_alias,
    )


def build_calibration_report(
    program_id: str,
    *,
    claims: tuple[ClaimEntry, ...],
    items: tuple[WorkItem, ...],
    as_of: datetime,
    latest_statuses: dict[str, ClaimStatusUpdate] | None = None,
    since: date | None = None,
) -> CalibrationReport:
    assessments = assess_claim_entries(
        claims,
        items=items,
        as_of=as_of,
        latest_statuses=latest_statuses,
    )
    assessments_by_id = {assessment.claim.id: assessment.effective_status for assessment in assessments}
    filtered_claims = tuple(
        claim
        for claim in claims
        if (since is None or claim.claim_date >= since)
        and assessments_by_id.get(claim.id, claim.status) in _TERMINAL_CLAIM_STATUSES
    )
    workstream_rows = build_calibration_priors(filtered_claims, assessments_by_id)
    dri_rows = build_dri_calibration_priors(filtered_claims, assessments_by_id)
    met = sum(1 for claim in filtered_claims if assessments_by_id.get(claim.id, claim.status) == "met")
    contradicted = sum(1 for claim in filtered_claims if assessments_by_id.get(claim.id, claim.status) == "contradicted")
    stale = sum(1 for claim in filtered_claims if assessments_by_id.get(claim.id, claim.status) == "stale")
    first_claim_date = min((claim.claim_date for claim in filtered_claims), default=None)
    last_claim_date = max((claim.claim_date for claim in filtered_claims), default=None)
    trend_cutoff = _ensure_utc(as_of).date() - timedelta(weeks=_TREND_WINDOW_WEEKS)
    recent_claims = tuple(claim for claim in filtered_claims if claim.claim_date >= trend_cutoff)
    prior_claims = tuple(claim for claim in filtered_claims if claim.claim_date < trend_cutoff)
    trajectory_delta_points = _trajectory_delta_points(
        recent_claims,
        prior_claims,
        assessments_by_id,
    )
    return CalibrationReport(
        program_id=program_id,
        generated_at=_ensure_utc(as_of),
        since=since,
        first_claim_date=first_claim_date,
        last_claim_date=last_claim_date,
        total_terminal_claims=len(filtered_claims),
        met=met,
        contradicted=contradicted,
        stale=stale,
        workstream_rows=workstream_rows,
        dri_rows=dri_rows,
        trend_window_weeks=_TREND_WINDOW_WEEKS,
        trajectory_delta_points=trajectory_delta_points,
    )


def compute_calibration_for_edition(
    program_id: str,
    *,
    items: tuple[WorkItem, ...],
    programs_root: Path,
    as_of_iso: str,
) -> tuple[WorkstreamCalibration, ...]:
    """Load all claims for a program, assess them, return per-workstream calibration."""
    from datetime import datetime
    claims = load_claim_entries(program_id, programs_root)
    if not claims:
        return ()
    latest_statuses = load_latest_claim_statuses(program_id, programs_root)
    as_of = datetime.fromisoformat(as_of_iso)
    assessments = assess_claim_entries(
        claims,
        items=items,
        as_of=as_of,
        latest_statuses=latest_statuses,
    )
    assessments_by_id = {a.claim.id: a.effective_status for a in assessments}
    return build_calibration_priors(claims, assessments_by_id)


def write_calibration_prior(
    edition: str,
    issue_number: int,
    calibrations: tuple[WorkstreamCalibration, ...],
    archive_root: Path = ARCHIVE_ROOT,
) -> Path:
    edition_archive = get_archive_root(edition, archive_root)
    review_dir = edition_archive / "review"
    review_dir.mkdir(parents=True, exist_ok=True)
    path = review_dir / f"issue_{issue_number:03d}.calibration.json"
    payload = {
        "issue_number": issue_number,
        "edition": edition,
        "workstreams": [
            {
                "workstream_id": c.workstream_id,
                "met": c.met,
                "contradicted": c.contradicted,
                "stale": c.stale,
                "sample_size": c.sample_size,
                "claim_accuracy": c.claim_accuracy,
            }
            for c in calibrations
        ],
    }
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temp, path)
    return path


def read_calibration_prior(
    edition: str,
    issue_number: int,
    archive_root: Path = ARCHIVE_ROOT,
) -> tuple[WorkstreamCalibration, ...] | None:
    path = get_archive_root(edition, archive_root) / "review" / f"issue_{issue_number:03d}.calibration.json"
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    result: list[WorkstreamCalibration] = []
    for entry in payload.get("workstreams", []):
        result.append(WorkstreamCalibration(
            workstream_id=str(entry["workstream_id"]),
            met=int(entry.get("met", 0)),
            contradicted=int(entry.get("contradicted", 0)),
            stale=int(entry.get("stale", 0)),
        ))
    return tuple(result)


def _build_calibration_rollups(
    claims: tuple[ClaimEntry, ...],
    assessments_by_id: dict[str, str],
    *,
    subject_resolver,
) -> tuple[CalibrationRollup, ...]:
    buckets: dict[str, dict[str, int]] = {}
    for claim in claims:
        subject_id = str(subject_resolver(claim) or "").strip().lower()
        status = assessments_by_id.get(claim.id, claim.status)
        if not subject_id or status not in _TERMINAL_CLAIM_STATUSES:
            continue
        if subject_id not in buckets:
            buckets[subject_id] = {"met": 0, "contradicted": 0, "stale": 0}
        buckets[subject_id][status] += 1
    return tuple(
        CalibrationRollup(
            subject_id=subject_id,
            met=counts["met"],
            contradicted=counts["contradicted"],
            stale=counts["stale"],
        )
        for subject_id, counts in sorted(buckets.items())
    )


def _trajectory_delta_points(
    recent_claims: tuple[ClaimEntry, ...],
    prior_claims: tuple[ClaimEntry, ...],
    assessments_by_id: dict[str, str],
) -> int | None:
    recent_accuracy = _accuracy_for_claims(recent_claims, assessments_by_id)
    prior_accuracy = _accuracy_for_claims(prior_claims, assessments_by_id)
    if recent_accuracy is None or prior_accuracy is None:
        return None
    return round((recent_accuracy - prior_accuracy) * 100)


def _accuracy_for_claims(
    claims: tuple[ClaimEntry, ...],
    assessments_by_id: dict[str, str],
) -> float | None:
    if not claims:
        return None
    met = sum(1 for claim in claims if assessments_by_id.get(claim.id, claim.status) == "met")
    return met / len(claims)


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
