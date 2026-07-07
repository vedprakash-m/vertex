"""WorkIQ / M365 enrichment quality gates (QG-WIQ-1 … QG-WIQ-9).

Newsletter-WorkIQ spec §14.1. These gates guard the M365 enrichment workflow across
three surfaces (``confirm``, ``report``, ``doctor``):

* QG-WIQ-1 (confirm, hard block)  — any PENDING WorkIQ signal blocks confirm.
* QG-WIQ-2 (confirm, soft warn)   — zero ``WorkstreamEvidence`` with confidence > 0.0.
* QG-WIQ-3 (confirm, soft warn)   — a source's last-seen is older than its per-source
  ``discovery_threshold_hours`` (IcM 12h, Teams/transcript 48h, Kusto 168h).
* QG-WIQ-4 (report, info)         — WorkIQ run cost > 80% of the configured per-run budget.
* QG-WIQ-5 (doctor, warning)      — a transcript-enabled meeting series has neither
  ``series_id`` nor ``calendar_name`` (no identifier seeded).
* QG-WIQ-6 (doctor, warning)      — no M365 signals in the journal for the recent window.
* QG-WIQ-7 (confirm, hard block)  — a confidence>0 evidence record has no
  ``EvidenceProvenanceRecord`` for its lane.
* QG-WIQ-8 (doctor, warning)      — the triple-null condition (``include_transcripts``
  + ``series_id`` null + ``calendar_name`` null): no transcript path is available, so
  transcript extraction is blocked for that series.
* QG-WIQ-9 (doctor, warning)      — ``workiq_latest`` for a lane is > 24h newer than the
  lane's ``WorkstreamEvidence.synthesized_at`` (display vs synthesis divergence).

Each gate is a pure function over well-defined inputs so it is unit-testable in
isolation. ``evaluate_workiq_confirm_gates`` is a convenience that reads the signal,
evidence, and provenance stores itself (matching the self-contained pattern of
``_evaluate_open_action_completeness_gate``) so the confirm wiring is a single
``combine_gate_reports`` call.

Zone A: imports only from ``src/core/`` (models, models_v2, evidence_models,
evidence_store, signal_review, store_factory, jsonl_utils).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from src.core.evidence_models import WorkstreamEvidence, parse_workiq_latest_date
from src.core.evidence_store import load_evidence_records
from src.core.jsonl_utils import read_jsonl_records
from src.core.models_v2 import Signal, TeamsMeetingSeries, Workstream
from src.core.quality_gates.models import GateEvaluation, QualityGateReport
from src.core.signal_review import signal_needs_review
from src.core.store_factory import build_signal_store_for_program_id

# Per-source discovery freshness thresholds (spec §14.1 QG-WIQ-3). Hours.
_DEFAULT_SOURCE_THRESHOLDS_HOURS: dict[str, int] = {
    "icm": 12,
    "teams": 48,
    "transcript": 48,
    "kusto": 168,
}

_M365_SOURCE_PREFIXES: tuple[str, ...] = ("workiq", "teams", "transcript")


def is_m365_signal(signal: Signal) -> bool:
    """True for WorkIQ / Teams / transcript signals (the M365 namespace)."""
    source = (signal.source or "").lower()
    return any(source == prefix or source.startswith(prefix + "/") or source == prefix for prefix in _M365_SOURCE_PREFIXES)


# ── QG-WIQ-1 ──────────────────────────────────────────────────────────────────


def evaluate_workiq_pending_signal_gate(
    *,
    pending_workiq_signals: tuple[Signal, ...] | list[Signal],
) -> GateEvaluation:
    """QG-WIQ-1 (confirm, hard block): any PENDING WorkIQ signal blocks confirm.

    ``pending_workiq_signals`` is the set of M365 signals still requiring review
    (``signal_needs_review`` True) — the caller filters, this gate judges.
    """
    count = len(tuple(pending_workiq_signals))
    if count == 0:
        return GateEvaluation(
            "QG-WIQ-1",
            True,
            "No pending WorkIQ signals blocking confirm.",
            3,
        )
    sample_ids = ", ".join(sorted({s.id for s in pending_workiq_signals if s.id}))[:200]
    return GateEvaluation(
        "QG-WIQ-1",
        False,
        f"{count} pending WorkIQ signal(s) require review before confirm: {sample_ids}",
        3,
        forceable=False,
    )


# ── QG-WIQ-2 ──────────────────────────────────────────────────────────────────


def evaluate_workiq_evidence_presence_gate(
    *,
    evidence: tuple[WorkstreamEvidence, ...] | list[WorkstreamEvidence],
) -> GateEvaluation:
    """QG-WIQ-2 (confirm, soft warn): zero confidence>0 evidence for the current issue."""
    confident = [ev for ev in evidence if ev.confidence > 0.0]
    if confident:
        return GateEvaluation(
            "QG-WIQ-2",
            True,
            f"{len(confident)} lane(s) have AI-extracted WorkstreamEvidence (confidence > 0.0).",
            1,
            forceable=True,
        )
    return GateEvaluation(
        "QG-WIQ-2",
        False,
        "No WorkstreamEvidence with confidence > 0.0 found. Run `vertex enrich` before confirm for M365-sourced narrative.",
        1,
        forceable=True,
    )


# ── QG-WIQ-3 ──────────────────────────────────────────────────────────────────


def evaluate_workiq_source_freshness_gate(
    *,
    source_last_seen: Mapping[str, datetime | None],
    thresholds_hours: Mapping[str, int] | None = None,
    as_of: datetime | None = None,
) -> GateEvaluation:
    """QG-WIQ-3 (confirm, soft warn): a source's last-seen exceeds its threshold.

    ``source_last_seen`` maps a source key (``icm``/``teams``/``transcript``/``kusto``)
    to its last-seen datetime, or ``None`` if never seen. A missing/old source warns.
    """
    resolved_as_of = as_of or datetime.now(timezone.utc)
    thresholds = dict(_DEFAULT_SOURCE_THRESHOLDS_HOURS)
    if thresholds_hours:
        thresholds.update({k.lower(): int(v) for k, v in thresholds_hours.items()})
    stale: list[str] = []
    for source, last_seen in source_last_seen.items():
        threshold_hours = thresholds.get(source.lower())
        if threshold_hours is None:
            continue
        if last_seen is None:
            stale.append(f"{source}=never")
            continue
        age = resolved_as_of - _coerce_utc(last_seen)
        if age > timedelta(hours=threshold_hours):
            stale.append(f"{source}={age.total_seconds() / 3600:.1f}h>{threshold_hours}h")
    if not stale:
        return GateEvaluation(
            "QG-WIQ-3",
            True,
            "All M365 quantitative/transcript sources are within their discovery freshness thresholds.",
            1,
            forceable=True,
        )
    return GateEvaluation(
        "QG-WIQ-3",
        False,
        "Evidence window older than per-source thresholds: " + ", ".join(sorted(stale)),
        1,
        forceable=True,
    )


# ── QG-WIQ-4 ──────────────────────────────────────────────────────────────────


def evaluate_workiq_budget_gate(
    *,
    cost_usd: float,
    budget_usd_per_run: float,
) -> GateEvaluation:
    """QG-WIQ-4 (report, info): WorkIQ run cost exceeds 80% of the configured budget.

    Info gates pass (``passed=True``) — they surface a note, never block.
    """
    if budget_usd_per_run <= 0:
        return GateEvaluation(
            "QG-WIQ-4",
            True,
            "WorkIQ budget gate skipped (no per-run budget configured).",
            0,
        )
    ratio = cost_usd / budget_usd_per_run
    if ratio <= 0.8:
        return GateEvaluation(
            "QG-WIQ-4",
            True,
            f"WorkIQ run cost ${cost_usd:.4f} is within 80% of the ${budget_usd_per_run:.2f} budget ({ratio:.0%}).",
            0,
        )
    return GateEvaluation(
        "QG-WIQ-4",
        True,
        f"WorkIQ run cost ${cost_usd:.4f} is {ratio:.0%} of the ${budget_usd_per_run:.2f} budget (over 80% threshold).",
        0,
    )


# ── QG-WIQ-5 / QG-WIQ-8 ────────────────────────────────────────────────────────


def _unidentified_transcript_series(
    meeting_series: tuple[TeamsMeetingSeries, ...] | list[TeamsMeetingSeries],
) -> tuple[TeamsMeetingSeries, ...]:
    """Series with ``include_transcripts`` on but neither ``series_id`` nor ``calendar_name``."""
    return tuple(
        series
        for series in meeting_series
        if series.include_transcripts and not series.series_id and not series.calendar_name
    )


def evaluate_workiq_transcript_identifier_gate(
    *,
    meeting_series: tuple[TeamsMeetingSeries, ...] | list[TeamsMeetingSeries],
) -> GateEvaluation:
    """QG-WIQ-5 (doctor, warning): no identifier seeded for a transcript-enabled series.

    Suggests seeding ``series_id`` (``vertex integration seed-id``) OR configuring
    ``calendar_name`` (P4-21 name-based path). No warning when ``calendar_name`` is set.
    """
    unidentified = _unidentified_transcript_series(meeting_series)
    if not unidentified:
        return GateEvaluation(
            "QG-WIQ-5",
            True,
            "All transcript-enabled meeting series have a series_id or calendar_name.",
            1,
        )
    names = ", ".join(sorted({s.display_name for s in unidentified}))[:200]
    return GateEvaluation(
        "QG-WIQ-5",
        False,
        f"Transcript-enabled meeting series without a series_id or calendar_name: {names}. "
        "Seed `series_id` via `vertex integration seed-id` OR configure `calendar_name` (P4-21).",
        1,
    )


def evaluate_workiq_transcript_extraction_block_gate(
    *,
    meeting_series: tuple[TeamsMeetingSeries, ...] | list[TeamsMeetingSeries],
) -> GateEvaluation:
    """QG-WIQ-8 (doctor, warning): triple-null → no transcript path; block extraction."""
    unidentified = _unidentified_transcript_series(meeting_series)
    if not unidentified:
        return GateEvaluation(
            "QG-WIQ-8",
            True,
            "Every transcript-enabled series has at least one transcript identifier.",
            1,
        )
    names = ", ".join(sorted({s.display_name for s in unidentified}))[:200]
    return GateEvaluation(
        "QG-WIQ-8",
        False,
        f"No transcript path available for series (include_transcripts=true, series_id=null, calendar_name=null): {names}. "
        "Transcript extraction blocked for these series until one identifier is configured.",
        1,
    )


# ── QG-WIQ-6 ──────────────────────────────────────────────────────────────────


def evaluate_workiq_signal_recency_gate(
    *,
    m365_signals: tuple[Signal, ...] | list[Signal],
    as_of: datetime | None = None,
) -> GateEvaluation:
    """QG-WIQ-6 (doctor, warning): no M365 signals in the journal for the recent window.

    The spec frames this as "last 3 gather runs"; operationally we check whether any
    M365 signal exists in the supplied window (the caller scopes ``m365_signals`` to
    the recent window, e.g. the evidence window).
    """
    m365 = tuple(s for s in m365_signals if is_m365_signal(s))
    if m365:
        return GateEvaluation(
            "QG-WIQ-6",
            True,
            f"{len(m365)} M365 signal(s) present in the recent journal window.",
            1,
        )
    return GateEvaluation(
        "QG-WIQ-6",
        False,
        "No M365 signals in the journal for the recent window. Run `vertex gather --workiq` / `vertex enrich` to populate.",
        1,
    )


# ── QG-WIQ-7 ──────────────────────────────────────────────────────────────────


def evaluate_workiq_blurb_provenance_gate(
    *,
    evidence: tuple[WorkstreamEvidence, ...] | list[WorkstreamEvidence],
    provenance_lane_ids: frozenset[str] | set[str],
) -> GateEvaluation:
    """QG-WIQ-7 (confirm, hard block if enabled): a confidence>0 evidence record has
    no ``EvidenceProvenanceRecord`` for its lane.

    ``provenance_lane_ids`` is the set of lane ids that have a provenance record
    (loaded from ``evidence_provenance.jsonl`` by the caller or by
    ``evaluate_workiq_confirm_gates``).
    """
    provenance_set = frozenset(provenance_lane_ids)
    unprovenanced = tuple(
        ev for ev in evidence
        if ev.confidence > 0.0 and ev.lane_id not in provenance_set
    )
    if not unprovenanced:
        return GateEvaluation(
            "QG-WIQ-7",
            True,
            "All AI-extracted evidence has a matching EvidenceProvenanceRecord.",
            3,
            forceable=True,
        )
    lanes = ", ".join(sorted({ev.lane_id for ev in unprovenanced}))[:200]
    return GateEvaluation(
        "QG-WIQ-7",
        False,
        f"AI-extracted WorkstreamEvidence without an EvidenceProvenanceRecord for lane(s): {lanes}.",
        3,
        forceable=True,
    )


# ── QG-WIQ-9 ──────────────────────────────────────────────────────────────────


def evaluate_workiq_latest_divergence_gate(
    *,
    workiq_latest_by_lane: Mapping[str, str | None],
    evidence_by_lane: Mapping[str, WorkstreamEvidence],
    as_of: datetime | None = None,
) -> GateEvaluation:
    """QG-WIQ-9 (doctor, warning): ``workiq_latest`` is > 24h newer than the lane's
    ``WorkstreamEvidence.synthesized_at`` — display vs synthesis divergence."""
    resolved_as_of = as_of or datetime.now(timezone.utc)
    divergent: list[str] = []
    for lane_id, workiq_latest in workiq_latest_by_lane.items():
        if not workiq_latest:
            continue
        latest_date = parse_workiq_latest_date(workiq_latest)
        evidence = evidence_by_lane.get(lane_id)
        if latest_date is None or evidence is None:
            continue
        # Compare the workiq_latest date to the evidence synthesis timestamp.
        synthesized = _coerce_utc(evidence.synthesized_at)
        latest_dt = datetime.combine(latest_date, datetime.min.time(), tzinfo=timezone.utc)
        if latest_dt > synthesized + timedelta(hours=24):
            divergent.append(lane_id)
    if not divergent:
        return GateEvaluation(
            "QG-WIQ-9",
            True,
            "workiq_latest timestamps are consistent with WorkstreamEvidence.synthesized_at.",
            1,
        )
    lanes = ", ".join(sorted(divergent))[:200]
    return GateEvaluation(
        "QG-WIQ-9",
        False,
        f"workiq_latest is > 24h newer than WorkstreamEvidence.synthesized_at for lane(s): {lanes}. "
        "The displayed NL summary may diverge from the evidence feeding blurb synthesis — re-run `vertex enrich`.",
        1,
    )


# ── Convenience: self-contained confirm block ──────────────────────────────────


def evaluate_workiq_confirm_gates(
    *,
    program_id: str | None,
    programs_root: Path,
    channel_states: Mapping[str, Mapping[str, Any]] | None = None,
    source_thresholds_hours: Mapping[str, int] | None = None,
    workstreams: tuple[Workstream, ...] | list[Workstream] = (),
    as_of: datetime | None = None,
) -> QualityGateReport:
    """QG-WIQ-1/2/3/7 for the confirm surface. Reads the signal, evidence, and
    provenance stores itself so the caller needs only ``program_id`` + ``programs_root``.

    Returns an empty report when ``program_id`` is None (no program-scoped data).
    """
    if program_id is None:
        return QualityGateReport(results=())
    resolved_as_of = as_of or datetime.now(timezone.utc)
    signal_store = build_signal_store_for_program_id(program_id, programs_root=programs_root)
    journal_signals = signal_store.read(program_id, end=resolved_as_of)
    review_states = signal_store.read_reviews(program_id)

    pending_workiq = tuple(
        signal
        for signal in journal_signals
        if is_m365_signal(signal) and signal_needs_review(signal, review_states)
    )
    evidence_records = load_evidence_records(program_id, programs_root=programs_root)
    evidence = tuple(record.evidence for record in evidence_records)
    provenance_lane_ids = _load_provenance_lane_ids(program_id, programs_root)
    source_last_seen = _channel_last_seen(channel_states)

    return QualityGateReport(
        results=(
            evaluate_workiq_pending_signal_gate(pending_workiq_signals=pending_workiq),
            evaluate_workiq_evidence_presence_gate(evidence=evidence),
            evaluate_workiq_source_freshness_gate(
                source_last_seen=source_last_seen,
                thresholds_hours=source_thresholds_hours,
                as_of=resolved_as_of,
            ),
            evaluate_workiq_blurb_provenance_gate(
                evidence=evidence,
                provenance_lane_ids=provenance_lane_ids,
            ),
        )
    )


def _load_provenance_lane_ids(program_id: str, programs_root: Path) -> frozenset[str]:
    """Lane ids that have at least one EvidenceProvenanceRecord."""
    path = programs_root / program_id / "journal" / "evidence_provenance.jsonl"
    if not path.exists():
        return frozenset()
    lane_ids: set[str] = set()
    for record in read_jsonl_records(path):
        lane_id = record.get("lane_id")
        if isinstance(lane_id, str):
            lane_ids.add(lane_id)
    return frozenset(lane_ids)


def _channel_last_seen(
    channel_states: Mapping[str, Mapping[str, Any]] | None,
) -> dict[str, datetime | None]:
    """Best-effort extraction of per-source last-seen timestamps from gather channel state.

    Tolerates missing/unknown structures — returns an empty mapping (→ QG-WIQ-3 passes
    vacuously) rather than raising, so an unexpected channel-state shape never blocks confirm.
    """
    if not channel_states:
        return {}
    last_seen: dict[str, datetime | None] = {}
    for source_key in _DEFAULT_SOURCE_THRESHOLDS_HOURS:
        state = channel_states.get(source_key)
        if not isinstance(state, Mapping):
            continue
        value = state.get("last_seen") or state.get("as_of") or state.get("last_run")
        last_seen[source_key] = _coerce_datetime(value)
    return last_seen


def _coerce_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _coerce_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return _coerce_utc(value)
    if isinstance(value, str):
        try:
            return _coerce_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None