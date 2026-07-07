"""Guards the D-09 / Phase 3 peel of editorial/advisory gate helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core import quality_gates as quality_gates_module
from src.core.program_fact_store import FactPrecedence, FactReviewState, ProgramFactInput, ProgramFactStore
from src.core.quality_gates import editorial as editorial_module
from src.core.gather_state_store import write_gather_state
from src.core.ledger.candidate_store import CandidateEvent, append_candidate
from src.core.ledger.event_log import ConfidenceTier, TemporalConfidence, build_event_envelope, write_event
from src.core.ledger.program_views import project_program_events
from src.core.ledger.source_refs import LTDeckRef
from src.core.models import FreshnessReport


def test_package_private_email_gate_alias_points_to_editorial_module() -> None:
    assert quality_gates_module._evaluate_email_signal_coverage_gate is editorial_module.evaluate_email_signal_coverage_gate


def test_exec_summary_staleness_gate_skips_without_identifiers() -> None:
    result = editorial_module.evaluate_exec_summary_staleness_gate(None, None)

    assert result.gate_id == "QG-23"
    assert result.passed is True


def test_candidate_triage_latency_gate_passes_without_program_id() -> None:
    result = editorial_module.evaluate_candidate_triage_latency_gate(program_id=None)

    assert result.gate_id == "QG-DM-6"
    assert result.passed is True


def test_gap_detection_sla_gate_skips_without_program_id() -> None:
    result = editorial_module.evaluate_gap_detection_sla_gate(program_id=None)

    assert result.gate_id == "QG-DM-5"
    assert result.passed is True


def test_gap_detection_sla_gate_warns_when_active_channel_is_stale_without_gap_record(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "id: acme",
                "name: Acme",
                "reality:",
                "  expected_gather_cadence_hours: 24",
            )
        ),
        encoding="utf-8",
    )
    write_gather_state(
        "acme",
        gathered_at=datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc),
        scanned_items=0,
        discovered_signals=0,
        new_signals=0,
        pending_review=0,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=0,
        archived_journal_files=0,
        background_proposals=0,
        channels={"workiq": {"active": True}},
        programs_root=programs_root,
    )

    result = editorial_module.evaluate_gap_detection_sla_gate(
        program_id="acme",
        programs_root=programs_root,
        now=datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
    )

    assert result.gate_id == "QG-DM-5"
    assert result.passed is False
    assert "workiq" in result.message
    assert "no recent `pipeline.gap_detected.v1` record" in result.message


def test_gap_detection_sla_gate_passes_when_stale_channel_has_recent_gap_record(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                "schema_version: '3.0'",
                "id: acme",
                "name: Acme",
                "reality:",
                "  expected_gather_cadence_hours: 24",
            )
        ),
        encoding="utf-8",
    )
    write_gather_state(
        "acme",
        gathered_at=datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc),
        scanned_items=0,
        discovered_signals=0,
        new_signals=0,
        pending_review=0,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=0,
        archived_journal_files=0,
        background_proposals=0,
        channels={"workiq": {"active": True}},
        programs_root=programs_root,
    )
    gap_event = build_event_envelope(
        program_id="acme",
        event_type="pipeline.gap_detected.v1",
        occurred_at=datetime(2026, 5, 3, 10, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 5, 3, 10, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.OPERATOR_CONFIRMED,
        actor="workiq_pipeline",
        payload={"pipeline": "workiq", "gap_kind": "empty_yield", "detail": "no new threads"},
        source_ref=LTDeckRef(file_path="deck.pptx", deck_date=datetime(2026, 5, 3, 10, 0, tzinfo=timezone.utc).date(), slide_number=1),
    )
    write_event(gap_event, programs_root=programs_root)

    result = editorial_module.evaluate_gap_detection_sla_gate(
        program_id="acme",
        programs_root=programs_root,
        now=datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc),
    )

    assert result.gate_id == "QG-DM-5"
    assert result.passed is True
    assert "covered by recent pipeline gap records" in result.message


def test_unresolved_conflict_budget_gate_skips_without_program_id() -> None:
    result = editorial_module.evaluate_unresolved_conflict_budget_gate(program_id=None)

    assert result.gate_id == "QG-DM-7"
    assert result.passed is True


def test_unresolved_conflict_budget_gate_warns_on_open_material_fact_conflicts(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    ProgramFactStore("acme", db_root=programs_root.parent).append_fact(
        ProgramFactInput(
            fact_type="fact.conflict",
            natural_key="conflict:commitment:1",
            entity_refs=("COMMIT-1",),
            payload={
                "family": "commitment",
                "description": "ADO due date disagrees with Teams due date.",
                "resolved": False,
                "is_material": True,
            },
            precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
            review_state=FactReviewState.ACCEPTED,
        )
    )

    result = editorial_module.evaluate_unresolved_conflict_budget_gate(
        program_id="acme",
        programs_root=programs_root,
    )

    assert result.gate_id == "QG-DM-7"
    assert result.passed is False
    assert "commitment" in result.message
    assert "ADO due date disagrees with Teams due date." in result.message


def test_unresolved_conflict_budget_gate_ignores_resolved_or_non_material_conflicts(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    store = ProgramFactStore("acme", db_root=programs_root.parent)
    store.append_fact(
        ProgramFactInput(
            fact_type="fact.conflict",
            natural_key="conflict:minor:1",
            entity_refs=("WI:1",),
            payload={
                "family": "narrative",
                "description": "Minor wording disagreement.",
                "resolved": False,
                "is_material": False,
            },
            precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
            review_state=FactReviewState.ACCEPTED,
        )
    )
    store.append_fact(
        ProgramFactInput(
            fact_type="fact.conflict",
            natural_key="conflict:resolved:1",
            entity_refs=("WI:2",),
            payload={
                "family": "commitment",
                "description": "Resolved disagreement.",
                "resolved": True,
                "is_material": True,
            },
            precedence=FactPrecedence.VERIFIED_SYSTEM_SIGNAL,
            review_state=FactReviewState.ACCEPTED,
        )
    )

    result = editorial_module.evaluate_unresolved_conflict_budget_gate(
        program_id="acme",
        programs_root=programs_root,
    )

    assert result.gate_id == "QG-DM-7"
    assert result.passed is True


def test_candidate_triage_latency_gate_warns_on_stale_candidates(tmp_path: Path) -> None:
    append_candidate(
        CandidateEvent(
            candidate_id="cand-1",
            program_id="acme",
            proposed_event_type="risk.raised.v1",
            proposed_payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
            proposed_occurred_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            proposed_temporal_confidence="exact",
            proposed_confidence="source_authoritative",
            source_ref=LTDeckRef(file_path="deck.pptx", deck_date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc).date(), slide_number=3),
            pipeline="deck_backfill",
            extraction_confidence=0.9,
            entity_resolution=(),
            dedupe_key="dedupe-1",
            dedupe_core_hash="sha256:abc",
            source_document_key="deck:1",
            corroborating_refs=(),
            batch_id="batch-1",
            staged_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        ),
        programs_root=tmp_path / "programs",
    )

    result = editorial_module.evaluate_candidate_triage_latency_gate(
        program_id="acme",
        programs_root=tmp_path / "programs",
        now=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
    )

    assert result.gate_id == "QG-DM-6"
    assert result.passed is False
    assert "oldest staged 19 day(s) ago" in result.message


def test_projection_freshness_gate_warns_when_projection_watermark_lags(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    first = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=LTDeckRef(file_path="deck-a.pptx", deck_date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc).date(), slide_number=1),
    )
    second = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r2", "title": "Risk two", "severity": "high"},
        source_ref=LTDeckRef(file_path="deck-b.pptx", deck_date=datetime(2026, 5, 2, 12, 0, tzinfo=timezone.utc).date(), slide_number=2),
    )
    write_event(first, programs_root=programs_root)
    project_program_events("acme", programs_root=programs_root)
    write_event(second, programs_root=programs_root)

    result = editorial_module.evaluate_projection_freshness_gate(
        program_id="acme",
        programs_root=programs_root,
    )

    assert result.gate_id == "QG-DM-10"
    assert result.passed is False
    assert "lags ledger head" in result.message


def test_projection_freshness_gate_passes_when_projection_matches_head(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    event = build_event_envelope(
        program_id="acme",
        event_type="risk.raised.v1",
        occurred_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        recorded_at=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
        temporal_confidence=TemporalConfidence.EXACT,
        confidence=ConfidenceTier.SOURCE_AUTHORITATIVE,
        actor="import",
        payload={"risk_id": "risk:r1", "title": "Risk one", "severity": "high"},
        source_ref=LTDeckRef(file_path="deck.pptx", deck_date=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc).date(), slide_number=1),
    )
    write_event(event, programs_root=programs_root)
    project_program_events("acme", programs_root=programs_root)

    result = editorial_module.evaluate_projection_freshness_gate(
        program_id="acme",
        programs_root=programs_root,
    )

    assert result.gate_id == "QG-DM-10"
    assert result.passed is True


def test_metric_injection_gate_passes_without_program_id() -> None:
    result = editorial_module.evaluate_metric_injection_and_ado_hygiene_gate(
        program_id=None,
        narratives={},
    )

    assert result.gate_id == "QG-24"
    assert result.passed is True


def test_email_signal_gate_passes_when_program_id_not_provided() -> None:
    result = editorial_module.evaluate_email_signal_coverage_gate(
        channel_states=None,
        program_id=None,
    )

    assert result.gate_id == "QG-25"
    assert result.passed is True


def test_email_signal_gate_passes_when_signals_exist() -> None:
    result = editorial_module.evaluate_email_signal_coverage_gate(
        channel_states={"workiq": {"active": True, "email_signals": 5}},
        program_id="acme",
    )

    assert result.gate_id == "QG-25"
    assert result.passed is True


def test_phase_1b_gates_continue_to_use_editorial_helpers(monkeypatch) -> None:
    monkeypatch.setattr(
        quality_gates_module,
        "_evaluate_gap_detection_sla_gate",
        lambda **kwargs: quality_gates_module.GateEvaluation(
            gate_id="QG-DM-5",
            passed=True,
            message="gap ok",
            exit_code=1,
            forceable=True,
        ),
    )
    monkeypatch.setattr(
        quality_gates_module,
        "_evaluate_unresolved_conflict_budget_gate",
        lambda **kwargs: quality_gates_module.GateEvaluation(
            gate_id="QG-DM-7",
            passed=True,
            message="conflicts ok",
            exit_code=1,
            forceable=True,
        ),
    )
    monkeypatch.setattr(
        quality_gates_module,
        "_evaluate_candidate_triage_latency_gate",
        lambda **kwargs: quality_gates_module.GateEvaluation(
            gate_id="QG-DM-6",
            passed=False,
            message="triage stale",
            exit_code=1,
            forceable=True,
        ),
    )
    monkeypatch.setattr(
        quality_gates_module,
        "_evaluate_projection_freshness_gate",
        lambda **kwargs: quality_gates_module.GateEvaluation(
            gate_id="QG-DM-10",
            passed=True,
            message="projection ok",
            exit_code=1,
            forceable=True,
        ),
    )
    monkeypatch.setattr(
        quality_gates_module,
        "_evaluate_exec_summary_staleness_gate",
        lambda edition_name, issue_number: quality_gates_module.GateEvaluation(
            gate_id="QG-23",
            passed=False,
            message="stale",
            exit_code=1,
            forceable=True,
        ),
    )
    monkeypatch.setattr(
        quality_gates_module,
        "_evaluate_metric_injection_and_ado_hygiene_gate",
        lambda **kwargs: quality_gates_module.GateEvaluation(
            gate_id="QG-24",
            passed=True,
            message="metric ok",
            exit_code=0,
            forceable=True,
        ),
    )
    monkeypatch.setattr(
        quality_gates_module,
        "_evaluate_email_signal_coverage_gate",
        lambda **kwargs: quality_gates_module.GateEvaluation(
            gate_id="QG-25",
            passed=True,
            message="email ok",
            exit_code=0,
            forceable=True,
        ),
    )

    report = quality_gates_module.evaluate_phase_1b_gates(
        freshness_report=FreshnessReport(issue_number=78, items=(), blocks=0, warns=0, infos=0),
        edition_name="acme_weekly",
        issue_number=78,
        program_id="acme",
        channel_states={"workiq": {"active": False}},
        as_of=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
    )

    assert report.qg_results["QG-23"] is False
    assert report.qg_results["QG-24"] is True
    assert report.qg_results["QG-25"] is True
    assert report.qg_results["QG-DM-5"] is True
    assert report.qg_results["QG-DM-7"] is True
    assert report.qg_results["QG-DM-6"] is False
    assert report.qg_results["QG-DM-10"] is True
