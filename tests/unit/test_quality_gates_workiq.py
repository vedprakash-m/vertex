"""Unit tests for WorkIQ / M365 enrichment quality gates (QG-WIQ-1 … QG-WIQ-9).

Newsletter-WorkIQ spec §14.1. Each gate is a pure function; these tests cover the
pass/fail branches, forceable semantics, and the ``evaluate_workiq_confirm_gates``
convenience reader.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.evidence_models import EtaRecord, SourceRef, WorkstreamEvidence
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import Signal, TeamsMeetingSeries
from src.core.quality_gates.models import QualityGateReport
from src.core.quality_gates.workiq import (
    evaluate_workiq_blurb_provenance_gate,
    evaluate_workiq_budget_gate,
    evaluate_workiq_confirm_gates,
    evaluate_workiq_evidence_presence_gate,
    evaluate_workiq_latest_divergence_gate,
    evaluate_workiq_pending_signal_gate,
    evaluate_workiq_signal_recency_gate,
    evaluate_workiq_source_freshness_gate,
    evaluate_workiq_transcript_extraction_block_gate,
    evaluate_workiq_transcript_identifier_gate,
    is_m365_signal,
)

_AS_OF = datetime(2026, 6, 18, 12, 0, tzinfo=timezone.utc)


def _ev(*, lane_id: str = "lane-a", confidence: float = 0.8, synthesized_at: datetime | None = None) -> WorkstreamEvidence:
    return WorkstreamEvidence(
        lane_id=lane_id,
        synthesized_at=synthesized_at or _AS_OF,
        risk_level=RiskLevel.MEDIUM,
        etas=(),
        blocking_items=(),
        owners=(),
        source_refs=(SourceRef(source_type="workiq_email", description="x", source_date=None, author=None),),
        raw_excerpts=(),
        confidence=confidence,
        narrative_summary="narrative",
        stale_after=None,
    )


def _sig(*, id: str = "s1", source: str = "workiq/email") -> Signal:
    return Signal(
        id=id,
        timestamp=_AS_OF,
        source=source,
        program_id="acme",
        workstream_id="lane-a",
        entity_refs=(),
        text="text",
        raw_ref="raw",
        confidence=Confidence.MEDIUM,
        metadata={},
        review_policy=None,
    )


# ── is_m365_signal ─────────────────────────────────────────────────────────────


def test_is_m365_signal_classifies_namespace() -> None:
    assert is_m365_signal(_sig(id="a", source="workiq/email"))
    assert is_m365_signal(_sig(id="b", source="teams"))
    assert is_m365_signal(_sig(id="c", source="transcript"))
    assert not is_m365_signal(_sig(id="d", source="ado/comment"))
    assert not is_m365_signal(_sig(id="e", source="kusto"))


# ── QG-WIQ-1 ───────────────────────────────────────────────────────────────────


def test_qg_wiq_1_blocks_when_pending() -> None:
    gate = evaluate_workiq_pending_signal_gate(pending_workiq_signals=[_sig(id="wq-1")])
    assert gate.gate_id == "QG-WIQ-1"
    assert gate.passed is False
    assert gate.forceable is False
    assert gate.exit_code == 3
    assert "wq-1" in gate.message


def test_qg_wiq_1_passes_when_empty() -> None:
    gate = evaluate_workiq_pending_signal_gate(pending_workiq_signals=[])
    assert gate.passed is True
    assert gate.forceable is False


# ── QG-WIQ-2 ───────────────────────────────────────────────────────────────────


def test_qg_wiq_2_warns_when_no_confident_evidence() -> None:
    gate = evaluate_workiq_evidence_presence_gate(evidence=[_ev(confidence=0.0)])
    assert gate.passed is False
    assert gate.forceable is True
    assert gate.exit_code == 1


def test_qg_wiq_2_passes_with_confident_evidence() -> None:
    gate = evaluate_workiq_evidence_presence_gate(evidence=[_ev(confidence=0.8)])
    assert gate.passed is True
    assert gate.forceable is True


# ── QG-WIQ-3 ───────────────────────────────────────────────────────────────────


def test_qg_wiq_3_warns_when_source_stale() -> None:
    stale_icm = _AS_OF - timedelta(hours=13)  # > 12h threshold
    gate = evaluate_workiq_source_freshness_gate(
        source_last_seen={"icm": stale_icm, "teams": _AS_OF},
        as_of=_AS_OF,
    )
    assert gate.passed is False
    assert gate.forceable is True
    assert "icm" in gate.message


def test_qg_wiq_3_warns_when_source_never_seen() -> None:
    gate = evaluate_workiq_source_freshness_gate(
        source_last_seen={"kusto": None},
        as_of=_AS_OF,
    )
    assert gate.passed is False
    assert "kusto=never" in gate.message


def test_qg_wiq_3_passes_when_within_threshold() -> None:
    fresh_icm = _AS_OF - timedelta(hours=5)
    gate = evaluate_workiq_source_freshness_gate(
        source_last_seen={"icm": fresh_icm},
        as_of=_AS_OF,
    )
    assert gate.passed is True


def test_qg_wiq_3_respects_custom_thresholds() -> None:
    gate = evaluate_workiq_source_freshness_gate(
        source_last_seen={"icm": _AS_OF - timedelta(hours=1)},
        thresholds_hours={"icm": 0},  # any age > 0h is stale
        as_of=_AS_OF,
    )
    assert gate.passed is False


# ── QG-WIQ-4 ───────────────────────────────────────────────────────────────────


def test_qg_wiq_4_info_within_budget() -> None:
    gate = evaluate_workiq_budget_gate(cost_usd=0.40, budget_usd_per_run=1.00)
    assert gate.passed is True
    assert gate.exit_code == 0


def test_qg_wiq_4_info_over_threshold_still_passes() -> None:
    gate = evaluate_workiq_budget_gate(cost_usd=0.90, budget_usd_per_run=1.00)
    assert gate.passed is True  # info gate never blocks
    assert "over 80%" in gate.message


def test_qg_wiq_4_skips_when_no_budget() -> None:
    gate = evaluate_workiq_budget_gate(cost_usd=0.5, budget_usd_per_run=0.0)
    assert gate.passed is True
    assert "skipped" in gate.message


# ── QG-WIQ-5 / QG-WIQ-8 ─────────────────────────────────────────────────────────


def _series(*, name: str, series_id: str | None = None, calendar_name: str | None = None, include: bool = True) -> TeamsMeetingSeries:
    return TeamsMeetingSeries(
        display_name=name,
        series_id=series_id,
        include_transcripts=include,
        calendar_name=calendar_name,
    )


def test_qg_wiq_5_warns_on_unidentified_series() -> None:
    gate = evaluate_workiq_transcript_identifier_gate(
        meeting_series=[_series(name="Acme Weekly", series_id=None, calendar_name=None)],
    )
    assert gate.passed is False
    assert "Acme Weekly" in gate.message


def test_qg_wiq_5_passes_when_calendar_name_set() -> None:
    gate = evaluate_workiq_transcript_identifier_gate(
        meeting_series=[_series(name="Acme Weekly", series_id=None, calendar_name="Acme Weekly Ops Review")],
    )
    assert gate.passed is True


def test_qg_wiq_5_passes_when_series_id_set() -> None:
    gate = evaluate_workiq_transcript_identifier_gate(
        meeting_series=[_series(name="Acme Weekly", series_id="series-7", calendar_name=None)],
    )
    assert gate.passed is True


def test_qg_wiq_5_ignores_transcripts_disabled_series() -> None:
    gate = evaluate_workiq_transcript_identifier_gate(
        meeting_series=[_series(name="Acme Weekly", series_id=None, calendar_name=None, include=False)],
    )
    assert gate.passed is True


def test_qg_wiq_8_blocks_extraction_on_triple_null() -> None:
    gate = evaluate_workiq_transcript_extraction_block_gate(
        meeting_series=[_series(name="Acme Weekly", series_id=None, calendar_name=None)],
    )
    assert gate.passed is False
    assert "blocked" in gate.message.lower()


def test_qg_wiq_8_passes_when_identifier_present() -> None:
    gate = evaluate_workiq_transcript_extraction_block_gate(
        meeting_series=[_series(name="Acme Weekly", series_id="series-7")],
    )
    assert gate.passed is True


# ── QG-WIQ-6 ───────────────────────────────────────────────────────────────────


def test_qg_wiq_6_warns_when_no_m365_signals() -> None:
    gate = evaluate_workiq_signal_recency_gate(m365_signals=[_sig(id="a", source="ado")])
    assert gate.passed is False


def test_qg_wiq_6_passes_with_m365_signals() -> None:
    gate = evaluate_workiq_signal_recency_gate(m365_signals=[_sig(id="a", source="teams")])
    assert gate.passed is True


# ── QG-WIQ-7 ───────────────────────────────────────────────────────────────────


def test_qg_wiq_7_warns_on_unprovenanced_confident_evidence() -> None:
    gate = evaluate_workiq_blurb_provenance_gate(
        evidence=[_ev(lane_id="lane-a", confidence=0.8)],
        provenance_lane_ids=frozenset({"lane-b"}),
    )
    assert gate.passed is False
    assert gate.forceable is True
    assert "lane-a" in gate.message


def test_qg_wiq_7_passes_when_provenance_present() -> None:
    gate = evaluate_workiq_blurb_provenance_gate(
        evidence=[_ev(lane_id="lane-a", confidence=0.8)],
        provenance_lane_ids=frozenset({"lane-a"}),
    )
    assert gate.passed is True


def test_qg_wiq_7_ignores_placeholder_evidence() -> None:
    gate = evaluate_workiq_blurb_provenance_gate(
        evidence=[_ev(lane_id="lane-a", confidence=0.0)],
        provenance_lane_ids=frozenset(),
    )
    assert gate.passed is True


# ── QG-WIQ-9 ───────────────────────────────────────────────────────────────────


def test_qg_wiq_9_warns_when_display_ahead_of_synthesis() -> None:
    # workiq_latest dated 2 days after the evidence synthesis → divergence.
    workiq_latest = {"lane-a": "2026-06-20 Acme narrative"}
    evidence = {"lane-a": _ev(lane_id="lane-a", synthesized_at=_AS_OF)}
    gate = evaluate_workiq_latest_divergence_gate(
        workiq_latest_by_lane=workiq_latest,
        evidence_by_lane=evidence,
        as_of=_AS_OF,
    )
    assert gate.passed is False
    assert "lane-a" in gate.message


def test_qg_wiq_9_passes_when_consistent() -> None:
    workiq_latest = {"lane-a": "2026-06-18 Acme narrative"}  # same day as _AS_OF synthesis
    evidence = {"lane-a": _ev(lane_id="lane-a", synthesized_at=_AS_OF)}
    gate = evaluate_workiq_latest_divergence_gate(
        workiq_latest_by_lane=workiq_latest,
        evidence_by_lane=evidence,
        as_of=_AS_OF,
    )
    assert gate.passed is True


def test_qg_wiq_9_skips_lanes_without_workiq_latest() -> None:
    gate = evaluate_workiq_latest_divergence_gate(
        workiq_latest_by_lane={"lane-a": None},
        evidence_by_lane={"lane-a": _ev(lane_id="lane-a")},
        as_of=_AS_OF,
    )
    assert gate.passed is True


# ── evaluate_workiq_confirm_gates (convenience reader) ─────────────────────────


def test_confirm_gates_empty_when_no_program(tmp_path: Path) -> None:
    report = evaluate_workiq_confirm_gates(program_id=None, programs_root=tmp_path)
    assert isinstance(report, QualityGateReport)
    assert report.results == ()


def test_confirm_gates_reads_stores_and_assembles_four(tmp_path: Path) -> None:
    program_id = "test-program"
    program_dir = tmp_path / program_id / "journal"
    program_dir.mkdir(parents=True)
    # No evidence, no provenance, no signals → QG-WIQ-1 pass, QG-WIQ-2 warn (forceable),
    # QG-WIQ-3 pass (no channel state), QG-WIQ-7 pass (no confident evidence).
    report = evaluate_workiq_confirm_gates(program_id=program_id, programs_root=tmp_path)
    gate_ids = [g.gate_id for g in report.results]
    assert gate_ids == ["QG-WIQ-1", "QG-WIQ-2", "QG-WIQ-3", "QG-WIQ-7"]
    # QG-WIQ-2 warns with no evidence but is forceable.
    assert report.results[1].gate_id == "QG-WIQ-2"
    assert report.results[1].passed is False
    assert report.results[1].forceable is True


def test_confirm_gates_channel_states_extracted(tmp_path: Path) -> None:
    program_id = "test-program"
    (tmp_path / program_id / "journal").mkdir(parents=True)
    stale = (_AS_OF - timedelta(hours=20)).isoformat()
    report = evaluate_workiq_confirm_gates(
        program_id=program_id,
        programs_root=tmp_path,
        channel_states={"icm": {"last_seen": stale}, "teams": {"last_seen": _AS_OF.isoformat()}},
        as_of=_AS_OF,
    )
    wiq3 = next(g for g in report.results if g.gate_id == "QG-WIQ-3")
    assert wiq3.passed is False  # icm 20h > 12h
    assert "icm" in wiq3.message