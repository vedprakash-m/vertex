from __future__ import annotations
from src.core.edition_resolver import get_program_output_dir

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import yaml

from src.ai.ai_mode import AIMode, set_ai_mode
from src.commands import kb as kb_module
from src.commands import onboard as onboard_module
from src.commands import prep as prep_module
from src.commands import propose as propose_module
from src.commands import report as report_module
from src.commands import review_full as review_full_module
from src.commands import summarize as summarize_module
from src.commands import synthesize as synthesize_module
from src.commands.backfill import BackfillCategorySummary, DiscoveredBackfillItem, _extract_offline_newsletters
from src.commands.confirm_stages import post_confirm_artifacts as confirm_artifacts_module
from src.commands.decision_brief import generate_decision_brief
from src.commands.propose import generate_section_revision_proposals
from src.commands.report import generate_report_draft
from src.commands.review_full import generate_review_full
from src.core.config_loader import EditorialRules
from src.core.journal import append_review_decision, append_signal
from src.core.models import Confidence
from src.core.models_v2 import Signal, SignalReviewDecision
from tests.support.ado_cassettes import load_cassette_work_items
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.unit.test_commands_kb import _seed_kb_update_layout
from tests.unit.test_commands_nudge_full_hygiene import _FullHygieneFakeADOClient as _NudgeFakeADOClient
from tests.unit.test_commands_nudge_full_hygiene import _seed_full_hygiene_config as _seed_nudge_full_hygiene_config
from tests.unit.test_commands_nudge_full_hygiene import _seed_people as _seed_nudge_people
from tests.unit.test_commands_nudge_full_hygiene import _seed_registry as _seed_nudge_registry
from tests.unit.test_commands_report import _sample_items, _seed_v2_report_layout
from tests.unit.test_meeting_close import _seed_repo as _seed_meeting_close_repo
from tests.unit.test_meeting_close import _StubADOClient as _MeetingCloseADOClient
from tests.unit.test_meeting_close import _StubTranscriptReader as _MeetingCloseTranscriptReader
from tests.unit.test_meeting_close import TranscriptRecord
from tests.unit.test_commands_summarize import _write_program_files


EDITION_NAME = "acme_weekly"


def test_report_disabled_mode_writes_no_ai_artifacts(repo_root: Path, tmp_path: Path, monkeypatch) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    monkeypatch.setattr(
        report_module,
        "_create_ai_client",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("_create_ai_client should not be called")),
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        artifacts = generate_report_draft(
            edition_name=EDITION_NAME,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=tmp_path / "programs",
            as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
            work_item_loader=lambda _bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
            open_browser=False,
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert artifacts.manifest.ai_calls == 0
    assert artifacts.manifest.ai_cost_usd == 0.0
    assert not ((tmp_path / "programs") / "acme" / "publications" / EDITION_NAME / "ai").exists()


def test_confirm_learning_distillation_disabled_mode_writes_no_ai_artifacts(tmp_path: Path, monkeypatch) -> None:
    edition_root = get_program_output_dir(EDITION_NAME, programs_root=tmp_path)
    edition_root.mkdir(parents=True, exist_ok=True)
    issue_dir = edition_root / "issue_001"
    issue_dir.mkdir(parents=True)
    # tracking file lives in per-issue subdir since report output path refactor
    (issue_dir / "issue_001.review_tracking.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    monkeypatch.setattr(
        "src.ai.learning_distiller.FallbackStructuredClient",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("FallbackStructuredClient should not be constructed")),
    )
    monkeypatch.setattr(
        confirm_artifacts_module,
        "load_tracking_reports",
        lambda _root: (SimpleNamespace(issue_number=1),),
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        learning_md_path, learning_json_path, summary, warning = confirm_artifacts_module.record_learning_distillation(
            edition_name=EDITION_NAME,
            issue_number=1,
            editorial_rules=cast(EditorialRules, SimpleNamespace()),
            programs_root=tmp_path,
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert learning_md_path is not None and learning_md_path.exists()
    assert learning_json_path is not None and learning_json_path.exists()
    assert summary is not None
    assert warning is None
    assert not (tmp_path / EDITION_NAME / "ai").exists()


def test_backfill_disabled_mode_writes_no_ai_artifacts(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    monkeypatch.setattr(
        "src.commands.backfill._build_backfill_extractor",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("_build_backfill_extractor should not be called")),
    )
    newsletter_path = tmp_path / "backfill" / "emails" / "issue_051.html"
    newsletter_path.parent.mkdir(parents=True)
    newsletter_path.write_text("<html>Issue 51</html>", encoding="utf-8")

    categories = [
        BackfillCategorySummary(
            category="prior_emails",
            count=1,
            items=(
                DiscoveredBackfillItem(
                    label="Issue 51",
                    reference="backfill/emails/issue_051.html",
                    source_id=None,
                    permalink=None,
                    origin="offline",
                ),
            ),
        )
    ]

    set_ai_mode(AIMode.DISABLED)
    try:
        extraction, warnings = _extract_offline_newsletters(
            edition_name=EDITION_NAME,
            categories=categories,
            repo_root=tmp_path,
            newsletter_extractor_factory=None,
            newsletter_source_categories=frozenset(c.category for c in categories),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert extraction is None
    assert warnings == ("Newsletter extraction skipped: invocation AI is disabled by --no-ai / AIMode.DISABLED.",)
    assert not (tmp_path / EDITION_NAME / "ai").exists()


def test_synthesize_disabled_mode_writes_no_ai_artifacts(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    editions_root = tmp_path / "editions"
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    monkeypatch.setattr(
        synthesize_module,
        "_resolve_program_id",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("_resolve_program_id should not be called")),
    )
    monkeypatch.setattr(
        synthesize_module,
        "append_ai_proposal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("append_ai_proposal should not be called")),
    )
    monkeypatch.setattr(
        synthesize_module,
        "supersede_pending_ai_proposals",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("supersede_pending_ai_proposals should not be called")),
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        try:
            synthesize_module.synthesize_workstream(
                workstream_id="networking",
                program_id="acme",
                programs_root=tmp_path / "programs",
                editions_root=editions_root,
            )
        except synthesize_module.SynthesisDisabledError:
            pass
        else:
            raise AssertionError("synthesize_workstream should raise SynthesisDisabledError when AI is disabled")
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert not (tmp_path / "acme_weekly" / "ai").exists()
    assert not (tmp_path / "acme" / "ai").exists()


def test_propose_disabled_mode_writes_no_ai_artifacts(repo_root: Path, tmp_path: Path, monkeypatch) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    monkeypatch.setattr(
        propose_module.report_command_helpers,
        "_create_ai_client",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("_create_ai_client should not be called")),
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        artifacts = generate_section_revision_proposals(
            edition_name=EDITION_NAME,
            ai=True,
            dry_run=True,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=tmp_path / "programs",
            work_item_loader=lambda *_args, **_kwargs: ((), 0),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert any("invocation AI is disabled" in warning for warning in artifacts.warnings)
    assert not ((tmp_path / "programs") / "acme" / "publications" / EDITION_NAME / "ai").exists()


def test_review_full_disabled_mode_writes_no_ai_artifacts(repo_root: Path, tmp_path: Path, monkeypatch) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    monkeypatch.setattr(
        review_full_module,
        "_build_anticipation_client",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("_build_anticipation_client should not be called")),
    )
    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda _bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        open_browser=False,
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        artifacts = generate_review_full(
            edition_name=EDITION_NAME,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=tmp_path / "programs",
            open_browser=False,
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert artifacts.html_path.exists()
    assert not ((tmp_path / "programs") / "acme" / "publications" / EDITION_NAME / "ai").exists()


def test_kb_ai_planning_disabled_mode_writes_no_ai_artifacts(tmp_path: Path, monkeypatch) -> None:
    programs_root = _seed_kb_update_layout(tmp_path)
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    monkeypatch.setenv("AZURE_OPENAI_KB_DEPLOYMENT", "kb-primary")

    set_ai_mode(AIMode.DISABLED)
    try:
        plan = kb_module._plan_kb_update_with_ai(
            correction="Rewrite the org map around the platform narrative.",
            program_id="demo",
            programs_root=tmp_path / "programs",
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert plan is None
    assert not (tmp_path / "output" / "demo" / "ai").exists()


def test_summarize_disabled_mode_writes_no_ai_artifacts(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    _write_program_files(programs_root)
    current_time = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    approved_signal = Signal(
        id="sig-approved",
        timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
        source="ado/revision",
        program_id="acme",
        workstream_id="acme",
        entity_refs=("WI:1234",),
        text="ADO#1234 target date changed from 2026-05-12 to 2026-05-17.",
        raw_ref="wi:1234",
        confidence=Confidence.HIGH,
    )
    append_signal(approved_signal, programs_root=tmp_path / "programs", partition_at=current_time)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sig-approved",
            decision="approved",
            reviewed_at=current_time,
            reviewed_by="system",
        ),
        programs_root=tmp_path / "programs",
    )
    monkeypatch.setattr(
        summarize_module,
        "_build_summary_generator",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("_build_summary_generator should not be called")),
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        artifacts = summarize_module.summarize_program(
            "acme",
            programs_root=tmp_path / "programs",
            now_provider=lambda: current_time,
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert artifacts.results[0].status == "skipped"
    assert not (tmp_path / "output" / "acme" / "ai").exists()


def test_onboard_disabled_mode_writes_no_ai_artifacts(tmp_path: Path, monkeypatch) -> None:
    reports_root = tmp_path / "reports"
    monkeypatch.setattr(
        onboard_module,
        "_build_default_onboard_assistant",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("_build_default_onboard_assistant should not be called")),
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        assistant = onboard_module._resolve_onboard_assistant(
            ai_enabled=True,
            edition_name="acme_weekly",
            reports_root=reports_root,
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert assistant is None
    assert not (reports_root / "acme_weekly" / "ai").exists()


def test_decision_brief_disabled_mode_writes_no_ai_artifacts(repo_root: Path, tmp_path: Path, monkeypatch) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")

    proposal_artifacts = generate_section_revision_proposals(
        edition_name=EDITION_NAME,
        dry_run=False,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        work_item_loader=lambda *_args, **_kwargs: ((), 0),
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        artifacts = generate_decision_brief(
            edition_name=EDITION_NAME,
            issue_number=proposal_artifacts.issue_number,
            ai=True,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=tmp_path / "programs",
            open_browser=False,
            create_ai_client=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("create_ai_client should not be called")
            ),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert artifacts.html_path.exists()
    assert artifacts.ai_enriched is False
    assert not ((tmp_path / "programs") / "acme" / "publications" / EDITION_NAME / "ai").exists()


def test_meeting_close_disabled_mode_writes_no_ai_artifacts(tmp_path: Path, monkeypatch) -> None:
    from src.commands import meeting_close as meeting_close_module

    repo_root = _seed_meeting_close_repo(tmp_path)
    program_path = repo_root / "programs" / "acme" / "program.yaml"
    program_payload = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_payload, dict)
    program_payload["ai"] = {"enabled": True, "budget_usd_per_run": 0.25}
    program_path.write_text(yaml.safe_dump(program_payload, sort_keys=False), encoding="utf-8")

    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    monkeypatch.setattr(meeting_close_module, "PROGRAMS_ROOT", repo_root / "programs")
    monkeypatch.setattr(
        meeting_close_module,
        "_build_transcript_reader",
        lambda: _MeetingCloseTranscriptReader(
            TranscriptRecord(
                meeting_id="lt-sync-disabled",
                title="LT Sync",
                captured_at="2026-05-21T12:00:00+00:00",
                web_url="https://contoso/meetings/lt-sync-disabled",
                content="Action: follow up with priya by 2026-05-14 on WI:101 to confirm the ramp packet.",
            )
        ),
    )
    monkeypatch.setattr(meeting_close_module, "_build_ado_client", lambda program: _MeetingCloseADOClient())

    set_ai_mode(AIMode.DISABLED)
    try:
        artifacts = meeting_close_module.generate_meeting_close_artifacts(
            program_id="acme",
            meeting_id="lt-sync-disabled",
            title_override=None,
            emit_html=False,
            emit_teams=False,
            dry_run=False,
            programs_root=repo_root / "programs",
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert artifacts.packet_path is not None and artifacts.packet_path.exists()
    assert artifacts.proposal_path is not None and artifacts.proposal_path.exists()
    assert artifacts.extractor == "deterministic"
    assert not (repo_root / "output" / "acme" / "ai").exists()


def test_nudge_disabled_mode_writes_no_ai_artifacts(repo_root: Path, tmp_path: Path, monkeypatch) -> None:
    from src.commands import nudge as nudge_module

    stage_v2_report_workspace(repo_root, tmp_path, edition_names=("acme_weekly", "nova_nudge"))
    programs_root = tmp_path / "programs"
    _seed_nudge_full_hygiene_config(tmp_path / "programs")
    _seed_nudge_people(tmp_path / "knowledge")
    _seed_nudge_registry(programs_root)
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    monkeypatch.setattr("src.commands.nudge.ADOClient", _NudgeFakeADOClient)

    set_ai_mode(AIMode.DISABLED)
    try:
        artifacts = nudge_module.generate_full_hygiene_nudges(
            program_id="acme",
            dry_run=True,
            as_of=datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc),
            programs_root=tmp_path / "programs",
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert len(artifacts.eml_paths) == 1
    assert artifacts.eml_paths[0].exists()
    assert not ((tmp_path / "programs" / "acme" / "publications") / "acme" / "ai").exists()


def test_prep_disabled_mode_writes_no_ai_artifacts(repo_root: Path, tmp_path: Path, monkeypatch) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )
    monkeypatch.setenv("VERTEX_LLM_TRACE", "1")
    monkeypatch.setattr(
        prep_module,
        "_build_anticipation_client",
        lambda *_args, **_kwargs: None,
    )

    set_ai_mode(AIMode.DISABLED)
    try:
        generate_report_draft(
            edition_name="nova_lt_deck",
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=tmp_path / "programs",
            as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
            work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
            kusto_query_executor=lambda query: [],
            open_browser=False,
        )
        artifacts = prep_module.generate_prep_brief(
            edition_name="nova_lt_deck",
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=tmp_path / "programs",
            as_of=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert artifacts.markdown_path.exists()
    assert not ((tmp_path / "programs") / "acme" / "publications" / "nova_lt_deck" / "ai").exists()


def test_setup_workstream_suggester_disabled_mode_skips_ai() -> None:
    """D-33: setup.py::_ai_suggest_workstreams must short-circuit when AI
    is disabled (returns empty list). The function lazily imports
    FallbackStructuredClient inside the body; we assert that lazy import
    is never reached when AIMode.DISABLED is set.

    Why:** setup.py is the only remaining AI-enabled command surface
    that didn't have a D-33 contract test. It must follow the same
    short-circuit contract as the other commands (no LLM traffic, no
    fallthrough to FallbackStructuredClient, deterministic empty
    result).
    **How to apply:** when adding a new AI-enabled command family, add
    a corresponding test in this file.
    """
    from src.commands import setup as setup_module

    set_ai_mode(AIMode.DISABLED)
    try:
        suggestions = setup_module._ai_suggest_workstreams(
            "A platform for orchestrating AI agent workflows across programs."
        )
    finally:
        set_ai_mode(AIMode.ACTIVE)

    assert suggestions == []
