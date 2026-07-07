"""Evidence quality checks: ETA slippage (BL-32) and False Done detection (BL-31).

BL-32: Emit checks when a WorkstreamEvidence ETA has passed without closure.
BL-31: Warn when a Done/Low lane has new evidence containing active-issue keywords.

Phase 2 note: Both checks depend on WorkstreamEvidence.etas being populated by
ContentExtractionAgent (Phase 3). In Phase 2, build_placeholder_evidence returns
confidence=0.0 (no ETAs extracted), so all checks silently return []. The checks
are wired now so Phase 3 activates them automatically when etas are populated.

ME-02: evidence_override parameter allows passing AI-extracted evidence to bypass
the placeholder evidence, activating these checks when gather --extract-evidence runs.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from src.commands.doctor_checks.models import DoctorCheck
from src.core.evidence_models import WorkstreamEvidence, build_placeholder_evidence
from src.core.models import RiskLevel

_WARN_OVERDUE_DAYS = 7
_ERROR_OVERDUE_DAYS = 14
_CONF_DRIFT_THRESHOLD = 0.20   # 20% drop triggers WARN
_LOOKBACK_DAYS = 30

_FALSE_DONE_KEYWORDS = frozenset({
    "blocking", "blocked", "blocker",
    "issue", "problem", "risk", "concern",
    "meeting", "sync", "review",
    "active", "open", "unresolved", "pending",
})


def check_eta_slippage(
    registry_entries_raw: list[dict],
    as_of: date,
    evidence_override: dict[str, WorkstreamEvidence] | None = None,
) -> list[DoctorCheck]:
    """Emit checks when an ETA in WorkstreamEvidence has passed without closure.

    Silently skips entries where evidence.confidence == 0.0 (placeholder-only).
    Becomes meaningful in Phase 3+ when ContentExtractionAgent populates etas.
    Pass evidence_override (from _load_evidence_store) to use AI-extracted evidence.
    """
    issues: list[DoctorCheck] = []
    for entry in registry_entries_raw:
        lane_id = entry.get("id", "?")
        # Use extracted evidence if available; fall back to placeholder
        if evidence_override and lane_id in evidence_override:
            evidence = evidence_override[lane_id]
        else:
            evidence = build_placeholder_evidence(  # type: ignore[assignment]
                lane_id=lane_id,
                workiq_latest=entry.get("workiq_latest"),
            )
        if evidence is None or evidence.confidence == 0.0:
            continue
        for eta in evidence.etas:
            if eta.status == "closed":
                continue
            days_overdue = (as_of - eta.eta_date).days
            if days_overdue <= 0:
                continue
            status = "fail" if days_overdue >= _ERROR_OVERDUE_DAYS else "warn"
            ado_hint = f" (ADO:{eta.ado_id})" if eta.ado_id else ""
            owner_hint = f" Owner: {eta.owner}." if eta.owner else ""
            issues.append(DoctorCheck(
                label="ETA slippage",
                status=status,
                detail=(
                    f"[ETA_SLIP] Lane '{lane_id}': ETA missed — '{eta.label}'{ado_hint} "
                    f"was due {eta.eta_date} ({days_overdue}d ago, status={eta.status}).{owner_hint}"
                ),
            ))
    return issues


def check_false_done_lanes(
    registry_entries_raw: list[dict],
    enrichments_by_lane: "dict[str, tuple]",
    as_of: date,
    keyword_threshold: int = 2,
    evidence_override: dict[str, WorkstreamEvidence] | None = None,
) -> list[DoctorCheck]:
    """Warn when a Done/Low lane has new evidence containing active-issue keywords.

    Requires body_text-populated Enrichment objects (available after Phase 1+3).
    In Phase 2, enrichments_by_lane will typically be empty so this returns [].
    Pass evidence_override to use AI-extracted risk_level instead of placeholder.
    """
    issues: list[DoctorCheck] = []
    for entry in registry_entries_raw:
        lane_id = entry.get("id", "?")
        # Use extracted evidence if available; fall back to placeholder
        if evidence_override and lane_id in evidence_override:
            evidence = evidence_override[lane_id]
        else:
            evidence = build_placeholder_evidence(  # type: ignore[assignment]
                lane_id=lane_id,
                workiq_latest=entry.get("workiq_latest"),
            )
        if evidence is None or evidence.risk_level not in (RiskLevel.DONE, RiskLevel.LOW):
            continue
        enrichments = enrichments_by_lane.get(lane_id, ())
        evidence_date = evidence.synthesized_at.date()
        new_enrichments = [
            e for e in enrichments
            if e.timestamp.date() >= evidence_date and e.body_text
        ]
        for enrichment in new_enrichments:
            words = set(enrichment.body_text.lower().split())
            hit_count = len(words & _FALSE_DONE_KEYWORDS)
            if hit_count >= keyword_threshold:
                issues.append(DoctorCheck(
                    label="False Done",
                    status="warn",
                    detail=(
                        f"[FALSE_DONE] Lane '{lane_id}' is marked {evidence.risk_level.value} but "
                        f"new evidence from {enrichment.timestamp.date()} contains "
                        f"{hit_count} active-issue keyword(s). Source: {enrichment.excerpt}. "
                        f"Verify the Done/Low status is still current."
                    ),
                ))
    return issues


def check_evidence_quality_drift(
    program_id: str,
    programs_root: Path,
    as_of: date,
) -> list[DoctorCheck]:
    """Warn when a lane's extraction confidence drops significantly vs 30-day baseline.

    Also warns when all recent records for a lane have confidence=0.0 (extractor never ran
    successfully, or ME-02 not yet wired).
    """
    from datetime import datetime, timezone, timedelta
    from src.core.evidence_quality import load_evidence_quality

    issues: list[DoctorCheck] = []
    since = datetime(as_of.year, as_of.month, as_of.day, tzinfo=timezone.utc) - timedelta(days=_LOOKBACK_DAYS)

    try:
        all_records = load_evidence_quality(program_id, programs_root=programs_root, since=since)
    except Exception:
        return issues

    if not all_records:
        return issues

    by_lane: dict[str, list] = {}
    for rec in all_records:
        by_lane.setdefault(rec.lane_id, []).append(rec)

    for lane_id, records in by_lane.items():
        confidences = [r.confidence for r in records]
        if not confidences:
            continue

        if all(c == 0.0 for c in confidences):
            issues.append(DoctorCheck(
                label="Evidence quality",
                status="warn",
                detail=(
                    f"[CONF_ZERO] Lane '{lane_id}': all {len(records)} quality record(s) in "
                    f"last {_LOOKBACK_DAYS}d have confidence=0.0. "
                    f"ContentExtractionAgent may not be running. Use: vertex gather --extract-evidence"
                ),
            ))
            continue

        mid = len(confidences) // 2
        if mid < 2:
            continue
        early_avg = sum(confidences[:mid]) / mid
        recent_avg = sum(confidences[mid:]) / (len(confidences) - mid)
        if early_avg > 0 and (early_avg - recent_avg) / early_avg > _CONF_DRIFT_THRESHOLD:
            drop_pct = int((early_avg - recent_avg) / early_avg * 100)
            issues.append(DoctorCheck(
                label="Evidence quality",
                status="warn",
                detail=(
                    f"[CONF_DRIFT] Lane '{lane_id}': extraction confidence dropped {drop_pct}% "
                    f"(from {early_avg:.2f} to {recent_avg:.2f}) over last {_LOOKBACK_DAYS}d. "
                    f"Source or model may have changed."
                ),
            ))

    return issues
