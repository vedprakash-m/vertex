from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import json
import sqlite3
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer
import yaml
from typer.testing import CliRunner

from cli import app
import src.commands.confirm as confirm_module
import src.commands.report as report_module
from src.commands.confirm_stages import claim_resolution  # D-25: claim-extractor resolution moved here
from src.commands.confirm_stages import post_confirm_support
from src.ai.exec_summary_drafter import ExecSummaryDraft
from src.ai.draft_reviewer import DraftReviewArtifact, DraftReviewReport, ReviewSuggestion
from src.core.claim_tracker import ClaimExtractionResult, append_claim_entry, load_claim_entries, load_decision_asks
from src.core.claim_extraction_calibration_store import load_claim_extraction_calibration_records
from src.core.analytics_store import get_program_analytics_dirty_path, get_program_analytics_store_path
from src.core.gather_state_store import GatherState
from src.core.models import ArchiveEntry, ArchiveIndex
from src.commands.confirm import confirm_issue
from src.commands.report import _DraftAIContext, _write_output_json, generate_report_draft
from src.ai.edit_learner import read_edit_patterns
import src.core.archive_store as archive_store
from src.core.config_loader import load_report_bundle
from src.core.decision_register import save_decisions
from src.core.journal import append_review_decision, append_signal, read_signals
from src.ai.learning_distiller import EditorialRuleProposal, LearningDistillation
from src.core.narrative_store import get_narratives_dir, load_narratives
from src.core.overrides_store import get_overrides_path, load_overrides
from src.core.program_fact_store import FactPrecedence, ProgramFactInput, ProgramFactStore
from src.core.models import Confidence, ReviewSection, ReviewState, ReviewStatus, RiskLevel
from src.core.models_v2 import ClaimEntry, DecisionAsk, DecisionEntry, DecisionStatus, SectionEvidenceBrief, SectionRevisionProposal, SectionRevisionStatus, Signal, SignalReviewDecision
from src.core.review_status_store import load_review_status, save_review_status
from src.core.semantic_index import get_semantic_index_path, load_semantic_index_state
from tests.support.slice_contract_fixtures import build_test_source_health_slice_contract
from src.core.section_proposal_store import append_proposal
from src.core.slice_contract_loader import SliceAdoSourceContract, SliceContract, SliceDegradation, SliceFilterDefinition, SliceFreshness, SliceOwners, SlicePredicateDefinition, SliceSourceContract, SliceTelemetryContract
from src.core.sqlite_stores import SQLiteSignalStore
from src.core.snapshot_store import get_archive_root
from src.core import quality_gates
from src.core.quality_gates import GateEvaluation, QualityGateReport
from src.core.trusted_baseline_store import load_trusted_baseline
from src.core.workstream_association_store import read_workstream_association_records
from tests.support.report_test_setup import disable_kusto_in_report_copy, reset_overrides_to_seed_state, stage_v2_report_workspace
from tests.unit.test_commands_report import _forecast_items, _lookback_snapshot, _manifest, _sample_items, _seed_v2_report_layout, _set_override_risks_for_section, _set_v2_program_artifact_base_url, _snapshot_item_from_work_item

runner = CliRunner()
EDITION_NAME = "acme_weekly"


@pytest.fixture(autouse=True)
def _isolate_fact_store_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect ProgramFactStore DB to an isolated per-test path.

    Prevents xdist workers from sharing ~/.vertex/acme/vertex.sqlite3 and
    causing spurious QG-SG-20 drift-gate failures when confirm shadow-writes
    land in the shared DB between another test's pin and drift-check.
    Tests that explicitly pass db_root= are unaffected (db_root overrides env).
    """
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / ".vertex"))


def test_program_fact_drift_gate_blocks_confirm_when_live_store_changes(tmp_path: Path) -> None:
    store = ProgramFactStore("acme", db_root=tmp_path)
    store.append_fact(
        ProgramFactInput(
            fact_type="decision",
            scope="program",
            entity_refs=("PROGRAM:acme",),
            payload={"decision": "hold"},
        ),
        recorded_at=datetime(2026, 5, 31, 11, 0, tzinfo=timezone.utc),
    )
    pin = report_module._pin_program_fact_snapshot(
        "acme",
        edition_name="acme_weekly",
        issue_number=79,
        generated_at=datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc),
        db_root=tmp_path,
    )
    assert pin is not None

    store.append_fact(
        ProgramFactInput(
            fact_type="decision",
            scope="program",
            entity_refs=("PROGRAM:acme",),
            payload={"decision": "ship"},
            precedence=FactPrecedence.CONFIRMED_GOVERNANCE_DECISION,
        ),
        recorded_at=datetime(2026, 5, 31, 13, 0, tzinfo=timezone.utc),
    )

    gate = quality_gates.evaluate_program_fact_drift_from_draft(
        draft_state={
            "program_fact_snapshot": {
                "snapshot_id": pin.snapshot_id,
                "program_id": "acme",
            }
        },
        program_id="acme",
        db_root=tmp_path,
    )

    assert gate.passed is False
    assert gate.results[0].gate_id == "QG-SG-20"
    assert "State Drift Warning" in gate.results[0].message


# ---------------------------------------------------------------------------
# ADF-W1.9 / QG-37: _assert_state_authority_for_confirm (the mutation-blocking
# half, wired into confirm_issue 2026-07-13). Isolated from confirm_issue's
# full pipeline on purpose -- this repo's acme fixture workspace is broken
# in this environment ("program.yaml absent after copy", unrelated to this
# change), which skips most of confirm_issue's own tests here.
# ---------------------------------------------------------------------------


def test_assert_state_authority_passes_when_unambiguous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # find_stray_fact_store_databases always checks the *real* Path.home() for
    # a home_fallback candidate (matching test_state_authority_gate.py's own
    # isolation convention) -- without this, a real ~/.vertex/acme/vertex.sqlite3
    # on the machine running the test would make this test non-hermetic.
    fake_home = tmp_path / "fake-home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    programs_root = tmp_path / "programs"
    (programs_root / "acme").mkdir(parents=True)
    # No stray databases exist under this tmp_path/fake_home -> unambiguous.
    confirm_module._assert_state_authority_for_confirm("acme", programs_root=programs_root)


def test_assert_state_authority_raises_confirm_error_when_ambiguous(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolated from the real gate logic: any StateAuthorityAmbiguousError
    must be converted to ConfirmError, regardless of what triggered it."""

    def _raise_ambiguous(program_id: str, *, programs_root: Path) -> None:
        raise confirm_module.StateAuthorityAmbiguousError(f"ambiguous fact-store authority for {program_id!r}")

    monkeypatch.setattr("src.commands.confirm.assert_state_authority_or_raise", _raise_ambiguous)

    with pytest.raises(confirm_module.ConfirmError, match="ambiguous fact-store authority"):
        confirm_module._assert_state_authority_for_confirm("acme", programs_root=tmp_path / "programs")


def test_assert_state_authority_real_ambiguity_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No mocking of the gate itself -- a real stray database triggers a
    real StateAuthorityAmbiguousError, converted to ConfirmError."""
    monkeypatch.delenv("VERTEX_DB_PATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "fake-home"))
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True)
    canonical_dir = programs_root.parent / "vertex-db" / "acme"
    canonical_dir.mkdir(parents=True)
    sqlite3.connect(str(canonical_dir / "vertex.sqlite3")).close()
    stray_dir = programs_root / "acme"
    stray_dir.mkdir(parents=True)
    sqlite3.connect(str(stray_dir / "vertex.sqlite3")).close()

    with pytest.raises(confirm_module.ConfirmError, match="ambiguous fact-store authority"):
        confirm_module._assert_state_authority_for_confirm("acme", programs_root=programs_root)


def test_confirm_records_context_gap_when_ncfl_hook_fails(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    captured_gaps: list[dict[str, object]] = []

    monkeypatch.setattr(
        confirm_module,
        "_record_ncfl_proposals_impl",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("ncfl blew up")),
    )
    monkeypatch.setattr(
        confirm_module,
        "append_context_gap",
        lambda **kwargs: captured_gaps.append(kwargs) or (programs_root / "acme" / "_feedback" / "context_gaps.jsonl"),
    )

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.exit_code == 0
    assert captured_gaps
    assert captured_gaps[0]["feature"] == "ncfl"
    assert captured_gaps[0]["field"] == "ncfl_extraction_failed"
    assert any("NCFL proposal extraction skipped: ncfl blew up" in warning for warning in result.warnings)


def _ack_decision_strip(overrides_payload: dict[str, object]) -> None:
    overrides_payload["decision_strip_ack"] = {
        "no_leadership_ask": True,
        "reason": "Freshness and risk signals are already tracked and do not require a new leadership ask for this confirm test.",
    }


def _write_authored_exec_summary(
    reports_root: Path,
    issue_number: int,
    text: str = "Confirmed executive summary.\n",
    *,
    edition_name: str = EDITION_NAME,
) -> None:
    exec_summary_path = get_narratives_dir(edition_name, issue_number, reports_root) / "exec_summary.md"
    exec_summary_path.parent.mkdir(parents=True, exist_ok=True)
    exec_summary_path.write_text(text, encoding="utf-8")


def _write_authored_workstream_narratives(
    reports_root: Path,
    issue_number: int,
    text: str = "Confirmed workstream narrative.\n",
    *,
    edition_name: str = EDITION_NAME,
) -> None:
    narratives_dir = get_narratives_dir(edition_name, issue_number, reports_root)
    narratives_dir.mkdir(parents=True, exist_ok=True)
    for narrative_path in narratives_dir.glob("ws_*.md"):
        narrative_path.write_text(text, encoding="utf-8")


def _append_accepted_section_proposal(
    programs_root: Path,
    *,
    issue_number: int,
    proposal_id: str = "proposal-exec-summary-accepted",
    section_id: str = "exec_summary",
    current_text: str = "Confirmed executive summary.",
    proposed_text: str | None = "Confirmed executive summary.",
    accepted_text: str | None = "Confirmed executive summary.",
    status: SectionRevisionStatus = SectionRevisionStatus.ACCEPTED,
    generated_at: datetime | None = None,
    resolved_at: datetime | None = None,
) -> None:
    append_proposal(
        SectionRevisionProposal(
            proposal_id=proposal_id,
            edition_id=EDITION_NAME,
            issue_number=issue_number,
            section_id=section_id,
            current_text=current_text,
            proposed_text=proposed_text,
            evidence_brief=SectionEvidenceBrief(
                section_id=section_id,
                ado_delta_summary="No material changes.",
                new_items=(),
                closed_items=(),
                risk_changed_items=(),
                eta_changed_items=(),
                top_signals=(),
                kpi_summary=None,
                stale_claims=(),
                vitality_summary="Stable",
                confidence=Confidence.MEDIUM,
            ),
            status=status,
            generated_at=generated_at or datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
            resolved_at=resolved_at or datetime(2026, 5, 5, 18, 5, tzinfo=timezone.utc),
            accepted_text=accepted_text,
            source_hash="sha256:test",
        ),
        "acme",
        issue_number,
        programs_root=programs_root,
    )


def _seed_high_risk_signal_coverage(
    programs_root: Path,
    *,
    captured_at,
    work_item_id: int = 900002,
) -> None:
    signal = Signal(
        id=f"coverage-signal-{work_item_id}",
        timestamp=captured_at,
        source="manual",
        program_id="acme",
        workstream_id="networking",
        entity_refs=(f"WI:{work_item_id}",),
        text=f"Reviewed coverage for WI:{work_item_id} High-risk execution item.",
        raw_ref=f"wi:{work_item_id}:coverage",
        confidence=Confidence.HIGH,
        metadata={"work_item_id": work_item_id},
    )
    append_signal(signal, programs_root=programs_root, partition_at=captured_at)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id=signal.id,
            decision="approved",
            reviewed_at=captured_at,
            reviewed_by="unit-test",
            note="Seed approved coverage for confirm tests.",
        ),
        programs_root=programs_root,
    )


def _prepare_confirmable_weekly_issue(repo_root: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(
        2026,
        5,
        5,
        18,
        0,
        tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc,
    )
    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(programs_root, captured_at=as_of)

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    exec_summary_path = get_narratives_dir(EDITION_NAME, 1, reports_root) / "exec_summary.md"
    exec_summary_path.parent.mkdir(parents=True, exist_ok=True)
    exec_summary_path.write_text("Confirmed executive summary.\n", encoding="utf-8")
    _write_authored_workstream_narratives(reports_root, 1)
    return reports_root, archive_root, (tmp_path / "programs" / "acme" / "publications")


def _set_program_storage_backend(programs_root: Path, *, storage_backend: str) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    document["storage_backend"] = storage_backend
    program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _set_program_semantic_index(programs_root: Path, *, enabled: bool) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    ai_document = document.get("ai")
    if not isinstance(ai_document, dict):
        ai_document = {}
    ai_document["semantic_index"] = enabled
    document["ai"] = ai_document
    program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _set_program_ai_claim_extractor(
    programs_root: Path,
    *,
    enabled: bool,
    mode: str,
) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    ai_document = document.get("ai")
    if not isinstance(ai_document, dict):
        ai_document = {}
    ai_document["enabled"] = enabled
    ai_document["claim_extractor"] = {
        "mode": mode,
        "calibration_min_confirms": 20,
    }
    document["ai"] = ai_document
    program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _source_health_slice_contract() -> SliceContract:
    return build_test_source_health_slice_contract(
        contract_id="acme.deployment_velocity",
        source_of_truth="telemetry_primary",
        include_ado=True,
        include_telemetry=True,
    )


def _seed_high_risk_signal_coverage_sqlite(
    programs_root: Path,
    *,
    captured_at,
    work_item_id: int = 900002,
) -> None:
    signal = Signal(
        id=f"coverage-signal-{work_item_id}",
        timestamp=captured_at,
        source="manual",
        program_id="acme",
        workstream_id="networking",
        entity_refs=(f"WI:{work_item_id}",),
        text=f"Reviewed coverage for WI:{work_item_id} High-risk execution item.",
        raw_ref=f"wi:{work_item_id}:coverage",
        confidence=Confidence.HIGH,
        metadata={"work_item_id": work_item_id},
    )
    signal_store = SQLiteSignalStore(programs_root=programs_root)
    signal_store.append(signal)
    signal_store.append_review(
        "acme",
        SignalReviewDecision(
            signal_id=signal.id,
            decision="approved",
            reviewed_at=captured_at,
            reviewed_by="unit-test",
            note="Seed approved coverage for confirm sqlite tests.",
        ),
    )


def test_confirm_issue_archives_outputs_and_resets_active_state(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(programs_root, captured_at=as_of)

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _write_authored_exec_summary(reports_root, 5, edition_name=EDITION_NAME)

    exec_summary_path = get_narratives_dir(EDITION_NAME, 1, reports_root) / "exec_summary.md"
    exec_summary_path.write_text("Confirmed executive summary.\n", encoding="utf-8")
    _write_authored_workstream_narratives(reports_root, 1)
    _append_accepted_section_proposal(programs_root, issue_number=1)
    _append_accepted_section_proposal(
        programs_root,
        issue_number=1,
        proposal_id="proposal-networking-modified",
        section_id="ws_networking",
        current_text="Confirmed workstream narrative.",
        proposed_text="Original generated networking narrative.",
        accepted_text="Edited workstream narrative after reviewer changes.",
        status=SectionRevisionStatus.ACCEPTED_MODIFIED,
        generated_at=datetime(2026, 5, 5, 18, 1, tzinfo=timezone.utc),
        resolved_at=datetime(2026, 5, 5, 18, 6, tzinfo=timezone.utc),
    )
    continuation_contract_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.continuation_contract.json"
    continuation_contract_path.parent.mkdir(parents=True, exist_ok=True)
    continuation_contract_path.write_text('{"schema_version":"1.0","issue_number":1}\n', encoding="utf-8")

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.exit_code == 0
    assert result.archive_paths is not None
    assert result.archive_paths.snapshot_path.exists()
    assert result.archive_paths.eml_path is not None and result.archive_paths.eml_path.exists()
    assert result.archive_paths.html_path.exists()
    assert result.archive_paths.md_path.exists()
    assert result.archive_paths.manifest_path.exists()
    assert result.archive_paths.overrides_path is not None and result.archive_paths.overrides_path.exists()
    assert result.archive_paths.review_path is not None and result.archive_paths.review_path.exists()
    assert result.archive_paths.narratives_path is not None and (result.archive_paths.narratives_path / "exec_summary.md").exists()
    accepted_proposals_path = result.archive_paths.narratives_path / "proposals_accepted.jsonl"
    assert accepted_proposals_path.exists()
    accepted_records = [json.loads(line) for line in accepted_proposals_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert accepted_records == [
        {
            "proposal_id": "proposal-exec-summary-accepted",
            "edition_id": EDITION_NAME,
            "issue_number": 1,
            "section_id": "exec_summary",
            "current_text": "Confirmed executive summary.",
            "proposed_text": "Confirmed executive summary.",
            "evidence_brief": {
                "section_id": "exec_summary",
                "ado_delta_summary": "No material changes.",
                "new_items": [],
                "closed_items": [],
                "risk_changed_items": [],
                "eta_changed_items": [],
                "top_signals": [],
                "kpi_summary": None,
                "stale_claims": [],
                "vitality_summary": "Stable",
                "confidence": "medium",
            },
            "status": "accepted",
            "generated_at": "2026-05-05T18:00:00+00:00",
            "resolved_at": "2026-05-05T18:05:00+00:00",
            "accepted_text": "Confirmed executive summary.",
            "rejection_reason": None,
            "source_hash": "sha256:test",
            "ai_model_used": None,
            "ai_cost_usd": None,
        },
        {
            "proposal_id": "proposal-networking-modified",
            "edition_id": EDITION_NAME,
            "issue_number": 1,
            "section_id": "ws_networking",
            "current_text": "Confirmed workstream narrative.",
            "proposed_text": "Original generated networking narrative.",
            "evidence_brief": {
                "section_id": "ws_networking",
                "ado_delta_summary": "No material changes.",
                "new_items": [],
                "closed_items": [],
                "risk_changed_items": [],
                "eta_changed_items": [],
                "top_signals": [],
                "kpi_summary": None,
                "stale_claims": [],
                "vitality_summary": "Stable",
                "confidence": "medium",
            },
            "status": "accepted_modified",
            "generated_at": "2026-05-05T18:01:00+00:00",
            "resolved_at": "2026-05-05T18:06:00+00:00",
            "accepted_text": "Edited workstream narrative after reviewer changes.",
            "rejection_reason": None,
            "source_hash": "sha256:test",
            "ai_model_used": None,
            "ai_cost_usd": None,
        }
    ]
    assert result.archive_paths.continuation_contract_path is not None and result.archive_paths.continuation_contract_path.exists()

    active_overrides = load_overrides(EDITION_NAME, reports_root=reports_root)
    active_narratives = load_narratives(EDITION_NAME, 2, reports_root=reports_root)
    active_review_status = load_review_status(EDITION_NAME, reports_root=reports_root)
    trusted_baseline = load_trusted_baseline(
        EDITION_NAME,
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    assert active_overrides is not None and active_overrides.issue_number == 2
    assert active_overrides.top_3_now == ()
    assert active_narratives["exec_summary.md"] == "Confirmed executive summary.\n"
    assert active_review_status is not None and active_review_status.issue_number == 2
    assert all(section.state.value == "pending" for section in active_review_status.sections)
    assert trusted_baseline is not None
    assert trusted_baseline.trusted_issue_number == 1
    assert trusted_baseline.history[-1].action == "established"


def test_confirm_issue_writes_analytics_projection(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.exit_code == 0
    analytics_path = get_program_analytics_store_path("acme", programs_root=programs_root)
    assert analytics_path.exists()
    assert get_program_analytics_dirty_path("acme", programs_root=programs_root).exists() is True
    assert any("Analytics projection skipped" in warning for warning in result.warnings)

    connection = sqlite3.connect(analytics_path)
    try:
        rows = connection.execute(
            "SELECT issue_number, dimension, risk FROM confirmed_risks WHERE edition = ? ORDER BY dimension ASC",
            (EDITION_NAME,),
        ).fetchall()
    finally:
        connection.close()

    assert rows == []


def test_confirm_issue_surfaces_claim_freshness_advisory_from_section_proposals(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    append_proposal(
        SectionRevisionProposal(
            proposal_id="proposal-networking-stale-claim",
            edition_id=EDITION_NAME,
            issue_number=1,
            section_id="ws_networking",
            current_text="Confirmed workstream narrative.",
            proposed_text="Confirmed workstream narrative.",
            evidence_brief=SectionEvidenceBrief(
                section_id="ws_networking",
                ado_delta_summary="No material changes.",
                new_items=(),
                closed_items=(),
                risk_changed_items=(),
                eta_changed_items=(),
                top_signals=(),
                kpi_summary=None,
                stale_claims=("claim-stale-1",),
                vitality_summary="Stable",
                confidence=Confidence.MEDIUM,
            ),
            status=SectionRevisionStatus.ACCEPTED,
            generated_at=datetime(2026, 5, 5, 18, 1, tzinfo=timezone.utc),
            resolved_at=datetime(2026, 5, 5, 18, 6, tzinfo=timezone.utc),
            accepted_text="Confirmed workstream narrative.",
            source_hash="sha256:test-stale-claim",
        ),
        "acme",
        1,
        programs_root=programs_root,
    )

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.exit_code == 0
    assert result.manifest.qg_results["QG-DM-13"] is False
    assert any("Claim freshness advisory" in warning for warning in result.warnings)
    assert any("claim-stale-1" in warning for warning in result.warnings)


def test_confirm_issue_updates_semantic_index_when_enabled(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_program_semantic_index(programs_root, enabled=True)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.exit_code == 0
    assert get_semantic_index_path(EDITION_NAME, archive_root=archive_root).exists()
    state = load_semantic_index_state(EDITION_NAME, archive_root=archive_root)
    assert state is not None
    assert state.semantic_index_dirty is False
    assert state.latest_confirmed_issue == 1


def test_confirm_issue_marks_semantic_index_dirty_when_update_fails(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_program_semantic_index(programs_root, enabled=True)

    def _fail_index_refresh(*args, **kwargs):
        raise RuntimeError("fts refresh failed")

    monkeypatch.setattr("src.commands.confirm.update_archive_semantic_index_for_issue", _fail_index_refresh)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.exit_code == 0
    assert any("Semantic index update skipped" in warning for warning in result.warnings)
    state = load_semantic_index_state(EDITION_NAME, archive_root=archive_root)
    assert state is not None
    assert state.semantic_index_dirty is True
    assert "fts refresh failed" in (state.dirty_reason or "")


def test_confirm_issue_phase_1b_reads_sqlite_backed_signals(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)
    _set_program_storage_backend(programs_root, storage_backend="sqlite")

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(
        2026,
        5,
        5,
        18,
        0,
        tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc,
    )
    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )
    _seed_high_risk_signal_coverage_sqlite(programs_root, captured_at=as_of)

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _write_authored_exec_summary(reports_root, 1, edition_name=EDITION_NAME)

    captured_gate_inputs: dict[str, tuple[Signal, ...]] = {}
    original_phase_1b = confirm_issue.__globals__["evaluate_phase_1b_gates"]

    def _capture_phase_1b(**kwargs):
        captured_gate_inputs["approved_signals"] = tuple(kwargs["approved_signals"])
        captured_gate_inputs["journal_signals"] = tuple(kwargs["journal_signals"])
        return original_phase_1b(**kwargs)

    monkeypatch.setattr("src.commands.confirm.evaluate_phase_1b_gates", _capture_phase_1b)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.snapshot is not None
    assert not read_signals("acme", programs_root=programs_root)
    assert [signal.id for signal in captured_gate_inputs["journal_signals"]] == ["coverage-signal-900002"]
    assert [signal.id for signal in captured_gate_inputs["approved_signals"]] == ["coverage-signal-900002"]


def test_confirm_issue_surfaces_advisory_readiness_gates_when_enabled(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
        reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
        programs_root = reports_root.parent / "programs"
        program_path = programs_root / "acme" / "program.yaml"
        program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
        assert isinstance(program_document, dict)
        program_document["readiness"] = {"gate": True, "snapshot_max_age_days": 7}
        program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")

        (programs_root / "acme" / "readiness.yaml").write_text(
                """
schema_version: '1.0'
snapshot_max_age_days: 7
dimensions:
    rollback_plan:
        source:
            type: manual_attestation
            attested_by: testowner
        pass_condition:
            kind: attested_within_days
            days: 30
""".strip(),
                encoding="utf-8",
        )
        readiness_snapshot_path = programs_root / "acme" / "readiness_snapshot.yaml"
        if readiness_snapshot_path.exists():
            readiness_snapshot_path.unlink()

        empty_report = QualityGateReport(results=())
        monkeypatch.setattr("src.commands.confirm.evaluate_phase_1a_gates", lambda **kwargs: empty_report)
        monkeypatch.setattr("src.commands.confirm.evaluate_phase_1b_gates", lambda **kwargs: empty_report)
        monkeypatch.setattr("src.commands.confirm.evaluate_phase_1c_gates", lambda **kwargs: empty_report)
        monkeypatch.setattr("src.commands.confirm.evaluate_bridge_gates", lambda **kwargs: empty_report)
        monkeypatch.setattr("src.commands.confirm.evaluate_continuity_gates", lambda **kwargs: empty_report)
        monkeypatch.setattr("src.commands.confirm.evaluate_context_integrity_gates", lambda **kwargs: empty_report)

        result = confirm_issue(
            edition_name=EDITION_NAME,
            issue_number=1,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=programs_root,
            dry_run=True,
        )

        assert result.exit_code == 0
        assert result.manifest.qg_results["QG-RD4"] is False
        assert any("Readiness snapshot is missing for program 'acme'" in warning for warning in result.warnings)


def test_confirm_issue_blocks_on_source_health_gate(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    failing_report = QualityGateReport(
        results=(
            GateEvaluation(
                gate_id="QG-SG-01",
                passed=False,
                message="Source health gate failed with 1 blocking source role(s) (demo.slice:telemetry=stale).",
                exit_code=3,
                forceable=True,
            ),
        )
    )
    monkeypatch.setattr("src.commands.confirm.evaluate_source_health_gates", lambda **kwargs: failing_report)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
    )

    assert result.exit_code == 3
    assert result.archive_paths is None
    assert result.manifest.qg_results["QG-SG-01"] is False
    assert failing_report.results[0].message in result.failures


def test_confirm_issue_force_surfaces_source_health_gate_as_warning(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    failing_report = QualityGateReport(
        results=(
            GateEvaluation(
                gate_id="QG-SG-01",
                passed=False,
                message="Source health gate failed with 1 blocking source role(s) (demo.slice:telemetry=stale).",
                exit_code=3,
                forceable=True,
            ),
        )
    )
    monkeypatch.setattr("src.commands.confirm.evaluate_source_health_gates", lambda **kwargs: failing_report)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        dry_run=True,
        force=True,
    )

    assert result.exit_code == 0
    assert result.manifest.qg_results["QG-SG-01"] is False
    assert any("Forced past QG-SG-01" in warning for warning in result.warnings)


def test_confirm_issue_force_does_not_override_non_forceable_unbound_source_health_gate(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    failing_report = QualityGateReport(
        results=(
            GateEvaluation(
                gate_id="QG-SG-01",
                passed=False,
                message="Source health gate failed with 1 blocking source role(s) (demo.slice:system_of_record=unbound). Fix the slice/source binding before confirming.",
                exit_code=3,
                forceable=False,
            ),
        )
    )
    monkeypatch.setattr("src.commands.confirm.evaluate_source_health_gates", lambda **kwargs: failing_report)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        dry_run=True,
        force=True,
    )

    assert result.exit_code == 3
    assert result.archive_paths is None
    assert result.manifest.qg_results["QG-SG-01"] is False
    assert not any("Forced past QG-SG-01" in warning for warning in result.warnings)
    assert failing_report.results[0].message in result.failures


def test_confirm_issue_maps_detailed_edition_source_health_to_newsletter_function(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    captured: dict[str, object] = {}
    passing_report = QualityGateReport(
        results=(
            GateEvaluation(
                gate_id="QG-SG-01",
                passed=True,
                message="Newsletter source health gate passed for 1 slice source contract(s).",
                exit_code=3,
                forceable=True,
            ),
        )
    )

    def _record_source_health_gate(**kwargs):
        captured.update(kwargs)
        return passing_report

    monkeypatch.setattr("src.commands.confirm.evaluate_source_health_gates", _record_source_health_gate)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        dry_run=True,
        force=True,
    )

    assert result.exit_code in {0, 3}
    assert captured["function_name"] == "newsletter"


def test_confirm_issue_allows_waived_source_health_roles(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    empty_report = QualityGateReport(results=())
    monkeypatch.setattr("src.commands.confirm.evaluate_phase_1a_gates", lambda **kwargs: empty_report)
    monkeypatch.setattr("src.commands.confirm.evaluate_phase_1b_gates", lambda **kwargs: empty_report)
    monkeypatch.setattr("src.commands.confirm.evaluate_phase_1c_gates", lambda **kwargs: empty_report)
    monkeypatch.setattr("src.commands.confirm.evaluate_bridge_gates", lambda **kwargs: empty_report)
    monkeypatch.setattr("src.commands.confirm.evaluate_continuity_gates", lambda **kwargs: empty_report)
    monkeypatch.setattr("src.commands.confirm.evaluate_context_integrity_gates", lambda **kwargs: empty_report)
    monkeypatch.setattr("src.commands.confirm.evaluate_program_fact_drift_from_draft", lambda **kwargs: empty_report)
    (programs_root / "acme" / "source_waivers.yaml").write_text(
        """
schema_version: '1.0'
waivers:
  - contract_id: acme.deployment_velocity
    role: telemetry
    owner: owner@example.com
    reason: Known telemetry lag during cutover.
    granted: 2026-05-01
    expires: 2026-06-30
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "src.commands.confirm.load_slice_contract_for_edition",
        lambda edition_name, reports_root: (_source_health_slice_contract(),),
    )
    monkeypatch.setattr(
        "src.commands.confirm.load_gather_state",
        lambda program_id, programs_root: GatherState(
            program_id=program_id,
            gathered_at=datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc),
            scanned_items=0,
            discovered_signals=0,
            new_signals=0,
            pending_review=0,
            trajectory_updates=0,
            auto_reviews_written=0,
            ado_calls=0,
            archived_journal_files=0,
            background_proposals=0,
            query_states={"velocity-p50": {"last_cycle_succeeded": True, "row_count": 2, "data_age_hours": 48.0}},
            channels={"ado": {"active": True, "signal_count": 4}},
        ),
    )

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        dry_run=True,
    )

    assert result.exit_code == 0
    assert result.manifest.qg_results["QG-SG-01"] is True
    assert not any("QG-SG-01" in warning for warning in result.warnings)


def test_confirm_issue_posts_weekly_summary_card_when_requested(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_v2_program_artifact_base_url(reports_root.parent / "programs", artifact_base_url="https://contoso.example/vertex-output")
    _set_v2_program_teams_webhook_url(reports_root.parent / "programs", webhook_url="https://contoso.example/webhook")

    sent_payloads: list[dict[str, object]] = []

    def _build_fake_sender(webhook_url: str):
        assert webhook_url == "https://contoso.example/webhook"

        def _sender(payload: dict[str, object]) -> None:
            sent_payloads.append(payload)

        return _sender

    monkeypatch.setattr("src.commands.confirm_stages.weekly_summary_card.build_confirm_weekly_summary_teams_sender", _build_fake_sender)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
        post_weekly_summary_card=True,
    )

    assert result.exit_code == 0
    assert result.posted_weekly_summary_card is True
    assert result.weekly_summary_card_path is not None and result.weekly_summary_card_path.exists()
    assert len(sent_payloads) == 1

    payload = json.loads(result.weekly_summary_card_path.read_text(encoding="utf-8"))
    assert payload == sent_payloads[0]
    assert result.archive_paths is not None
    assert f"file:///{result.archive_paths.html_path.as_posix()}" in json.dumps(payload)


def test_confirm_issue_can_skip_trusted_baseline_update(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
        untrusted=True,
        untrusted_reason="Issue 001 is being archived for traceability, but it should not become the trusted continuation baseline.",
    )
    trusted_baseline = load_trusted_baseline(
        EDITION_NAME,
        editions_root=reports_root.parent / "editions",
        programs_root=reports_root.parent / "programs",
    )

    assert result.exit_code == 0
    assert trusted_baseline is not None
    assert trusted_baseline.trusted_issue_number is None
    assert trusted_baseline.last_untrusted is not None
    assert trusted_baseline.last_untrusted.issue == 1
    assert trusted_baseline.history[-1].action == "untrusted"
    assert result.manifest.metadata["untrusted"] is True


def test_confirm_issue_persists_workstream_association_ledger(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    records = read_workstream_association_records("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert result.workstream_association_log_path is not None and result.workstream_association_log_path.exists()
    assert any(record.issue_number == 1 for record in records)
    assert any(record.source_type == "curated_slice" for record in records)
    assert any(record.source_type in {"query_derived", "area_path_derived", "slice_membership"} for record in records)


def test_write_context_snapshot_for_issue_reads_workstreams_from_program_facts(tmp_path: Path, monkeypatch) -> None:
    milestone = object()
    risk = object()
    decision = object()
    workstream = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(post_confirm_support, "load_current_milestones", lambda *_args, **_kwargs: (milestone,))
    monkeypatch.setattr(post_confirm_support, "load_current_risk_entries", lambda *_args, **_kwargs: (risk,))
    monkeypatch.setattr(post_confirm_support, "load_current_decision_entries", lambda *_args, **_kwargs: (decision,))
    monkeypatch.setattr(post_confirm_support, "load_current_workstreams", lambda *_args, **_kwargs: (workstream,))
    monkeypatch.setattr(
        post_confirm_support,
        "_load_program_context",
        lambda *_args, **_kwargs: SimpleNamespace(maturity_level=SimpleNamespace(value=0)),
    )
    monkeypatch.setattr(
        post_confirm_support,
        "write_context_snapshot",
        lambda *args, **kwargs: captured.update(kwargs),
    )

    confirm_module._write_context_snapshot_for_issue(
        program_id="acme",
        edition_id="acme_weekly",
        issue_number=1,
        confirmed_at=datetime(2026, 5, 10, 8, 0, tzinfo=timezone.utc),
        archive_root=tmp_path / "archive",
        programs_root=tmp_path / "programs",
        prior_issue_entry=None,
    )

    assert captured["program_id"] == "acme"
    assert captured["milestones"] == [milestone]
    assert captured["risks"] == [risk]
    assert captured["decisions"] == [decision]
    assert captured["workstreams"] == [workstream]


def test_confirm_issue_rejects_weekly_summary_card_without_webhook(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    with pytest.raises(typer.BadParameter, match="teams_incoming_webhook_url"):
        confirm_issue(
            edition_name=EDITION_NAME,
            issue_number=1,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=programs_root,
            force=True,
            post_weekly_summary_card=True,
        )


def test_confirm_issue_warns_when_weekly_summary_card_post_fails(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_v2_program_teams_webhook_url(reports_root.parent / "programs", webhook_url="https://contoso.example/webhook")

    def _build_failing_sender(webhook_url: str):
        assert webhook_url == "https://contoso.example/webhook"

        def _sender(payload: dict[str, object]) -> None:
            raise RuntimeError("webhook down")

        return _sender

    monkeypatch.setattr("src.commands.confirm_stages.weekly_summary_card.build_confirm_weekly_summary_teams_sender", _build_failing_sender)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
        post_weekly_summary_card=True,
    )

    assert result.exit_code == 0
    assert result.archive_paths is not None and result.archive_paths.html_path.exists()
    assert result.posted_weekly_summary_card is False
    assert result.weekly_summary_card_path is not None and result.weekly_summary_card_path.exists()
    assert any("Weekly summary card not posted: webhook down" in warning for warning in result.warnings)


def test_confirm_cli_blocks_when_risks_are_missing(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )

    monkeypatch.setattr("src.commands.confirm.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.confirm.ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr("src.commands.confirm._CONFIRM_PROGRAMS_ROOT", programs_root)

    result = runner.invoke(app, ["confirm", "--edition", EDITION_NAME, "--issue", "1"])

    assert result.exit_code == 3
    assert "Confirm blocked for issue 001." in result.stdout
    assert not (get_archive_root(EDITION_NAME, archive_root) / "snapshots" / "issue_001.snapshot.json").exists()


def test_confirm_issue_blocks_when_exec_summary_scaffold_placeholders_remain(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=__import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    blocked = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert any("unresolved scaffold placeholders" in failure for failure in blocked.failures)


def test_confirm_issue_v2_records_claims_and_decision_asks(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=__import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(
        programs_root,
        captured_at=__import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc),
    )

    overrides_path = programs_root / "acme" / "overrides" / "issue_001.yaml"
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    # Disable AI claim extraction so regex extractor is used deterministically,
    # regardless of whether VERTEX_AI_DEPLOYMENT is set in the local environment.
    _set_program_ai_claim_extractor(programs_root, enabled=False, mode="calibration")

    narratives_dir = programs_root / "acme" / "narratives" / "issue_001"
    narratives_dir.mkdir(parents=True, exist_ok=True)
    (narratives_dir / "exec_summary.md").write_text(
        "WI:900001 Deployment readiness expected by June 15. Need LT decision on SCHIE timeline.\n",
        encoding="utf-8",
    )
    _write_authored_workstream_narratives(reports_root, 1, edition_name="acme_weekly")

    result = confirm_issue(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    claims = load_claim_entries("acme", programs_root)
    asks = load_decision_asks("acme", programs_root)

    assert result.exit_code == 0
    assert result.archive_paths is not None
    assert result.archive_paths.vitality_path is not None and result.archive_paths.vitality_path.exists()
    vitality_payload = json.loads(result.archive_paths.vitality_path.read_text(encoding="utf-8"))
    assert any(claim.text == "WI:900001 Deployment readiness expected by June 15" for claim in claims)
    assert any(ask.text == "Need LT decision on SCHIE timeline" for ask in asks)
    assert any("Claim tracker recorded 1 claim(s) and 1 decision ask(s)." in warning for warning in result.warnings)
    assert vitality_payload["entries"][0]["issue_number"] == 1
    assert "per_workstream" in vitality_payload["entries"][0]
    assert "per_owner" in vitality_payload["entries"][0]


def test_confirm_issue_v2_uses_ai_claim_extractor_in_calibration_mode(repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_program_ai_claim_extractor(programs_root, enabled=True, mode="calibration")

    generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(programs_root, captured_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc))

    overrides_path = programs_root / "acme" / "overrides" / "issue_001.yaml"
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    narratives_dir = programs_root / "acme" / "narratives" / "issue_001"
    narratives_dir.mkdir(parents=True, exist_ok=True)
    (narratives_dir / "exec_summary.md").write_text(
        "Delivery remains under discussion with leadership and needs a SCHIE decision to proceed.\n",
        encoding="utf-8",
    )
    _write_authored_workstream_narratives(reports_root, 1, edition_name="acme_weekly")

    class _FakeExtractor:
        def extract_claims(self, **kwargs):
            del kwargs
            return ClaimExtractionResult(
                claims=(
                    ClaimEntry(
                        id="ai-claim-test",
                        program_id="acme",
                        edition_id="acme_weekly",
                        issue_number=1,
                        workstream_id="acme",
                        text="AI captured readiness commitment",
                        entity_refs=("WI:900001",),
                        claim_date=date(2026, 5, 5),
                        owner_alias="owner",
                        due_date=date(2026, 6, 15),
                    ),
                ),
                decision_asks=(
                    DecisionAsk(
                        id="ai-ask-test",
                        program_id="acme",
                        edition_id="acme_weekly",
                        issue_number=1,
                        text="AI captured LT decision ask",
                        entity_refs=("WI:900001",),
                        ask_date=date(2026, 5, 5),
                        owner_alias="lt",
                    ),
                ),
            )

    monkeypatch.setattr(
        claim_resolution.ClaimExtractor,
        "from_program",
        classmethod(lambda cls, program, trace_context=None: _FakeExtractor()),
    )

    result = confirm_issue(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    claims = load_claim_entries("acme", programs_root)
    asks = load_decision_asks("acme", programs_root)
    calibration_records = load_claim_extraction_calibration_records("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert result.manifest.qg_results["QG-18"] is True
    assert result.manifest.qg_results["QG-CE1"] is True
    assert any(claim.text == "AI captured readiness commitment" for claim in claims)
    assert any(ask.text == "AI captured LT decision ask" for ask in asks)
    assert calibration_records and calibration_records[-1].mode == "calibration"


def test_confirm_issue_v2_surfaces_qg18_when_ai_and_regex_claims_diverge(repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_program_ai_claim_extractor(programs_root, enabled=True, mode="calibration")

    generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(programs_root, captured_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc))

    overrides_path = programs_root / "acme" / "overrides" / "issue_001.yaml"
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    narratives_dir = programs_root / "acme" / "narratives" / "issue_001"
    narratives_dir.mkdir(parents=True, exist_ok=True)
    (narratives_dir / "exec_summary.md").write_text(
        "Delivery remains under discussion with leadership and needs a SCHIE decision to proceed.\n",
        encoding="utf-8",
    )
    _write_authored_workstream_narratives(reports_root, 1, text="Narrative without regex claim hints.\n", edition_name="acme_weekly")

    class _DivergentExtractor:
        def extract_claims(self, **kwargs):
            del kwargs
            return ClaimExtractionResult(
                claims=(
                    ClaimEntry(
                        id="ai-claim-1",
                        program_id="acme",
                        edition_id="acme_weekly",
                        issue_number=1,
                        workstream_id="acme",
                        text="AI captured readiness commitment",
                        entity_refs=("WI:900001",),
                        claim_date=date(2026, 5, 5),
                        owner_alias="owner",
                        due_date=date(2026, 6, 15),
                    ),
                    ClaimEntry(
                        id="ai-claim-2",
                        program_id="acme",
                        edition_id="acme_weekly",
                        issue_number=1,
                        workstream_id="acme",
                        text="AI captured repair checkpoint",
                        entity_refs=("WI:900002",),
                        claim_date=date(2026, 5, 5),
                        owner_alias="owner",
                        due_date=date(2026, 6, 20),
                    ),
                    ClaimEntry(
                        id="ai-claim-3",
                        program_id="acme",
                        edition_id="acme_weekly",
                        issue_number=1,
                        workstream_id="acme",
                        text="AI captured compliance milestone",
                        entity_refs=("WI:900003",),
                        claim_date=date(2026, 5, 5),
                        owner_alias="owner",
                        due_date=date(2026, 6, 25),
                    ),
                ),
                decision_asks=(),
            )

    monkeypatch.setattr(
        claim_resolution.ClaimExtractor,
        "from_program",
        classmethod(lambda cls, program, trace_context=None: _DivergentExtractor()),
    )

    result = confirm_issue(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    calibration_records = load_claim_extraction_calibration_records("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert result.manifest.qg_results["QG-18"] is True
    assert result.manifest.qg_results["QG-CE1"] is False
    assert any("Forced past QG-CE1" in warning for warning in result.warnings)
    assert calibration_records and calibration_records[-1].ai_only_count >= 3


def test_confirm_issue_v2_falls_back_to_regex_when_ai_claim_extraction_fails(repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_program_ai_claim_extractor(programs_root, enabled=True, mode="production")

    generate_report_draft(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(programs_root, captured_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc))

    overrides_path = programs_root / "acme" / "overrides" / "issue_001.yaml"
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    narratives_dir = programs_root / "acme" / "narratives" / "issue_001"
    narratives_dir.mkdir(parents=True, exist_ok=True)
    (narratives_dir / "exec_summary.md").write_text(
        "WI:900001 Deployment readiness expected by June 15. Need LT decision on SCHIE timeline.\n",
        encoding="utf-8",
    )
    _write_authored_workstream_narratives(reports_root, 1, edition_name="acme_weekly")

    class _FailingExtractor:
        def extract_claims(self, **kwargs):
            del kwargs
            raise claim_resolution.ClaimExtractorError("AI claim extraction failed: Azure OpenAI structured response returned invalid JSON")

    monkeypatch.setattr(
        claim_resolution.ClaimExtractor,
        "from_program",
        classmethod(lambda cls, program, trace_context=None: _FailingExtractor()),
    )

    result = confirm_issue(
        edition_name="acme_weekly",
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    claims = load_claim_entries("acme", programs_root)

    assert result.exit_code == 0
    assert any(claim.text == "WI:900001 Deployment readiness expected by June 15" for claim in claims)
    assert any("invalid structured output" in warning for warning in result.warnings)

def test_confirm_issue_warns_on_stale_proposed_decisions(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(programs_root, captured_at=as_of)

    save_decisions(
        "acme",
        (
            DecisionEntry(
                id="decision-1",
                program_id="acme",
                title="SCHIE timeline approval",
                context="Timeline needs leadership alignment before partner commit.",
                decision="Await LT approval before locking external target.",
                rationale=None,
                alternatives_considered=(),
                decided_by="lt",
                decision_date=date(2026, 4, 15),
                status=DecisionStatus.PROPOSED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id="deployment_readiness",
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=programs_root,
    )

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _write_authored_exec_summary(reports_root, 1, edition_name=EDITION_NAME)
    _write_authored_workstream_narratives(reports_root, 1, edition_name=EDITION_NAME)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.exit_code == 0
    assert any("[DECISIONS] 1 proposed decision entry pending >14 days" in warning for warning in result.warnings)
    assert any("decision-1 (SCHIE timeline approval)" in warning for warning in result.warnings)


def test_build_stale_proposed_decision_warnings_reads_decisions_from_program_facts(
    monkeypatch,
    tmp_path: Path,
) -> None:
    reports_root = tmp_path / "reports"
    programs_root = tmp_path / "programs"
    captured: dict[str, object] = {}

    # _build_stale_proposed_decision_warnings now lives in
    # src/commands/confirm_stages/validation.py (D-25 decomposition); patch the
    # dependency names in that module's namespace where it resolves them.
    monkeypatch.setattr(
        "src.commands.confirm_stages.validation.resolve_edition",
        lambda edition_name, editions_root, programs_root: SimpleNamespace(
            program=SimpleNamespace(id="acme")
        ),
    )

    def _load_current_decision_entries(program_id: str, *, programs_root: Path):
        captured["program_id"] = program_id
        captured["programs_root"] = programs_root
        return (
            DecisionEntry(
                id="decision-1",
                program_id="acme",
                title="SCHIE timeline approval",
                context="Timeline needs leadership alignment before partner commit.",
                decision="Await LT approval before locking external target.",
                rationale=None,
                alternatives_considered=(),
                decided_by="lt",
                decision_date=date(2026, 4, 15),
                status=DecisionStatus.PROPOSED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id="deployment_readiness",
                entity_refs=("WI:900001",),
            ),
        )

    monkeypatch.setattr("src.commands.confirm_stages.validation.load_current_decision_entries", _load_current_decision_entries)

    warnings = confirm_module._build_stale_proposed_decision_warnings(
        edition_name="acme_weekly",
        as_of=date(2026, 5, 5),
        reports_root=reports_root,
    )

    assert captured == {
        "program_id": "acme",
        "programs_root": programs_root,
    }
    assert len(warnings) == 1
    assert "decision-1" in warnings[0]


def test_confirm_issue_force_does_not_override_qg11_at_l2(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(programs_root, captured_at=as_of)

    program_path = programs_root / "acme" / "program.yaml"
    program_payload = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_payload["maturity_level"] = 2
    program_path.write_text(yaml.safe_dump(program_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    append_claim_entry(
        ClaimEntry(
            id="claim-contradiction-1",
            program_id="acme",
            edition_id=EDITION_NAME,
            issue_number=1,
            workstream_id="networking",
            text="WI:900001 Deployment readiness expected by 2026-05-12.",
            entity_refs=("WI:900001",),
            claim_date=as_of.date(),
            owner_alias="owner",
            due_date=as_of.date() + __import__("datetime", fromlist=["timedelta"]).timedelta(days=7),
        ),
        programs_root=programs_root,
    )

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.exit_code == 2
    assert any("Open claims contradicted by current ADO state without acknowledgment" in failure for failure in result.failures)

def test_confirm_issue_v2_records_edit_patterns(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=__import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(
        programs_root,
        captured_at=__import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc),
    )

    issue_number = artifacts.issue_number
    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=issue_number)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    narrative_path = programs_root / "acme" / "narratives" / f"issue_{issue_number:03d}" / "exec_summary.md"
    narrative_path.write_text(
        "The blocking lane is deployment readiness, and leadership needs to decide the fallback timing this week.\n",
        encoding="utf-8",
    )
    _write_authored_workstream_narratives(reports_root, issue_number, edition_name="acme_weekly")

    result = confirm_issue(
        edition_name="acme_weekly",
        issue_number=issue_number,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    patterns = read_edit_patterns("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert len(patterns) == 1
    assert patterns[0].section_id == "exec_summary"
    assert "Author edits" in patterns[0].summary


def test_confirm_issue_v2_records_ai_prompt_version_in_edit_patterns(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    program_payload = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_payload, dict)
    ai_block = program_payload.setdefault("ai", {})
    assert isinstance(ai_block, dict)
    ai_block["enabled"] = True
    ai_block["exec_summary_deployment"] = "fake-exec"
    program_path.write_text(yaml.safe_dump(program_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    monkeypatch.setattr(
        "src.commands.report._load_draft_ai_context",
        lambda **kwargs: _DraftAIContext(
            program_id="acme",
            programs_root=programs_root,
            workstreams=(),
            rolling_summaries={},
            approved_signals=(),
            drift_patterns=(),
            dependency_cascades=(),
        ),
    )
    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage._create_ai_client", lambda **kwargs: object())
    monkeypatch.setattr(
        "src.commands.report_pipeline.assemble_stage.draft_exec_summary",
        lambda **kwargs: ExecSummaryDraft(
            text="AI draft highlights deployment readiness risk and asks for a fallback timing decision this week.",
            prompt_version="exec_summary_drafter.v1",
            cited_work_item_ids=(900001,),
            ai_confidence=Confidence.HIGH,
        ),
    )
    monkeypatch.setattr("src.commands.report_pipeline.assemble_stage.generate_workstream_blurb", lambda **kwargs: None)

    artifacts = generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=__import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(
        programs_root,
        captured_at=__import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc),
    )

    issue_number = artifacts.issue_number
    draft_state_path = programs_root / "acme" / "publications" / EDITION_NAME / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.draft.json"
    draft_state = json.loads(draft_state_path.read_text(encoding="utf-8"))
    assert draft_state["ai_prompt_versions"] == {"exec_summary": "exec_summary_drafter.v1"}
    assert draft_state["ai_confidences"] == {"exec_summary": "high"}
    assert str(draft_state["ai_trace_run_id"]).startswith("acme_weekly:issue-")

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=issue_number)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    narrative_path = programs_root / "acme" / "narratives" / f"issue_{issue_number:03d}" / "exec_summary.md"
    narrative_path.write_text(
        "The blocking lane is deployment readiness, and leadership needs to decide the fallback timing this week.\n",
        encoding="utf-8",
    )
    _write_authored_workstream_narratives(reports_root, issue_number, edition_name="acme_weekly")

    result = confirm_issue(
        edition_name="acme_weekly",
        issue_number=issue_number,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    patterns = read_edit_patterns("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert len(patterns) == 1
    assert patterns[0].task_type == "exec_summary"
    assert patterns[0].prompt_version == "exec_summary_drafter.v1"
    assert patterns[0].ai_confidence == Confidence.HIGH
    assert patterns[0].trace_run_id == draft_state["ai_trace_run_id"]
    assert patterns[0].author_override_magnitude is not None
    assert patterns[0].author_override_magnitude > 0


def test_confirm_issue_requires_forecast_ack(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    config_path = programs_root / "acme" / "editions" / f"{EDITION_NAME}.yaml"
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace("forecast_enabled: false", "forecast_enabled: true"),
        encoding="utf-8",
    )

    for issue_number in range(1, 5):
        as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 4, issue_number, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
        archive_store.write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=as_of,
                items=(
                    _snapshot_item_from_work_item(_forecast_items(as_of)[0], risk_level=RiskLevel.LOW),
                ),
                scorecard_risks={"Deployment Velocity": RiskLevel.LOW},
            ),
            html_body=f"<html><body>Issue {issue_number:03d}</body></html>",
            markdown_body=f"# Issue {issue_number:03d}\n",
            manifest=_manifest(issue_number=issue_number, as_of=as_of, edition=EDITION_NAME),
            archive_root=archive_root,
        )

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 10, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_forecast_items(timestamp), 0),
        open_browser=False,
    )

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=5)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _write_authored_exec_summary(reports_root, 5, edition_name=EDITION_NAME)
    _write_authored_workstream_narratives(
        reports_root, 5, "Confirmed workstream narrative. Status update for WI:900002.\n", edition_name=EDITION_NAME
    )

    blocked = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=5,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    if blocked.manifest.metadata.get("forecast_summary") is None:
        pytest.skip("Current continuity forecast fixture did not produce a forecast candidate.")

    assert any("Forecast present" in failure for failure in blocked.failures)

    allowed = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=5,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
        ack_forecast=True,
    )

    assert allowed.exit_code == 0


def test_confirm_issue_requires_stale_approval_ack(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    approved_at = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    initial = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=approved_at,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )
    initial_manifest_id = json.loads(initial.manifest_path.read_text(encoding="utf-8"))["manifest_id"]

    review_status = load_review_status(EDITION_NAME, reports_root=reports_root)
    assert review_status is not None
    save_review_status(
        EDITION_NAME,
        ReviewStatus(
            issue_number=review_status.issue_number,
            sections=tuple(
                ReviewSection(
                    section_id=section.section_id,
                    state=ReviewState.APPROVED,
                    reviewer="Lead PM",
                    note="Approved against prior draft.",
                    updated_at=approved_at,
                    manifest_id=initial_manifest_id,
                )
                for section in review_status.sections
            ),
        ),
        reports_root=reports_root,
    )

    refreshed_as_of = approved_at + __import__("datetime", fromlist=["timedelta"]).timedelta(days=1)
    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=refreshed_as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(programs_root, captured_at=refreshed_as_of)

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _write_authored_exec_summary(reports_root, 1, edition_name=EDITION_NAME)
    _write_authored_workstream_narratives(reports_root, 1, edition_name=EDITION_NAME)

    blocked = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert any("Stale approval + data changed" in failure for failure in blocked.failures)
    assert any("[STALE APPROVAL]" in warning for warning in blocked.warnings)

    allowed = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
        ack_stale_approval=True,
    )

    assert allowed.exit_code == 0
    assert allowed.manifest.metadata["overrode_stale_approval"] is True
    assert allowed.manifest.metadata["override_method"] == "interactive_confirm"


def test_confirm_issue_surfaces_malformed_draft_manifest(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(
        2026,
        5,
        5,
        18,
        0,
        tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc,
    )
    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.manifest_path is not None
    artifacts.manifest_path.write_text("{malformed", encoding="utf-8")

    with pytest.raises(typer.BadParameter, match="Manifest at .* is invalid"):
        confirm_issue(
            edition_name=EDITION_NAME,
            issue_number=1,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=programs_root,
            force=True,
        )


def test_confirm_issue_rolls_back_partial_archive_on_promotion_failure(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(programs_root, captured_at=as_of)

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _write_authored_exec_summary(reports_root, 1, edition_name=EDITION_NAME)
    _write_authored_workstream_narratives(reports_root, 1, edition_name=EDITION_NAME)

    original_promote = archive_store._promote_file_with_rollback
    call_count = {"value": 0}

    def fail_on_second_promotion(staged_path, final_path, rollback_root, rollback_entries):
        call_count["value"] += 1
        original_promote(staged_path, final_path, rollback_root, rollback_entries)
        if call_count["value"] == 2:
            raise RuntimeError("simulated archive promote failure")

    monkeypatch.setattr(archive_store, "_promote_file_with_rollback", fail_on_second_promotion)

    with pytest.raises(RuntimeError, match="simulated archive promote failure"):
        confirm_issue(
            edition_name=EDITION_NAME,
            issue_number=1,
            reports_root=reports_root,
            archive_root=archive_root,
            programs_root=programs_root,
            force=True,
        )

    edition_root = get_archive_root(EDITION_NAME, archive_root)
    active_overrides = load_overrides(EDITION_NAME, reports_root=reports_root)

    assert not (edition_root / "snapshots" / "issue_001.snapshot.json").exists()
    assert not (edition_root / "html" / "issue_001.html").exists()
    assert not (edition_root / "md" / "issue_001.md").exists()
    assert not (edition_root / "manifests" / "issue_001.json").exists()
    assert not (edition_root / "index.json").exists()
    assert not (edition_root / "scorecards.json").exists()
    assert not (edition_root / "staging").exists()
    assert active_overrides is not None and active_overrides.issue_number == 1


def test_confirm_issue_blocks_when_freshness_has_blocking_items(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)
    empty_report = QualityGateReport(results=())
    monkeypatch.setattr("src.commands.confirm.evaluate_context_integrity_gates", lambda **kwargs: empty_report)

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_freshness_blocking_items(timestamp), 0),
        open_browser=False,
    )

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _write_authored_exec_summary(reports_root, 1, edition_name=EDITION_NAME)
    _write_authored_workstream_narratives(reports_root, 1, edition_name=EDITION_NAME)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
    )

    assert result.exit_code == 2
    assert "Freshness gate failed with 1 blocking item(s)." in result.failures
    assert result.archive_paths is None


def test_confirm_issue_force_allows_archive_when_only_freshness_blocks(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_freshness_blocking_items(timestamp), 0),
        open_browser=False,
    )

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _write_authored_exec_summary(reports_root, 1, edition_name=EDITION_NAME)
    _write_authored_workstream_narratives(reports_root, 1, edition_name=EDITION_NAME)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.exit_code == 0
    assert result.archive_paths is not None
    assert "Forced past QG-1: Freshness gate failed with 1 blocking item(s)." in result.warnings


def test_confirm_issue_uses_visible_section_narratives_to_cover_high_risk_items(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=artifacts.issue_number)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    narratives_dir = get_narratives_dir(EDITION_NAME, artifacts.issue_number, reports_root)
    for narrative_path in narratives_dir.glob("*.md"):
        narrative_path.write_text("Authored section coverage without explicit work item IDs.\n", encoding="utf-8")

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=artifacts.issue_number,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.exit_code == 0
    assert not any("High-risk coverage gap on active items" in failure for failure in result.failures)
    assert not any("High-risk coverage gap on active items" in warning for warning in result.warnings)


def test_confirm_issue_surfaces_graduated_bridge_drift_as_warning(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    original_build_confirm_artifacts = __import__("src.commands.confirm", fromlist=["_build_confirm_artifacts"])._build_confirm_artifacts

    def _patched_build_confirm_artifacts(*args, **kwargs):
        artifacts = original_build_confirm_artifacts(*args, **kwargs)
        qg_report = QualityGateReport(
            results=(
                GateEvaluation(
                    "QG-B1",
                    False,
                    "Bridge section-roster drift remains advisory after graduation: added sections: new-section",
                    1,
                ),
            )
        )
        manifest = replace(
            artifacts[3],
            qg_results=qg_report.qg_results,
        )
        return (qg_report, artifacts[1], artifacts[2], manifest, artifacts[4], artifacts[5], artifacts[6], artifacts[7], artifacts[8])

    monkeypatch.setattr("src.commands.confirm._build_confirm_artifacts", _patched_build_confirm_artifacts)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        dry_run=True,
    )

    assert result.exit_code == 0
    assert result.failures == ()
    assert any("Bridge section-roster drift remains advisory after graduation" in warning for warning in result.warnings)


def test_confirm_issue_warns_when_medium_risk_narrative_is_empty(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=__import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(
        programs_root,
        captured_at=__import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc),
    )
    first_section = "acme-adventure-xio-100-ramp-readiness-schie-gaps"
    _set_override_risks_for_section(
        reports_root=reports_root,
        snapshot=artifacts.snapshot,
        section_id=first_section,
        risk=RiskLevel.MEDIUM,
        edition_name=EDITION_NAME,
    )
    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    overrides_payload["decision_strip_ack"] = {
        "no_leadership_ask": True,
        "reason": "Freshness signals are already tracked in ADO and do not require new leadership action for this confirm warning test.",
    }
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    exec_summary_path = get_narratives_dir(EDITION_NAME, 1, reports_root) / "exec_summary.md"
    exec_summary_path.write_text("Confirmed executive summary for the Medium-risk warning path.\n", encoding="utf-8")
    _write_authored_workstream_narratives(reports_root, 1, edition_name=EDITION_NAME)
    (get_narratives_dir(EDITION_NAME, 1, reports_root) / f"ws_{first_section}.md").write_text("", encoding="utf-8")

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.exit_code == 0
    assert result.archive_paths is not None
    assert any(f"Medium-risk section {first_section}" in warning for warning in result.warnings)


def test_confirm_issue_reuses_persisted_kusto_sections(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    reset_overrides_to_seed_state(reports_root)

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        kusto_query_executor=_confirm_kusto_query_results,
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(programs_root, captured_at=as_of)

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    exec_summary_path = get_narratives_dir(EDITION_NAME, 1, reports_root) / "exec_summary.md"
    exec_summary_path.write_text("Confirmed executive summary.\n", encoding="utf-8")
    _write_authored_workstream_narratives(reports_root, 1, edition_name=EDITION_NAME)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.archive_paths is not None
    draft_payload = json.loads((programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json").read_text(encoding="utf-8"))
    assert draft_payload["kusto_sections"]
    active_incidents = next(section for section in draft_payload["kusto_sections"] if section["title"] == "Active Incidents")
    assert active_incidents["rows"][0][-1]["href"] == "https://portal.microsofticm.com/imp/v3/incidents/details/12345"


def test_confirm_issue_tracks_ai_review_acceptance_from_review_artifact(repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(programs_root, captured_at=as_of)

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    review_artifact = DraftReviewArtifact(
        issue_number=1,
        info_messages=(),
        review_report=DraftReviewReport(
            issue_number=1,
            suggestions=(
                ReviewSuggestion(
                    category="leadership_question",
                    section_id="exec_summary",
                    suggestion_text="Add a direct answer for the likely leadership question.",
                    confidence=Confidence.MEDIUM,
                ),
            ),
            data_gaps=0,
            leadership_questions=1,
            cross_issue_flags=0,
            structural_notes=0,
        ),
        reviewed_sections={
            "exec_summary": artifacts.report.exec_summary_text,
        },
        rendered_kusto_query_ids=(),
    )
    _write_output_json(programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.review.json", review_artifact)

    exec_summary_path = get_narratives_dir(EDITION_NAME, 1, reports_root) / "exec_summary.md"
    exec_summary_path.write_text("Confirmed executive summary with direct leadership answer.\n", encoding="utf-8")
    _write_authored_workstream_narratives(reports_root, 1, edition_name=EDITION_NAME)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.exit_code == 0
    assert result.review_tracking_path is not None and result.review_tracking_path.exists()
    assert result.review_tracking_summary == "AI review tracking: 1 accepted, 0 dismissed."
    tracking_payload = json.loads(result.review_tracking_path.read_text(encoding="utf-8"))
    outcomes = {entry["suggestion"]["section_id"]: entry["outcome"] for entry in tracking_payload["suggestions"]}
    assert outcomes["exec_summary"] == "accepted"

def test_confirm_issue_records_learning_distillation_when_review_tracking_exists(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)
    reset_overrides_to_seed_state(reports_root)

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )
    _seed_high_risk_signal_coverage(programs_root, captured_at=as_of)

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

    review_artifact = DraftReviewArtifact(
        issue_number=1,
        info_messages=(),
        review_report=DraftReviewReport(
            issue_number=1,
            suggestions=(
                ReviewSuggestion(
                    category="leadership_question",
                    section_id="exec_summary",
                    suggestion_text="Add a direct answer for the likely leadership question.",
                    confidence=Confidence.MEDIUM,
                ),
            ),
            data_gaps=0,
            leadership_questions=1,
            cross_issue_flags=0,
            structural_notes=0,
        ),
        reviewed_sections={
            "exec_summary": artifacts.report.exec_summary_text,
        },
        rendered_kusto_query_ids=(),
    )
    _write_output_json(programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.review.json", review_artifact)

    captured_trace_contexts: list[object] = []

    class _FakeLearningDistiller:
        def distill(self, *, editorial_rules, tracking_reports):
            del editorial_rules
            assert tuple(report.issue_number for report in tracking_reports) == (1,)
            return LearningDistillation(
                tracked_issue_numbers=(1,),
                proposals=(
                    EditorialRuleProposal(
                        target="banned_openings",
                        action="append",
                        value="Current status is as follows",
                        rationale="Accepted edits repeatedly replaced a generic opener with a direct answer.",
                        supporting_issue_numbers=(1,),
                        supporting_examples=("Issue 001 accepted: exec summary was rewritten to answer directly.",),
                    ),
                ),
                prompt_version="learning_distiller.v1",
            )

    def _fake_build_learning_distiller(*, trace_context=None):
        captured_trace_contexts.append(trace_context)
        return _FakeLearningDistiller()

    monkeypatch.setattr("src.commands.confirm_stages.learning_distiller.build_learning_distiller", _fake_build_learning_distiller)

    exec_summary_path = get_narratives_dir(EDITION_NAME, 1, reports_root) / "exec_summary.md"
    exec_summary_path.write_text("Confirmed executive summary with direct leadership answer.\n", encoding="utf-8")
    _write_authored_workstream_narratives(reports_root, 1, edition_name=EDITION_NAME)

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert result.exit_code == 0
    assert len(captured_trace_contexts) == 1
    trace_context = captured_trace_contexts[0]
    assert trace_context is not None
    assert trace_context.edition == EDITION_NAME
    assert trace_context.caller == "src.commands.confirm._record_learning_distillation"
    assert trace_context.metadata["task_type"] == "learning_distillation"
    assert trace_context.metadata["issue_number"] == 1
    assert result.learning_md_path is not None and result.learning_md_path.exists()
    assert result.learning_json_path is not None and result.learning_json_path.exists()
    assert result.learning_summary == "AI learning distillation: 1 proposed rule update(s) from 1 tracked issue(s)."
    learning_payload = json.loads(result.learning_json_path.read_text(encoding="utf-8"))
    assert learning_payload["proposals"][0]["target"] == "banned_openings"
    assert learning_payload["tracked_issue_numbers"] == [1]


def test_confirm_cli_rejects_already_confirmed_issue(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"

    monkeypatch.setattr(confirm_module, "REPORTS_ROOT", reports_root)
    monkeypatch.setattr(confirm_module, "ARCHIVE_ROOT", archive_root)
    monkeypatch.setattr(confirm_module, "_CONFIRM_PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        confirm_module,
        "read_archive_index",
        lambda edition, archive_root=archive_root: ArchiveIndex(
            edition=edition,
            issues=(
                ArchiveEntry(
                    issue_number=1,
                    generated_at=datetime(2026, 5, 5, 9, 0, tzinfo=timezone.utc),
                    kind="confirmed",
                    eml_path=None,
                    html_path=str(archive_root / edition / "html" / "issue_001.html"),
                    md_path=str(archive_root / edition / "md" / "issue_001.md"),
                    snapshot_path=str(archive_root / edition / "snapshots" / "issue_001.snapshot.json"),
                    manifest_path=str(archive_root / edition / "manifests" / "issue_001.json"),
                    reason=None,
                    metadata={},
                ),
            ),
        ),
    )

    result = runner.invoke(
        app,
        ["confirm", "--edition", EDITION_NAME, "--issue", "1", "--force"],
    )

    assert result.exit_code == 1
    assert "already confirmed" in result.stdout.lower()


def _freshness_blocking_items(as_of):
    overdue_item, = _sample_items(as_of)[:1]
    return (
        overdue_item.__class__(
            id=overdue_item.id,
            type=overdue_item.type,
            title=overdue_item.title,
            state=overdue_item.state,
            assigned_to=overdue_item.assigned_to,
            assigned_to_email=overdue_item.assigned_to_email,
            area_path=overdue_item.area_path,
            iteration_path=overdue_item.iteration_path,
            target_date=as_of.date() - __import__("datetime", fromlist=["timedelta"]).timedelta(days=2),
            risk_level=overdue_item.risk_level,
            tags=list(overdue_item.tags),
            custom_fields=dict(overdue_item.custom_fields),
            revisions=list(overdue_item.revisions),
            comments=list(overdue_item.comments),
            fetched_at=overdue_item.fetched_at,
        ),
    )


def _confirmable_items(as_of):
    items = list(_sample_items(as_of))
    return tuple(
        replace(
            item,
            target_date=as_of.date() + __import__("datetime", fromlist=["timedelta"]).timedelta(days=10 + index),
            tags=[*item.tags, "RAMPP1"] if "RAMPP1" not in item.tags else item.tags,
        )
        for index, item in enumerate(items)
    )


def _confirm_kusto_query_results(query):
    if query.id == "velocity-p50":
        return [{"Snapshot": "Current", "P50Hours": 4.2, "P90Hours": 7.8}]
    if query.id == "fleet-health":
        return [
            {"Date": "2026-05-01", "HealthyPct": 98.2, "Nodes": 1200},
            {"Date": "2026-05-02", "HealthyPct": 98.9, "Nodes": 1218},
        ]
    if query.id == "icm-active":
        return [
            {
                "IncidentId": "ICM-12345",
                "Severity": "3",
                "Title": "Fleet capacity alert",
                "Status": "Active",
                "IncidentUrl": "https://portal.microsofticm.com/imp/v3/incidents/details/12345",
            }
        ]
    if query.id == "icm-mttr":
        return [{"Severity": "SEV3", "AvgHours": 3.8, "Count": 4}]
    if query.id == "readiness_observability_coverage":
        return [{"coverage_pct": 97.4, "CoveredTenantCount": 148, "ExpectedTenantCount": 152}]
    if query.id == "readiness_capacity_headroom":
        return [{"headroom_pct": 91.2, "WithinTarget": 83, "TotalDeployments": 91}]
    if query.id == "readiness_dora_fail_rate":
        return [{"fail_rate_pct": 2.1, "FailCount": 4, "ObservedChecks": 190}]
    if query.id == "bios-ap-shared-service-pct":
        return [{"IsGoodStorageTotal": 95.0, "IsGoodStorageGen7": 92.0, "IsGoodStorageGen8": 96.0, "IsGoodStorageGen9": 98.0}]
    if query.id == "wingtip-fleet-rollout-pct":
        return [{"RolloutPct": 88.5}]
    raise AssertionError(f"Unexpected Kusto query id: {query.id}")


def test_build_confirm_artifacts_deck_skips_html_renderer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """_build_confirm_artifacts must not call HTMLRenderer for DECK editions (Phase 1A HTML renderer rejects 'deck' type)."""
    import ast, inspect, textwrap
    from src.commands.confirm import _build_confirm_artifacts
    source = inspect.getsource(_build_confirm_artifacts)
    tree = ast.parse(textwrap.dedent(source))

    # Verify the guard exists: an If node whose test compares resolved_edition_type == EditionType.DECK
    found_deck_guard = False
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            test = node.test
            if isinstance(test, ast.Compare) and isinstance(test.left, ast.Attribute):
                if test.left.attr == "DECK":
                    found_deck_guard = True
                    break
            if isinstance(test, ast.Compare) and isinstance(test.left, ast.Name):
                if any(
                    isinstance(comp, ast.Attribute) and comp.attr == "DECK"
                    for comp in [test.comparators[0]] if test.comparators
                ):
                    found_deck_guard = True
                    break
    assert found_deck_guard, (
        "_build_confirm_artifacts is missing the DECK guard before HTMLRenderer.render(); "
        "EditionType.DECK triggers RenderError in Phase 1A HTML renderer"
    )

    # Verify that within the DECK branch, HTMLRenderer is NOT called
    class _DeckBranchVisitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.in_deck_branch = False
            self.html_renderer_called_in_deck_branch = False

        def visit_If(self, node: ast.If) -> None:
            test = node.test
            is_deck_check = False
            if isinstance(test, ast.Compare):
                for child in ast.walk(test):
                    if isinstance(child, ast.Attribute) and child.attr == "DECK":
                        is_deck_check = True
            if is_deck_check:
                prev = self.in_deck_branch
                self.in_deck_branch = True
                for stmt in node.body:
                    self.visit(stmt)
                self.in_deck_branch = prev
                for stmt in node.orelse:
                    self.visit(stmt)
            else:
                self.generic_visit(node)

        def visit_Call(self, node: ast.Call) -> None:
            if self.in_deck_branch:
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "render":
                    if isinstance(func.value, ast.Call):
                        inner = func.value.func
                        if isinstance(inner, ast.Name) and inner.id == "HTMLRenderer":
                            self.html_renderer_called_in_deck_branch = True
            self.generic_visit(node)

    visitor = _DeckBranchVisitor()
    visitor.visit(tree)
    assert not visitor.html_renderer_called_in_deck_branch, (
        "HTMLRenderer.render() must not be called inside the DECK branch of _build_confirm_artifacts"
    )


def _set_v2_program_teams_webhook_url(programs_root: Path, *, webhook_url: str) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    m365 = program_document.setdefault("m365", {})
    assert isinstance(m365, dict)
    m365["teams_incoming_webhook_url"] = webhook_url
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")

