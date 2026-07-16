from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from src.commands import report_ai
from src.core.evidence_models import SourceRef, WorkstreamEvidence
from src.core.exceptions import ConfigError
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import Signal
from src.core.models_v2 import WorkstreamEvidenceBundle


def test_missing_report_ai_deployment_warning_for_exec_summary_is_vertex_first() -> None:
    warning = report_ai._missing_report_ai_deployment_warning("exec summary")

    assert "VERTEX_EXEC_DEPLOYMENT, VERTEX_AI_DEPLOYMENT, or AZURE_OPENAI_DEPLOYMENT" in warning
    assert "configure one of the supported Vertex deployment aliases" in warning


def test_missing_report_ai_deployment_warning_for_section_is_vertex_first() -> None:
    warning = report_ai._missing_report_ai_deployment_warning("section networking")

    assert "VERTEX_AI_DEPLOYMENT or AZURE_OPENAI_DEPLOYMENT" in warning
    assert "configure one of the supported Vertex deployment aliases" in warning


def test_load_report_signal_context_ignores_unrelated_action_loader_failures(monkeypatch, tmp_path) -> None:
    reports_root = tmp_path / "reports"
    reports_root.mkdir(parents=True)
    (tmp_path / "programs" / "demo").mkdir(parents=True)

    bundle = SimpleNamespace(config=SimpleNamespace(ado=SimpleNamespace(date_window_days=7)))
    resolved = SimpleNamespace(
        program=SimpleNamespace(id="demo"),
        edition=SimpleNamespace(workstream_filter=(), altitude="weekly"),
        workstreams=(),
        scorecards=(),
    )

    class _SignalStore:
        def read(self, *args, **kwargs):
            return ()

        def read_reviews(self, *args, **kwargs):
            return {}

    def _boom(*args, **kwargs):
        raise ConfigError("actions broken")

    monkeypatch.setattr(report_ai, "load_bundle_with_mode", lambda *args, **kwargs: SimpleNamespace(mode="v2"))
    monkeypatch.setattr(report_ai, "resolve_edition", lambda *args, **kwargs: resolved)
    monkeypatch.setattr(report_ai, "filter_workstreams", lambda workstreams, _: workstreams)
    monkeypatch.setattr(report_ai, "build_signal_store_for_program_id", lambda *args, **kwargs: _SignalStore())
    monkeypatch.setattr(report_ai, "load_program_knowledge", lambda *args, **kwargs: SimpleNamespace(people_directory=()))
    monkeypatch.setattr(report_ai, "detect_dependency_cascades", lambda **kwargs: ())
    monkeypatch.setattr("src.core.action_tracker.load_actions", _boom)

    context = report_ai._load_report_signal_context(
        edition_name="demo_weekly",
        bundle=bundle,
        items=(),
        as_of=datetime(2026, 5, 31, tzinfo=timezone.utc),
        previous_snapshot=None,
        reports_root=reports_root,
        item_trajectory_points=lambda *args, **kwargs: (),
    )

    assert context is not None
    assert context.program_id == "demo"
    assert context.dependency_cascades == ()


def test_signal_context_lines_use_signal_class_ranking_when_ai_context_available() -> None:
    now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    recent_status = Signal(
        id="status",
        timestamp=datetime(2026, 5, 31, 11, 59, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="demo",
        workstream_id="ws1",
        entity_refs=("WI:1",),
        text="Status update: rollout remains on track.",
        raw_ref="msg-status",
        confidence=Confidence.MEDIUM,
        metadata={"sender_alias": "owner"},
    )
    older_decision = Signal(
        id="decision",
        timestamp=datetime(2026, 5, 31, 11, 30, tzinfo=timezone.utc),
        source="workiq/email",
        program_id="demo",
        workstream_id="ws1",
        entity_refs=("WI:1",),
        text="Decision: leadership approved the rollout.",
        raw_ref="msg-decision",
        confidence=Confidence.MEDIUM,
        metadata={"sender_alias": "owner"},
    )

    lines = report_ai._signal_context_lines(
        (recent_status, older_decision),
        item_ids=set(),
        workstream_ids=(),
        limit=1,
        as_of=now,
        source_confidence_order=("workiq",),
    )

    assert lines == ("Approved signal 2026-05-31T11:30:00+00:00 [workiq/email]: Decision: leadership approved the rollout.",)


def test_feedback_context_lines_thread_workiq_email_feedback_with_subject_and_sender() -> None:
    lines = report_ai._feedback_context_lines(
        (
            Signal(
                id="feedback-1",
                timestamp=datetime(2026, 5, 31, 11, 59, tzinfo=timezone.utc),
                source="workiq/email",
                program_id="demo",
                workstream_id="ws1",
                entity_refs=("WI:1",),
                text="Please call out the blocker explicitly in next week's summary.",
                raw_ref="msg-feedback-1",
                confidence=Confidence.MEDIUM,
                metadata={"sender_alias": "gm", "subject": "Re: Leadership feedback digest"},
                thread_id="thread-feedback",
            ),
            Signal(
                id="feedback-2",
                timestamp=datetime(2026, 5, 30, 9, 0, tzinfo=timezone.utc),
                source="workiq/email",
                program_id="demo",
                workstream_id="ws1",
                entity_refs=("WI:1",),
                text="Can you confirm whether the fallback date moved?",
                raw_ref="msg-feedback-2",
                confidence=Confidence.MEDIUM,
                metadata={"sender_alias": "alex", "subject": "Re: Leadership feedback digest"},
                thread_id="thread-feedback",
            ),
        ),
        item_ids=set(),
        workstream_ids=(),
        limit=2,
        as_of=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
        source_confidence_order=("workiq",),
    )

    assert lines == (
        "Approved feedback thread thread-feedback [subject=Re: Leadership feedback digest; latest_sender=gm]: "
        "2026-05-31 gm: Please call out the blocker explicitly in next week's summary. | "
        "2026-05-30 alex: Can you confirm whether the fallback date moved?",
    )


def test_exec_ai_context_lines_include_feedback_context_for_next_issue(tmp_path) -> None:
    ai_context = report_ai._DraftAIContext(
        program_id="demo",
        programs_root=tmp_path,
        workstreams=(),
        rolling_summaries={},
        approved_signals=(
            Signal(
                id="feedback-1",
                timestamp=datetime(2026, 5, 31, 11, 59, tzinfo=timezone.utc),
                source="workiq/email",
                program_id="demo",
                workstream_id="ws1",
                entity_refs=("WI:1",),
                text="Please acknowledge the blocker question in the next issue.",
                raw_ref="msg-feedback-1",
                confidence=Confidence.MEDIUM,
                metadata={"sender_alias": "gm", "subject": "Re: Leadership feedback digest"},
                thread_id="thread-feedback",
            ),
        ),
        drift_patterns=(),
        dependency_cascades=(),
        as_of=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
    )

    lines = report_ai._exec_ai_context_lines(None, ai_context)

    assert any(line.startswith("Approved feedback thread thread-feedback") for line in lines)
    assert any("Please acknowledge the blocker question in the next issue." in line for line in lines)


def test_program_synthesis_exec_summary_context_lines_covers_three_categories(monkeypatch, tmp_path) -> None:
    # ADF-W2.9: supplements the exec summary's narrow WorkItem-delta view
    # with strategic risk / contradiction / critical-path milestone lines
    # already assembled by assemble_program_synthesis_request.
    from src.core.program_synthesis import ProgramSynthesisRequest, SynthesisInputItem

    request = ProgramSynthesisRequest(
        program_id="demo",
        as_of=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
        items=(
            SynthesisInputItem(category="strategic_risk", item_id="risk-1", summary="Vendor delay risk.", severity="high"),
            SynthesisInputItem(category="contradiction", item_id="conf-1", summary="Milestone date disagreement."),
            SynthesisInputItem(category="critical_path_milestone", item_id="ms-1", summary="M1 code complete | status=at_risk"),
            SynthesisInputItem(category="kusto_slo_breach", item_id="slo-1", summary="Latency SLO breached."),
        ),
    )
    monkeypatch.setattr(
        "src.core.program_synthesis.assemble_program_synthesis_request",
        lambda program_id, **kwargs: request,
    )

    lines = report_ai._program_synthesis_exec_summary_context_lines("demo", tmp_path)

    assert any("Vendor delay risk." in line and "[high]" in line for line in lines)
    assert any("Milestone date disagreement." in line for line in lines)
    assert any("M1 code complete" in line for line in lines)
    # kusto_slo_breach is deliberately not surfaced in this pass.
    assert not any("Latency SLO breached." in line for line in lines)


def test_program_synthesis_exec_summary_context_lines_caps_per_category(monkeypatch, tmp_path) -> None:
    from src.core.program_synthesis import ProgramSynthesisRequest, SynthesisInputItem

    request = ProgramSynthesisRequest(
        program_id="demo",
        as_of=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
        items=tuple(
            SynthesisInputItem(category="strategic_risk", item_id=f"risk-{i}", summary=f"Risk {i}.")
            for i in range(10)
        ),
    )
    monkeypatch.setattr(
        "src.core.program_synthesis.assemble_program_synthesis_request",
        lambda program_id, **kwargs: request,
    )

    lines = report_ai._program_synthesis_exec_summary_context_lines("demo", tmp_path, limit=4)

    assert len(lines) == 4


def test_program_synthesis_exec_summary_context_lines_degrades_on_failure(monkeypatch, tmp_path) -> None:
    # Best-effort: a failure in the underlying accessor must never break
    # exec-summary generation, only degrade to no supplemental lines.
    def _raise(program_id, **kwargs):
        raise RuntimeError("fact store unavailable")

    monkeypatch.setattr("src.core.program_synthesis.assemble_program_synthesis_request", _raise)

    lines = report_ai._program_synthesis_exec_summary_context_lines("demo", tmp_path)

    assert lines == ()


def test_program_synthesis_blurb_context_lines_scopes_by_workstream(monkeypatch) -> None:
    # ADF-W2.9: workstream-scoped analog of the exec-summary enrichment --
    # a risk/milestone linked to a DIFFERENT workstream must not leak into
    # this section's blurb context.
    from src.core.models_v2 import RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus
    from src.core.program_reality import ProgramReality, RealityConflict

    def _risk(risk_id: str, *, workstream_ids: tuple[str, ...]) -> RiskEntry:
        return RiskEntry(
            id=risk_id, program_id="demo", title=f"Risk {risk_id}", description="Something at risk.",
            probability=RiskProbability.LIKELY, impact=RiskImpact.HIGH, category=RiskCategory.EXTERNAL,
            owner_alias="alice", mitigation_plan=None, mitigation_due_date=None,
            linked_workstream_ids=workstream_ids, linked_work_item_ids=(), linked_milestone_ids=(),
            linked_claim_ids=(), linked_action_ids=(), status=RiskStatus.OPEN,
            identified_date=date(2026, 1, 1), identified_in_vertex_issue=None, last_reviewed_date=None, entity_refs=(),
        )

    matched_risk = SimpleNamespace(record=_risk("risk-in-scope", workstream_ids=("ws-deployment",)), fact_id="fact-risk-1")
    unmatched_risk = SimpleNamespace(record=_risk("risk-out-of-scope", workstream_ids=("ws-other",)), fact_id="fact-risk-2")

    matched_milestone = SimpleNamespace(
        record=SimpleNamespace(name="M1", status="at_risk", target_date=None, linked_workstream_ids=("ws-deployment",)),
        fact_id="fact-1",
    )
    unmatched_milestone = SimpleNamespace(
        record=SimpleNamespace(name="M2", status="on_track", target_date=None, linked_workstream_ids=("ws-other",)),
        fact_id="fact-2",
    )
    matched_conflict = RealityConflict(
        conflict_id="conf-1", entity_refs=("WI:101",), family="icm_vs_evidence_risk", open=True, description="In-scope disagreement.",
    )
    unmatched_conflict = RealityConflict(
        conflict_id="conf-2", entity_refs=("WI:999",), family="icm_vs_evidence_risk", open=True, description="Out-of-scope disagreement.",
    )
    mock_reality = SimpleNamespace(
        risks=lambda: (matched_risk, unmatched_risk),
        milestones=lambda: (matched_milestone, unmatched_milestone),
        conflicts=lambda open_only=True: (matched_conflict, unmatched_conflict),
    )
    monkeypatch.setattr(ProgramReality, "load", lambda program_id, **kwargs: mock_reality)

    lines = report_ai._program_synthesis_blurb_context_lines(
        "demo", Path("unused"), workstream_ids=("ws-deployment",), item_ids={101},
    )

    assert any("risk-in-scope" in line or "Risk risk-in-scope" in line for line in lines)
    assert not any("risk-out-of-scope" in line for line in lines)
    assert any("M1" in line for line in lines)
    assert not any("M2" in line for line in lines)
    assert any("In-scope disagreement." in line for line in lines)
    assert not any("Out-of-scope disagreement." in line for line in lines)


def test_program_synthesis_blurb_context_lines_degrades_on_failure() -> None:
    lines = report_ai._program_synthesis_blurb_context_lines(
        "demo", Path("/does/not/exist"), workstream_ids=("ws-1",), item_ids={101},
    )
    assert lines == ()


def test_program_synthesis_blurb_context_lines_empty_scope_short_circuits() -> None:
    # No workstream_ids AND no item_ids -- nothing could ever match, so skip
    # the accessor calls entirely rather than doing pointless work.
    lines = report_ai._program_synthesis_blurb_context_lines(
        "demo", Path("unused"), workstream_ids=(), item_ids=set(),
    )
    assert lines == ()


def test_build_workstream_source_footnote_renders_source_dates_and_dedups() -> None:
    bundle = WorkstreamEvidenceBundle(
        lane_id="ws1",
        ado_signals=(
            Signal(
                id="ado-1",
                timestamp=datetime(2026, 6, 18, 18, 0, tzinfo=timezone.utc),
                source="ado",
                program_id="demo",
                workstream_id="ws1",
                entity_refs=(),
                text="ADO status refreshed.",
                raw_ref="ado:1",
                confidence=Confidence.HIGH,
            ),
        ),
        kusto_metrics=(
            Signal(
                id="kusto-1",
                timestamp=datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc),
                source="kusto_kpi",
                program_id="demo",
                workstream_id="ws1",
                entity_refs=(),
                text="Error budget at 92%.",
                raw_ref="kusto:1",
                confidence=Confidence.HIGH,
            ),
        ),
        icm_blockers=(),
        ado_comments=(),
        m365_evidence=WorkstreamEvidence(
            lane_id="ws1",
            synthesized_at=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
            risk_level=RiskLevel.MEDIUM,
            etas=(),
            blocking_items=(),
            owners=("owner@example.com",),
            source_refs=(
                SourceRef(
                    source_type="workiq_transcript",
                    description="Acme Weekly standup transcript",
                    source_date=date(2026, 6, 16),
                    author="Owner",
                ),
                SourceRef(
                    source_type="workiq_transcript",
                    description="Acme Weekly standup transcript",
                    source_date=date(2026, 6, 16),
                    author="Owner",
                ),
            ),
            raw_excerpts=(),
            confidence=0.88,
            narrative_summary="Blocked on validation.",
        ),
        freshness_by_source={
            "ado": datetime(2026, 6, 18, 18, 0, tzinfo=timezone.utc),
            "kusto": datetime(2026, 6, 17, 18, 0, tzinfo=timezone.utc),
            "m365": datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
        },
    )

    footnote = report_ai._build_workstream_source_footnote(
        bundle,
        bundle.m365_evidence.source_refs,
    )

    assert footnote == (
        "Signal sources: ADO tracking (2026-06-18); "
        "Acme Weekly standup transcript (2026-06-16); "
        "Kusto telemetry (2026-06-17)."
    )


def test_boost_evidence_confidence_from_corroboration_raises_transient_confidence() -> None:
    evidence = WorkstreamEvidence(
        lane_id="ws1",
        synthesized_at=datetime(2026, 6, 18, 9, 0, tzinfo=timezone.utc),
        risk_level=RiskLevel.MEDIUM,
        etas=(),
        blocking_items=("ADO:123456",),
        owners=("owner@example.com",),
        source_refs=(),
        raw_excerpts=("Blocked on burn-in completion.",),
        confidence=0.62,
        narrative_summary="Deployment remains blocked on burn-in sign-off.",
    )

    boosted, notes = report_ai._boost_evidence_confidence_from_corroboration(
        evidence,
        ado_signals=(),
        ado_comments=(
            Signal(
                id="ado/comment/1",
                timestamp=datetime(2026, 6, 18, 10, 0, tzinfo=timezone.utc),
                source="ado/comment",
                program_id="demo",
                workstream_id="ws1",
                entity_refs=(),
                text="Blocked until burn-in sign-off lands.",
                raw_ref="ado:comment:1",
                confidence=Confidence.HIGH,
            ),
        ),
        kusto_metrics=(
            Signal(
                id="kusto/1",
                timestamp=datetime(2026, 6, 18, 11, 0, tzinfo=timezone.utc),
                source="kusto",
                program_id="demo",
                workstream_id="ws1",
                entity_refs=(),
                text="Deployment blocked in burn-in queue this week.",
                raw_ref="kusto:1",
                confidence=Confidence.HIGH,
            ),
        ),
        icm_blockers=(),
    )

    assert boosted.confidence == 0.82
    assert notes == (
        "blocked agreement across m365, ado, kusto raised confidence to 0.82",
    )
