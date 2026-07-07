from __future__ import annotations

from types import SimpleNamespace
from datetime import datetime, timezone
from pathlib import Path

import yaml
import pytest
from src.ai.ai_mode import AIMode, set_ai_mode
from src.ai.blurb_generator import WorkstreamBlurb
from src.ai.exec_summary_drafter import ExecSummaryDraft
from src.core.models import Confidence

from src.commands.propose import generate_section_revision_proposals
from src.core.gather_state_store import write_gather_state
from src.core.section_proposal_store import load_proposals
from tests.unit.test_commands_report import _enable_v2_program_ai, _sample_items, _seed_v2_report_layout


def _empty_work_item_loader(*_args: object, **_kwargs: object) -> tuple[tuple[object, ...], int]:
    return (), 0


def test_generate_section_revision_proposals_writes_proposals_jsonl(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )

    proposals = load_proposals("acme", artifacts.issue_number, programs_root=reports_root.parent / "programs")

    assert artifacts.proposals_path is not None
    assert artifacts.proposals_path.exists()
    assert artifacts.proposal_count == len(proposals)
    assert any(proposal.section_id == "exec_summary" for proposal in proposals)
    assert all(proposal.proposed_text is None for proposal in proposals)
    assert all(proposal.evidence_brief.section_id == proposal.section_id for proposal in proposals)


def test_generate_section_revision_proposals_dry_run_skips_writing(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=True,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )

    assert artifacts.proposals_path is None
    assert artifacts.preview_lines
    assert any(line.startswith("[exec_summary]") for line in artifacts.preview_lines)
    proposals = load_proposals("acme", artifacts.issue_number, programs_root=reports_root.parent / "programs")
    assert proposals == ()


def test_generate_section_revision_proposals_writes_ai_text_when_enabled(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    _enable_v2_program_ai(reports_root.parent / "programs")

    artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        ai=True,
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        create_ai_client=lambda **_kwargs: SimpleNamespace(),
        draft_exec_summary_runner=lambda **_kwargs: ExecSummaryDraft(
            text="AI exec proposal.",
            prompt_version="exec_summary_drafter.v1",
            cited_work_item_ids=(),
            ai_confidence=Confidence.HIGH,
        ),
        generate_workstream_blurb_runner=lambda **kwargs: WorkstreamBlurb(
            text=f"AI proposal for {kwargs['workstream_name']}.",
            prompt_version="workstream_blurb.v1",
            cited_work_item_ids=(),
            ai_confidence=Confidence.HIGH,
        ),
    )

    proposals = load_proposals("acme", artifacts.issue_number, programs_root=reports_root.parent / "programs")
    exec_summary = next(proposal for proposal in proposals if proposal.section_id == "exec_summary")
    workstream_proposal = next(proposal for proposal in proposals if proposal.section_id != "exec_summary")

    assert exec_summary.proposed_text == "AI exec proposal."
    assert exec_summary.ai_model_used == "exec_summary_drafter.v1"
    if workstream_proposal.proposed_text is None:
        assert workstream_proposal.evidence_brief.confidence is Confidence.LOW
        assert any(
            f"AI skipped for {workstream_proposal.section_id}: insufficient evidence (confidence=low)." == warning
            for warning in artifacts.warnings
        )
    else:
        assert workstream_proposal.proposed_text.startswith("AI proposal for ")
        assert workstream_proposal.ai_model_used == "workstream_blurb.v1"


def test_generate_section_revision_proposals_ai_skips_low_confidence_sections(repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    _enable_v2_program_ai(reports_root.parent / "programs")

    original_assemble = generate_section_revision_proposals.__globals__["assemble_section_evidence_brief"]

    def _patched_assemble(section_id: str, *args: object, **kwargs: object):
        evidence = original_assemble(section_id, *args, **kwargs)
        if section_id == "exec_summary":
            return type(evidence)(
                section_id=evidence.section_id,
                ado_delta_summary=evidence.ado_delta_summary,
                new_items=evidence.new_items,
                closed_items=evidence.closed_items,
                risk_changed_items=evidence.risk_changed_items,
                eta_changed_items=evidence.eta_changed_items,
                top_signals=evidence.top_signals,
                kpi_summary=evidence.kpi_summary,
                stale_claims=evidence.stale_claims,
                vitality_summary=evidence.vitality_summary,
                confidence=Confidence.LOW,
            )
        return evidence

    monkeypatch.setitem(generate_section_revision_proposals.__globals__, "assemble_section_evidence_brief", _patched_assemble)

    artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        ai=True,
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
        create_ai_client=lambda **_kwargs: SimpleNamespace(),
        draft_exec_summary_runner=lambda **_kwargs: ExecSummaryDraft(
            text="should not be used",
            prompt_version="exec_summary_drafter.v1",
            cited_work_item_ids=(),
            ai_confidence=Confidence.HIGH,
        ),
        generate_workstream_blurb_runner=lambda **kwargs: WorkstreamBlurb(
            text=f"AI proposal for {kwargs['workstream_name']}.",
            prompt_version="workstream_blurb.v1",
            cited_work_item_ids=(),
            ai_confidence=Confidence.HIGH,
        ),
    )

    proposals = load_proposals("acme", artifacts.issue_number, programs_root=reports_root.parent / "programs")
    exec_summary = next(proposal for proposal in proposals if proposal.section_id == "exec_summary")

    assert exec_summary.proposed_text is None
    assert any("AI skipped for exec_summary" in warning for warning in artifacts.warnings)


def test_generate_section_revision_proposals_threads_signal_ranking_context(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    captured_calls: list[dict[str, object]] = []
    original_assemble = generate_section_revision_proposals.__globals__["assemble_section_evidence_brief"]

    def _capturing_assemble(section_id: str, *args: object, **kwargs: object):
        captured_calls.append(
            {
                "section_id": section_id,
                "people_directory": kwargs.get("people_directory"),
                "source_confidence_order": kwargs.get("source_confidence_order"),
            }
        )
        return original_assemble(section_id, *args, **kwargs)

    monkeypatch.setitem(generate_section_revision_proposals.__globals__, "assemble_section_evidence_brief", _capturing_assemble)

    artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=True,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )

    assert artifacts.proposal_count > 0
    assert captured_calls
    assert all(call["people_directory"] is not None for call in captured_calls)
    assert all(isinstance(call["source_confidence_order"], tuple) for call in captured_calls)


def test_generate_section_revision_proposals_ai_disabled_falls_back_to_evidence_only(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    program_path = reports_root.parent / "programs" / "acme" / "program.yaml"
    doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    doc.setdefault("ai", {})["enabled"] = False
    program_path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")

    artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        ai=True,
        dry_run=True,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=_empty_work_item_loader,
    )

    assert any("AI skipped: program AI is disabled" in warning for warning in artifacts.warnings)
    assert any("[exec_summary]" in line for line in artifacts.preview_lines)


def test_generate_section_revision_proposals_invocation_ai_disabled_falls_back_to_evidence_only(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    _enable_v2_program_ai(reports_root.parent / "programs")
    set_ai_mode(AIMode.DISABLED)
    try:
        artifacts = generate_section_revision_proposals(
            edition_name="acme_weekly",
            ai=True,
            dry_run=False,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=(tmp_path / "programs"),
            work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
            create_ai_client=lambda **_kwargs: (_ for _ in ()).throw(AssertionError("AI client should not be created when AIMode.DISABLED")),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    proposals = load_proposals("acme", artifacts.issue_number, programs_root=reports_root.parent / "programs")
    assert all(proposal.proposed_text is None for proposal in proposals)
    assert any("AI skipped: invocation AI is disabled" in warning for warning in artifacts.warnings)


def test_generate_section_revision_proposals_skips_ban_list_violating_exec_summary_text(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    _enable_v2_program_ai(reports_root.parent / "programs")

    artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        ai=True,
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        create_ai_client=lambda **_kwargs: SimpleNamespace(),
        draft_exec_summary_runner=lambda **_kwargs: ExecSummaryDraft(
            text="This week we improved coverage due to better follow-up.",
            prompt_version="exec_summary_drafter.v1",
            cited_work_item_ids=(),
            ai_confidence=Confidence.HIGH,
        ),
        generate_workstream_blurb_runner=lambda **kwargs: WorkstreamBlurb(
            text=f"AI proposal for {kwargs['workstream_name']}.",
            prompt_version="workstream_blurb.v1",
            cited_work_item_ids=(),
            ai_confidence=Confidence.HIGH,
        ),
    )

    proposals = load_proposals("acme", artifacts.issue_number, programs_root=reports_root.parent / "programs")
    exec_summary = next(proposal for proposal in proposals if proposal.section_id == "exec_summary")

    assert exec_summary.proposed_text is None
    assert any("AI skipped for exec_summary: generated text violates the editorial ban-list" in warning for warning in artifacts.warnings)


def test_generate_section_revision_proposals_skips_ban_list_violating_workstream_text(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    _enable_v2_program_ai(reports_root.parent / "programs")

    original_assemble = generate_section_revision_proposals.__globals__["assemble_section_evidence_brief"]

    def _patched_assemble(section_id: str, *args: object, **kwargs: object):
        evidence = original_assemble(section_id, *args, **kwargs)
        if section_id != "exec_summary":
            return type(evidence)(
                section_id=evidence.section_id,
                ado_delta_summary=evidence.ado_delta_summary,
                new_items=evidence.new_items,
                closed_items=evidence.closed_items,
                risk_changed_items=evidence.risk_changed_items,
                eta_changed_items=evidence.eta_changed_items,
                top_signals=evidence.top_signals,
                kpi_summary=evidence.kpi_summary,
                stale_claims=evidence.stale_claims,
                vitality_summary=evidence.vitality_summary,
                confidence=Confidence.MEDIUM,
            )
        return evidence

    monkeypatch.setitem(generate_section_revision_proposals.__globals__, "assemble_section_evidence_brief", _patched_assemble)

    artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        ai=True,
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        create_ai_client=lambda **_kwargs: SimpleNamespace(),
        draft_exec_summary_runner=lambda **_kwargs: ExecSummaryDraft(
            text="AI exec proposal.",
            prompt_version="exec_summary_drafter.v1",
            cited_work_item_ids=(),
            ai_confidence=Confidence.HIGH,
        ),
        generate_workstream_blurb_runner=lambda **kwargs: WorkstreamBlurb(
            text="This week we improved coverage due to better follow-up.",
            prompt_version="workstream_blurb.v1",
            cited_work_item_ids=(),
            ai_confidence=Confidence.HIGH,
        ),
    )

    proposals = load_proposals("acme", artifacts.issue_number, programs_root=reports_root.parent / "programs")
    workstream_proposal = next(proposal for proposal in proposals if proposal.section_id != "exec_summary")

    assert workstream_proposal.proposed_text is None
    assert any(
        f"AI skipped for {workstream_proposal.section_id}: generated text violates the editorial ban-list"
        in warning
        for warning in artifacts.warnings
    )


def test_generate_section_revision_proposals_warns_when_latest_gather_is_stale(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    write_gather_state(
        "acme",
        gathered_at=datetime(2026, 5, 8, 12, 0, tzinfo=timezone.utc),
        scanned_items=4,
        discovered_signals=2,
        new_signals=1,
        pending_review=1,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=3,
        archived_journal_files=0,
        background_proposals=0,
        programs_root=programs_root,
    )

    artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=True,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=_empty_work_item_loader,
    )

    assert any(
        "Last gather was 54 hours ago. Consider running 'vertex gather --program acme' before proposing to get fresh signals."
        in warning
        for warning in artifacts.warnings
    )


def test_generate_section_revision_proposals_ai_passes_workstream_evidence_bundle(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    _enable_v2_program_ai(reports_root.parent / "programs")
    bundle_calls: list[object] = []

    artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        ai=True,
        dry_run=True,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        create_ai_client=lambda **_kwargs: SimpleNamespace(),
        draft_exec_summary_runner=lambda **_kwargs: ExecSummaryDraft(
            text="AI exec proposal.",
            prompt_version="exec_summary_drafter.v1",
            cited_work_item_ids=(),
            ai_confidence=Confidence.HIGH,
        ),
        generate_workstream_blurb_runner=lambda **kwargs: (
            bundle_calls.append(kwargs.get("workstream_evidence_bundle"))
            or WorkstreamBlurb(
                text=f"AI proposal for {kwargs['workstream_name']}.",
                prompt_version="workstream_blurb.v1",
                cited_work_item_ids=(),
                ai_confidence=Confidence.HIGH,
            )
        ),
    )

    assert artifacts.proposal_count > 0
    assert bundle_calls
    assert all(bundle is None or hasattr(bundle, "lane_id") for bundle in bundle_calls)


def test_generate_section_revision_proposals_skips_gather_warning_when_recent(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    write_gather_state(
        "acme",
        gathered_at=datetime(2026, 5, 10, 6, 0, tzinfo=timezone.utc),
        scanned_items=4,
        discovered_signals=2,
        new_signals=1,
        pending_review=1,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=3,
        archived_journal_files=0,
        background_proposals=0,
        programs_root=programs_root,
    )

    artifacts = generate_section_revision_proposals(
        edition_name="acme_weekly",
        dry_run=True,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
        work_item_loader=_empty_work_item_loader,
    )

    assert not any("Last gather was" in warning for warning in artifacts.warnings)

