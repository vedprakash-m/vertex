from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
import sqlite3
import shutil
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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
from src.core.analytics_store import _connect_analytics_db, get_program_analytics_dirty_path, get_program_analytics_store_path
from src.core.gather_state_store import GatherState
from src.core.models import ArchiveEntry, ArchiveIndex, WorkItem
from src.commands.confirm import confirm_issue
from src.commands.report import _DraftAIContext, _write_output_json, generate_report_draft
from src.ai.edit_learner import read_edit_patterns
import src.core.archive_store as archive_store
from src.core.config_loader import load_report_bundle
from src.core.decision_register import save_decisions
from src.core.exceptions import ConfigError
from src.core.journal import append_review_decision, append_signal, read_signals
from src.core.file_stores import FileTrajectoryStore
from src.core.models_v2 import TrajectoryPoint
from src.core.pipeline import capture_milestone_trajectory_snapshot
from src.core.quality_gates import fact_drift as fact_drift_module
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
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore
from src.core.store_factory import build_trajectory_store_for_program_id
from src.core.snapshot_store import get_archive_root
from src.core import quality_gates
from src.core.quality_gates import GateEvaluation, QualityGateReport
from src.core.trusted_baseline_store import load_trusted_baseline
from src.core.workstream_association_store import read_workstream_association_records
from tests.support.report_test_setup import disable_kusto_in_report_copy, reset_overrides_to_seed_state, stage_v2_report_workspace
from tests.unit.test_commands_report import _forecast_items as _report_forecast_items, _lookback_snapshot, _manifest, _sample_items as _report_sample_items, _seed_v2_report_layout, _set_override_risks_for_section, _set_v2_program_artifact_base_url, _snapshot_item_from_work_item
from tests.support.synthetic_report_source import synthetic_report_root as repo_root

runner = CliRunner()
EDITION_NAME = "acme_weekly"
_CONFIRMABLE_WORKSPACE_TEMPLATES: dict[str, Path] = {}
_CONFIRMABLE_WORKSPACE_HOLDERS: list[tempfile.TemporaryDirectory[str]] = []
_DEFAULT_STAGE_V2_REPORT_WORKSPACE = stage_v2_report_workspace
_DEFAULT_GENERATE_REPORT_DRAFT = generate_report_draft


@pytest.fixture(autouse=True)
def _isolate_fact_store_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Isolate ProgramFactStore DB and home-directory authority checks.

    Prevents xdist workers from sharing ~/.vertex/acme/vertex.sqlite3 and
    causing spurious QG-SG-20 drift-gate failures when confirm shadow-writes
    land in the shared DB between another test's pin and drift-check.
    Tests that explicitly pass db_root= are unaffected (db_root overrides env).
    """
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / ".vertex"))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))


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


@pytest.mark.parametrize("backend", ["file", "sqlite"])
@pytest.mark.parametrize("omit_linked_item", [False, True])
def test_milestone_trajectory_gate_binds_exact_assessment_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    backend: str,
    omit_linked_item: bool,
) -> None:
    store = (
        FileTrajectoryStore(programs_root=tmp_path)
        if backend == "file"
        else SQLiteTrajectoryStore(programs_root=tmp_path)
    )
    points = {
        item_id: TrajectoryPoint(
            date=date(2026, 5, item_id - 900000),
            state="Active",
            assigned_to=None,
            target_date=None,
            risk_level=None,
            area_path="",
        )
        for item_id in (900001, 900002)
    }
    for item_id, point in points.items():
        assert store.append("fixture", item_id, point)
    monkeypatch.setattr(
        fact_drift_module,
        "build_trajectory_store_for_program_id",
        lambda program_id, *, programs_root: store,
    )
    captured_ids = (900001,) if omit_linked_item else (900001, 900002)
    captured = capture_milestone_trajectory_snapshot(
        "fixture",
        store,
        {item_id: store.read("fixture", item_id) for item_id in captured_ids},
    )
    gate = quality_gates.evaluate_program_fact_drift_from_draft(
        draft_state={"edition_type": "detailed"},
        program_id="fixture",
        milestone_assessments=[
            {"milestone_id": "m1", "linked_work_item_ids": [900001, 900002]},
            {"milestone_id": "m2", "linked_work_item_ids": []},
        ],
        milestone_trajectory_snapshot=captured,
        programs_root=tmp_path,
    )
    if omit_linked_item:
        assert not gate.passed
        assert gate.results[0].gate_id == "QG-SG-20"
        assert "scope does not match" in gate.results[0].message
    else:
        assert gate.passed


def test_milestone_trajectory_gate_rejects_unbound_historical_assessments(
    tmp_path: Path,
) -> None:
    gate = quality_gates.evaluate_program_fact_drift_from_draft(
        draft_state={"edition_type": "detailed"},
        program_id="fixture",
        milestone_assessments=[{"milestone_id": "m1"}],
        milestone_trajectory_snapshot=None,
        programs_root=tmp_path,
    )
    assert not gate.passed
    assert gate.results[0].gate_id == "QG-SG-20"
    assert "item scope" in gate.results[0].message


@pytest.mark.parametrize(
    ("draft_state", "assessments"),
    [
        ({"edition_type": "detailed"}, []),
        ({"edition_type": "lookback"}, None),
    ],
)
def test_milestone_trajectory_gate_bypasses_unassessed_drafts_without_reading(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    draft_state: dict[str, str],
    assessments: object,
) -> None:
    def forbidden(*_args, **_kwargs):
        pytest.fail("Unassessed drafts must not read trajectory history.")

    monkeypatch.setattr(fact_drift_module, "build_trajectory_store_for_program_id", forbidden)
    gate = quality_gates.evaluate_program_fact_drift_from_draft(
        draft_state=draft_state,
        program_id="fixture",
        milestone_assessments=assessments,
        milestone_trajectory_snapshot={"malformed": True},
        programs_root=tmp_path,
    )
    assert gate.passed


@pytest.mark.parametrize(
    ("location", "value", "message"),
    [
        ("assessments", None, "metadata"),
        ("program_id", None, "program"),
        ("row", False, "metadata"),
        ("milestone_id", "", "scope"),
        ("duplicate_row", None, "scope"),
        ("linked_ids", [True], "item scope"),
        ("linked_ids", [0], "item scope"),
        ("linked_ids", [2, 1], "item scope"),
        ("linked_ids", [1, 1], "item scope"),
        ("capture", None, "capture"),
        ("extra_key", None, "shape"),
        ("schema_version", "2", "version or program"),
        ("program", "other", "version or program"),
        ("backend", "other", "backend"),
        ("items", None, "item list"),
        ("item", None, "item is invalid"),
        ("item", {}, "item is invalid"),
        ("work_item_id", True, "item ID"),
        ("work_item_id", 0, "item ID"),
        ("points", None, "points"),
        ("point", None, "point"),
        ("point", {}, "point"),
        ("date", None, "point"),
        ("date", "not-a-date", "point"),
        ("date", "20260505", "point"),
        ("state", None, "point"),
        ("target_date", "not-a-date", "point"),
        ("sha256", "incorrect", "digest"),
    ],
)
def test_milestone_trajectory_gate_rejects_malformed_capture_before_store_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
    value: object,
    message: str,
) -> None:
    from src.core.manifest_writer import hash_jsonable

    program_id = "fixture"
    assessments = [{"milestone_id": "m1", "linked_work_item_ids": [1]}]
    captured = {
        "schema_version": "1",
        "program_id": program_id,
        "backend": "file",
        "items": [{
            "work_item_id": 1,
            "points": [{"date": "2026-05-05", "state": "Active", "target_date": None}],
        }],
    }
    captured["sha256"] = hash_jsonable(captured)
    if location == "assessments":
        assessments = value
    elif location == "program_id":
        program_id = value
    elif location == "row":
        assessments[0] = value
    elif location == "milestone_id":
        assessments[0]["milestone_id"] = value
    elif location == "duplicate_row":
        assessments.append(dict(assessments[0]))
    elif location == "linked_ids":
        assessments[0]["linked_work_item_ids"] = value
    elif location == "capture":
        captured = value
    elif location == "extra_key":
        captured["extra"] = value
    elif location == "program":
        captured["program_id"] = value
    elif location in {"schema_version", "backend", "items", "sha256"}:
        captured[location] = value
    elif location == "item":
        captured["items"][0] = value
    elif location in {"work_item_id", "points"}:
        captured["items"][0][location] = value
    elif location == "point":
        captured["items"][0]["points"][0] = value
    else:
        captured["items"][0]["points"][0][location] = value

    def forbidden(*_args, **_kwargs):
        pytest.fail("Invalid captured evidence must fail before reading current trajectories.")

    monkeypatch.setattr(fact_drift_module, "build_trajectory_store_for_program_id", forbidden)
    gate = quality_gates.evaluate_program_fact_drift_from_draft(
        draft_state={"edition_type": "detailed"},
        program_id=program_id,
        milestone_assessments=assessments,
        milestone_trajectory_snapshot=captured,
        programs_root=tmp_path,
    )
    assert not gate.passed
    assert gate.results[0].gate_id == "QG-SG-20"
    assert message in gate.results[0].message


@pytest.mark.parametrize("change", ["unavailable", "backend", "points", "unchanged"])
def test_milestone_trajectory_gate_checks_selected_current_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    store = FileTrajectoryStore(programs_root=tmp_path)
    captured = capture_milestone_trajectory_snapshot("fixture", store, {1: ()})
    if change == "backend":
        current_store = SQLiteTrajectoryStore(programs_root=tmp_path)
    else:
        current_store = store
    if change == "points":
        store.append(
            "fixture", 1,
            TrajectoryPoint(
                date=date(2026, 5, 5), state="Active", assigned_to=None,
                target_date=None, risk_level=None, area_path="",
            ),
        )
    factory = Mock(return_value=current_store)
    if change == "unavailable":
        factory.side_effect = OSError("Synthetic unavailable history")
    monkeypatch.setattr(fact_drift_module, "build_trajectory_store_for_program_id", factory)
    gate = quality_gates.evaluate_program_fact_drift_from_draft(
        draft_state={"edition_type": "detailed"},
        program_id="fixture",
        milestone_assessments=[{"milestone_id": "m1", "linked_work_item_ids": [1]}],
        milestone_trajectory_snapshot=captured,
    )
    factory.assert_called_once_with("fixture")
    assert gate.passed is (change == "unchanged")
    if change != "unchanged":
        assert gate.results[0].gate_id == "QG-SG-20"
        assert {
            "unavailable": "unavailable",
            "backend": "backend changed",
            "points": "trajectories changed",
        }[change] in gate.results[0].message


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


def _build_confirmable_weekly_issue(
    repo_root: Path,
    workspace_root: Path,
    *,
    webhook_url: str | None = None,
    artifact_base_url: str | None = None,
) -> tuple[Path, Path, Path]:
    reports_root = stage_v2_report_workspace(repo_root, workspace_root)
    archive_root = workspace_root / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)
    if webhook_url is not None:
        _set_v2_program_teams_webhook_url(programs_root, webhook_url=webhook_url)
    if artifact_base_url is not None:
        _set_v2_program_artifact_base_url(programs_root, artifact_base_url=artifact_base_url)
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
    return reports_root, archive_root, (workspace_root / "programs" / "acme" / "publications")


def _prepare_confirmable_weekly_issue(
    repo_root: Path,
    tmp_path: Path,
    *,
    webhook_url: str | None = None,
    artifact_base_url: str | None = None,
) -> tuple[Path, Path, Path]:
    cache_allowed = (
        stage_v2_report_workspace is _DEFAULT_STAGE_V2_REPORT_WORKSPACE
        and generate_report_draft is _DEFAULT_GENERATE_REPORT_DRAFT
        and _confirmable_items.__name__ == "_confirmable_items"
    )
    if not cache_allowed:
        return _build_confirmable_weekly_issue(
            repo_root,
            tmp_path,
            webhook_url=webhook_url,
            artifact_base_url=artifact_base_url,
        )

    fingerprint = hashlib.sha256()
    config_files = [
        *sorted((repo_root / "programs").rglob("*.yaml")),
        *sorted((repo_root / "knowledge").rglob("*.yaml")),
        *sorted((repo_root / "knowledge").rglob("*.json")),
    ]
    for path in config_files:
        fingerprint.update(path.relative_to(repo_root).as_posix().encode("utf-8"))
        fingerprint.update(path.read_bytes())
    fingerprint.update(repr((webhook_url, artifact_base_url)).encode("utf-8"))
    template_key = fingerprint.hexdigest()
    template = _CONFIRMABLE_WORKSPACE_TEMPLATES.get(template_key)
    if template is None:
        holder = tempfile.TemporaryDirectory(prefix="vertex-confirmable-")
        template = Path(holder.name)
        original_db_path = os.environ.get("VERTEX_DB_PATH")
        os.environ["VERTEX_DB_PATH"] = str(template / "vertex-db")
        try:
            _build_confirmable_weekly_issue(
                repo_root,
                template,
                webhook_url=webhook_url,
                artifact_base_url=artifact_base_url,
            )
        finally:
            if original_db_path is None:
                os.environ.pop("VERTEX_DB_PATH", None)
            else:
                os.environ["VERTEX_DB_PATH"] = original_db_path
        _CONFIRMABLE_WORKSPACE_HOLDERS.append(holder)
        _CONFIRMABLE_WORKSPACE_TEMPLATES[template_key] = template

    shutil.copytree(
        template, tmp_path, dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("vertex-db"),
    )
    reports_root = tmp_path / "reports"
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    template_db = template / "vertex-db"
    if template_db.exists():
        target_db = Path(os.environ["VERTEX_DB_PATH"])
        if target_db.exists():
            shutil.rmtree(target_db)
        shutil.copytree(template_db, target_db)
    return reports_root, archive_root, programs_root / "acme" / "publications"


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


def test_confirm_issue_archives_outputs_and_resets_active_state(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    selected_roots: dict[str, Path | None] = {}
    for name in (
        "get_review_status_path",
        "get_narratives_dir",
        "reset_overrides_for_next_issue",
        "reset_narratives_for_next_issue",
        "reset_review_status",
    ):
        original = getattr(confirm_module, name)

        def capture_root(*args, _name=name, _original=original, **kwargs):
            selected_roots[_name] = kwargs.get("programs_root")
            return _original(*args, **kwargs)

        monkeypatch.setattr(confirm_module, name, capture_root)
    disable_kusto_in_report_copy(reports_root)

    as_of = __import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 5, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc)
    draft_artifacts = generate_report_draft(
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

    assert result.exit_code == 0, result.failures
    assert result.manifest.metadata["milestone_assessments"] == draft_artifacts.manifest.metadata["milestone_assessments"]
    assert (
        result.manifest.metadata["milestone_trajectory_snapshot"]
        == draft_artifacts.manifest.metadata["milestone_trajectory_snapshot"]
    )
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
    assert selected_roots == {
        "get_review_status_path": programs_root,
        "get_narratives_dir": programs_root,
        "reset_overrides_for_next_issue": programs_root,
        "reset_narratives_for_next_issue": programs_root,
        "reset_review_status": programs_root,
    }
    assert trusted_baseline is not None
    assert trusted_baseline.trusted_issue_number == 1
    assert trusted_baseline.history[-1].action == "established"


def test_confirm_issue_writes_analytics_projection(repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    with _connect_analytics_db("acme", programs_root=programs_root):
        pass

    def fail_projection(**kwargs):
        raise OSError("Synthetic analytics projection failure")

    monkeypatch.setattr("src.commands.confirm_stages.archive_transaction.project_confirmed_issue", fail_projection)

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
    assert get_semantic_index_path(EDITION_NAME, archive_root=archive_root, programs_root=programs_root).exists()
    state = load_semantic_index_state(EDITION_NAME, archive_root=archive_root, programs_root=programs_root)
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
    state = load_semantic_index_state(EDITION_NAME, archive_root=archive_root, programs_root=programs_root)
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
    recorded_gate_roots: list[Path | None] = []
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
    monkeypatch.setattr(
        confirm_module,
        "record_gate_failures_from_report",
        lambda *args, **kwargs: recorded_gate_roots.append(kwargs.get("programs_root")),
    )

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
    assert recorded_gate_roots == [programs_root]


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


def test_confirm_issue_force_does_not_override_claim_contradiction_gate(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    from src.core.quality_gates import narrative as narrative_gates

    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    monkeypatch.setattr(
        narrative_gates,
        "build_ado_reconcile_report",
        lambda **kwargs: SimpleNamespace(
            discrepancies=(SimpleNamespace(kind="claim_eta", context="claim-1", work_item_id=1001),)
        ),
    )
    monkeypatch.setattr(narrative_gates, "load_open_claims", lambda *args, **kwargs: ())
    qg11 = narrative_gates.evaluate_claim_contradiction_gate(
        items=(SimpleNamespace(id=1001),),
        program_id="acme",
        program_maturity_level=0,
        workstreams=(SimpleNamespace(id="velocity"),),
        programs_root=tmp_path,
    )
    real_phase_1b = confirm_module.evaluate_phase_1b_gates
    monkeypatch.setattr(
        confirm_module,
        "evaluate_phase_1b_gates",
        lambda **kwargs: QualityGateReport(
            results=tuple(r for r in real_phase_1b(**kwargs).results if r.gate_id != "QG-11") + (qg11,)
        ),
    )

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
    assert result.manifest.qg_results["QG-11"] is False
    assert not any("Forced past QG-11" in warning for warning in result.warnings)
    assert qg11.message in result.failures


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
    clock = Mock(wraps=datetime)
    clock.now.return_value = datetime(2026, 5, 10, 18, tzinfo=timezone.utc)
    monkeypatch.setattr("src.core.source_health.datetime", clock)
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
    post_summary = Mock(wraps=confirm_module._post_confirm_weekly_summary_card)
    monkeypatch.setattr(confirm_module, "_post_confirm_weekly_summary_card", post_summary)
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(
        repo_root, tmp_path,
        webhook_url="https://contoso.example/webhook",
        artifact_base_url="https://contoso.example/vertex-output",
    )
    programs_root = reports_root.parent / "programs"

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
    assert post_summary.call_args.kwargs["programs_root"] == programs_root
    assert result.posted_weekly_summary_card is True
    assert result.weekly_summary_card_path is not None and result.weekly_summary_card_path.exists()
    assert len(sent_payloads) == 1

    payload = json.loads(result.weekly_summary_card_path.read_text(encoding="utf-8"))
    assert payload == sent_payloads[0]
    assert result.archive_paths is not None
    assert "https://contoso.example/vertex-output/acme_weekly/issue_001/issue_001.html" in json.dumps(payload)


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
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(
        repo_root, tmp_path, webhook_url="https://contoso.example/webhook",
    )
    programs_root = reports_root.parent / "programs"

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


def test_confirm_issue_blocks_when_exec_summary_scaffold_placeholders_remain(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    original_find_placeholders = confirm_module.find_unresolved_scaffold_placeholders
    placeholder_roots: list[Path | None] = []

    def find_placeholders(*args, **kwargs):
        placeholder_roots.append(kwargs.get("programs_root"))
        return original_find_placeholders(*args, **kwargs)

    monkeypatch.setattr(confirm_module, "find_unresolved_scaffold_placeholders", find_placeholders)
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
    assert placeholder_roots == [programs_root]


def test_draft_and_confirm_count_vitality_touches_at_data_as_of(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_items = _confirmable_items

    def items_with_future_comment(as_of: datetime) -> tuple[WorkItem, ...]:
        items = list(original_items(as_of))
        item = items[1]
        items[1] = replace(
            item,
            comments=[replace(item.comments[0], created_date=as_of + timedelta(days=1))],
        )
        return tuple(items)

    monkeypatch.setitem(globals(), "_confirmable_items", items_with_future_comment)
    reports_root, archive_root, output_root = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    draft_html = (output_root / EDITION_NAME / "issue_001" / "issue_001.html").read_text(encoding="utf-8")

    result = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=reports_root.parent / "programs",
        force=True,
    )

    assert result.exit_code == 0, result.failures
    assert result.archive_paths is not None
    confirmed_html = result.archive_paths.html_path.read_text(encoding="utf-8")
    for html in (draft_html, confirmed_html):
        assert "<strong>Best documented:</strong> WI:900002" in html
        assert "1 ADO touch in the last 7 days" in html


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
        "WI:900001 Deployment readiness expected by June 15. Need LT decision on EXGATE timeline.\n",
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
    assert any(ask.text == "Need LT decision on EXGATE timeline" for ask in asks)
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
        "Delivery remains under discussion with leadership and needs a EXGATE decision to proceed.\n",
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
        "Delivery remains under discussion with leadership and needs a EXGATE decision to proceed.\n",
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
        "WI:900001 Deployment readiness expected by June 15. Need LT decision on EXGATE timeline.\n",
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


def test_confirm_issue_v2_budget_failure_keeps_regex_fallback_without_retry(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from src.ai.ai_mode import AIMode
    from src.ai.client import BudgetExceeded
    from src.ai.deployment_fallback import FallbackStructuredClient
    from src.core.ledger.event_log import read_events

    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    _set_program_ai_claim_extractor(programs_root, enabled=True, mode="production")
    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    generate_report_draft(
        edition_name="acme_weekly", issue_number=1,
        reports_root=reports_root, archive_root=archive_root, programs_root=programs_root,
        as_of=as_of, work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [], open_browser=False,
    )
    _seed_high_risk_signal_coverage(programs_root, captured_at=as_of)
    overrides_path = programs_root / "acme" / "overrides" / "issue_001.yaml"
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False), encoding="utf-8")
    narrative = programs_root / "acme" / "narratives" / "issue_001" / "exec_summary.md"
    narrative.write_text(
        "WI:900001 Deployment readiness expected by June 15. Need LT decision on EXGATE timeline.\n",
        encoding="utf-8",
    )
    _write_authored_workstream_narratives(
        reports_root, 1, text="Synthetic narrative.\n", edition_name="acme_weekly",
    )

    monkeypatch.setattr("src.ai.tiered_router.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.claim_extractor._extract_deterministic_claims", lambda **kwargs: (None, 0.0))
    provider = Mock()
    provider.structured.side_effect = BudgetExceeded("synthetic budget exhaustion")
    factory = Mock(return_value=provider)
    client = FallbackStructuredClient(
        deployments=("primary", "backup"), temperature=0.0, budget_usd=1.0, client_factory=factory,
    )
    monkeypatch.setattr(
        claim_resolution.ClaimExtractor, "from_program",
        classmethod(lambda cls, program, trace_context=None: cls(client=client)),
    )

    result = confirm_issue(
        edition_name="acme_weekly", issue_number=1,
        reports_root=reports_root, archive_root=archive_root, programs_root=programs_root, force=True,
    )

    assert result.exit_code == 0, (result.failures, result.warnings)
    assert result.archive_paths is not None
    assert any("Claim tracker skipped: AOAI cost guard triggered" in warning for warning in result.warnings)
    assert any(
        claim.text == "WI:900001 Deployment readiness expected by June 15"
        for claim in load_claim_entries("acme", programs_root)
    )
    factory.assert_called_once()
    provider.structured.assert_called_once()
    decisions = [
        event for event in read_events("acme", programs_root=programs_root)
        if event.event_type == "ai.release_decision.v1"
    ]
    assert len(decisions) == 1
    assert decisions[0].payload["terminal"] == "discarded"


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
                title="EXGATE timeline approval",
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
    assert any("decision-1 (EXGATE timeline approval)" in warning for warning in result.warnings)


@pytest.mark.parametrize("selected", [False, True])
def test_build_stale_proposed_decision_warnings_reads_decisions_from_program_facts(
    monkeypatch,
    tmp_path: Path,
    selected: bool,
) -> None:
    reports_root = tmp_path / "reports"
    selected_root = tmp_path / "selected" / "programs"
    if selected:
        selected_edition = selected_root / "acme" / "editions" / "acme_weekly.yaml"
        selected_edition.parent.mkdir(parents=True)
        selected_edition.write_text("program_id: acme\n", encoding="utf-8")
    programs_root = selected_root if selected else reports_root.parent / "programs"
    captured: dict[str, object] = {}

    # _build_stale_proposed_decision_warnings now lives in
    # src/commands/confirm_stages/validation.py (D-25 decomposition); patch the
    # dependency names in that module's namespace where it resolves them.
    def resolve_edition(edition_name, *, editions_root, programs_root):
        captured["edition"] = edition_name
        captured["editions_root"] = editions_root
        captured["resolved_programs_root"] = programs_root
        return SimpleNamespace(program=SimpleNamespace(id="acme"))

    monkeypatch.setattr("src.commands.confirm_stages.validation.resolve_edition", resolve_edition)

    def _load_current_decision_entries(program_id: str, *, programs_root: Path):
        captured["program_id"] = program_id
        captured["programs_root"] = programs_root
        return (
            DecisionEntry(
                id="decision-1",
                program_id="acme",
                title="EXGATE timeline approval",
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
        **({"programs_root": selected_root} if selected else {}),
    )

    assert captured["edition"] == "acme_weekly"
    assert captured["editions_root"] == programs_root.parent / "editions"
    assert captured["resolved_programs_root"] == programs_root
    assert captured["program_id"] == "acme"
    assert captured["programs_root"] == programs_root
    assert len(warnings) == 1
    assert "decision-1" in warnings[0]


@pytest.mark.parametrize("root_case", ["missing", "relative", "legacy_decoy"])
def test_stale_decision_warnings_reject_invalid_explicit_root_before_reads(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    root_case: str,
) -> None:
    from src.commands.confirm_stages import validation

    reports_root = tmp_path / "reports"
    reports_root.mkdir()
    if root_case == "missing":
        selected_root = tmp_path / "selected" / "missing-programs"
    elif root_case == "relative":
        selected_root = Path("selected-relative-programs")
    else:
        selected_root = tmp_path / "selected" / "programs"
        selected_root.mkdir(parents=True)
        decoy_editions = selected_root.parent / "editions"
        decoy_editions.mkdir()
        (decoy_editions / "acme_weekly.yaml").write_text("program_id: acme\n", encoding="utf-8")

    resolver_calls = []
    provider_calls = []
    monkeypatch.setattr(
        validation,
        "resolve_edition",
        lambda *args, **kwargs: resolver_calls.append((args, kwargs)),
    )
    monkeypatch.setattr(
        validation,
        "load_current_decision_entries",
        lambda *args, **kwargs: provider_calls.append((args, kwargs)) or (),
    )

    with pytest.raises(ConfigError):
        validation.build_stale_proposed_decision_warnings(
            edition_name="acme_weekly",
            as_of=date(2026, 5, 5),
            reports_root=reports_root,
            programs_root=selected_root,
        )

    assert resolver_calls == []
    assert provider_calls == []


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

    assert result.exit_code == 3
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
    edition_document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    edition_document.update(forecast_enabled=True, layout_mode="dashboard")
    config_path.write_text(
        yaml.safe_dump(edition_document),
        encoding="utf-8",
    )

    for issue_number in range(1, 7):
        as_of = datetime(2026, 4, 1, 18, tzinfo=timezone.utc) + timedelta(weeks=issue_number - 1)
        history_items = tuple(
            replace(item, state="Active", risk_level=RiskLevel.MEDIUM) if item.state == "Closed" else item
            for item in _forecast_items(as_of)
        )
        history_items = (replace(history_items[0], target_date=date(2026, 5, 14)), *history_items[1:])
        archive_store.write_confirmed_issue(
            edition=EDITION_NAME,
            issue_number=issue_number,
            snapshot=_lookback_snapshot(
                issue_number=issue_number,
                as_of=as_of,
                items=tuple(_snapshot_item_from_work_item(item, risk_level=item.risk_level) for item in history_items),
                scorecard_risks={"Deployment Velocity": RiskLevel.LOW, "Deployment Readiness": RiskLevel.LOW},
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

    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=7)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _write_authored_exec_summary(reports_root, 7, edition_name=EDITION_NAME)
    _write_authored_workstream_narratives(
        reports_root, 7, "Confirmed workstream narrative. Status update for WI:900002.\n", edition_name=EDITION_NAME
    )

    blocked = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=7,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        force=True,
    )

    assert blocked.manifest.metadata.get("forecast_summary") is not None
    assert blocked.manifest.metadata["forecast_confidence"] == "high"
    assert any("Forecast present" in failure for failure in blocked.failures)

    allowed = confirm_issue(
        edition_name=EDITION_NAME,
        issue_number=7,
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

    with pytest.raises(typer.BadParameter, match="Cannot read draft manifest .*JSONDecodeError"):
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
    first_section = "deployment_readiness"
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
    (get_narratives_dir(EDITION_NAME, 1, reports_root) / f"chapter_{first_section}.md").write_text("", encoding="utf-8")

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
    assert active_incidents["rows"][0][-1]["href"] == "https://incident.example.invalid/items/12345"


@pytest.mark.parametrize("gate_id", ("QG-21", "QG-22"))
@pytest.mark.parametrize("dry_run", (True, False))
@pytest.mark.parametrize("force", (True, False))
def test_confirm_preserves_recorded_chart_hard_failure(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, gate_id: str, dry_run: bool, force: bool,
) -> None:
    from src.core.view_models import KustoSectionData

    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)
    section = KustoSectionData(
        section_id="synthetic-chart", title="Synthetic chart", query_id="synthetic-chart",
        render_mode="chart_image", source_label="Synthetic", confidence="high",
        columns=(), rows=(), metrics=(), image_data_url=None, reference_url=None,
        caveats=(), message=None, is_degraded=gate_id == "QG-22",
        chart_png_size_bytes=102_401 if gate_id == "QG-21" else 0,
        chart_blocks_publish=gate_id == "QG-22", section_placement="exec_summary",
    )
    monkeypatch.setattr(
        "src.core.stages.render_stage.build_kusto_sections",
        lambda *args, **kwargs: ((section,), (), ()),
    )
    as_of = datetime.now(timezone.utc)
    draft = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )
    assert draft.manifest.qg_results[gate_id] is False
    _seed_high_risk_signal_coverage(programs_root, captured_at=as_of)
    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    (get_narratives_dir(EDITION_NAME, 1, reports_root) / "exec_summary.md").write_text(
        "Confirmed executive summary.\n", encoding="utf-8",
    )
    _write_authored_workstream_narratives(reports_root, 1, edition_name=EDITION_NAME)

    assert draft.manifest_path is not None
    original_manifest = draft.manifest_path.read_bytes()
    draft_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json"
    original_draft = draft_path.read_bytes()
    control_draft = json.loads(original_draft)
    control_manifest = json.loads(original_manifest)
    control_manifest["qg_results"].pop(gate_id)
    _rebind_chart_test_artifacts(control_draft, control_manifest)
    draft_path.write_text(json.dumps(control_draft), encoding="utf-8")
    draft.manifest_path.write_text(json.dumps(control_manifest), encoding="utf-8")
    control = confirm_issue(
        edition_name=EDITION_NAME, issue_number=1, reports_root=reports_root,
        archive_root=archive_root, programs_root=programs_root, force=True, dry_run=True,
    )
    assert control.exit_code == 0
    assert control.archive_paths is None
    draft.manifest_path.write_bytes(original_manifest)
    draft_path.write_bytes(original_draft)

    result = confirm_issue(
        edition_name=EDITION_NAME, issue_number=1, reports_root=reports_root,
        archive_root=archive_root, programs_root=programs_root, force=force, dry_run=dry_run,
    )

    assert result.exit_code == 3
    assert result.archive_paths is None
    assert result.manifest.qg_results[gate_id] is False
    assert any(gate_id in failure for failure in result.failures)
    assert not any(f"Forced past {gate_id}" in warning for warning in result.warnings)
    assert not archive_root.exists() or not any(archive_root.rglob("*"))


@pytest.mark.parametrize("hidden_at", ["never", "draft", "confirm"])
def test_narrative_draft_and_confirm_respect_removed_executive_chart(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hidden_at: str,
) -> None:
    from src.core.view_models import KustoSectionData

    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    edition_path = programs_root / "acme" / "editions" / f"{EDITION_NAME}.yaml"
    edition = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    edition["layout_mode"] = "dashboard"
    edition_path.write_text(yaml.safe_dump(edition), encoding="utf-8")
    options = dict(
        edition_name=EDITION_NAME, reports_root=reports_root, archive_root=archive_root,
        programs_root=programs_root, as_of=as_of, edition_type_override="narrative",
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )
    generate_report_draft(**options)
    _write_authored_exec_summary(reports_root, 1, "Synthetic executive narrative.\n")
    _write_authored_workstream_narratives(reports_root, 1)
    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    if hidden_at == "draft":
        overrides["removed_sections"] = ["exec_summary"]
        overrides_path.write_text(yaml.safe_dump(overrides), encoding="utf-8")
    chart = KustoSectionData(
        section_id="synthetic-executive", title="Synthetic executive chart", query_id="synthetic-executive",
        render_mode="chart", source_label="Synthetic source", confidence="high", columns=(), rows=(), metrics=(),
        image_data_url=None, reference_url="https://example.invalid/chart", caveats=(), message="Synthetic trend",
        is_degraded=False, captured_at=as_of, chart_png_base64="c3ludGhldGlj", chart_png_size_bytes=9,
        section_placement="exec_summary",
    )
    monkeypatch.setattr("src.core.stages.render_stage.build_kusto_sections", lambda *args, **kwargs: ((chart,), (), ()))
    draft = generate_report_draft(**options)
    for body in (draft.html_body, draft.markdown_body):
        assert ("Synthetic executive narrative" in body) is (hidden_at != "draft")
        assert ("Synthetic executive chart" in body) is (hidden_at != "draft")

    if hidden_at == "confirm":
        overrides["removed_sections"] = ["exec_summary"]
        overrides_path.write_text(yaml.safe_dump(overrides), encoding="utf-8")
    result = confirm_issue(
        edition_name=EDITION_NAME, issue_number=1, reports_root=reports_root,
        archive_root=archive_root, programs_root=programs_root, force=True,
    )
    assert result.exit_code == 0
    assert result.archive_paths is not None
    for path in (result.archive_paths.html_path, result.archive_paths.md_path):
        body = path.read_text(encoding="utf-8")
        assert ("Synthetic executive narrative" in body) is (hidden_at == "never")
        assert ("Synthetic executive chart" in body) is (hidden_at == "never")


@pytest.mark.parametrize("hide_executive", [False, True])
def test_confirm_preserves_reviewed_charts_with_unmatched_workstream_fallback(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, hide_executive: bool,
) -> None:
    from html.parser import HTMLParser
    from src.commands.confirm_stages.draft_loaders import load_confirm_draft_manifest
    from src.core.view_models import KustoSectionData

    class SectionImages(HTMLParser):
        def __init__(self, anchor: str) -> None:
            super().__init__()
            self.anchor = anchor
            self.waiting = False
            self.depth = 0
            self.images = []

        def handle_starttag(self, tag, attrs):
            attributes = dict(attrs)
            if tag == "a" and attributes.get("id") == self.anchor:
                self.waiting = True
            elif tag == "table" and (self.waiting or self.depth):
                self.waiting = False
                self.depth += 1
            elif tag == "img" and self.depth:
                self.images.append(attributes)

        def handle_startendtag(self, tag, attrs):
            self.handle_starttag(tag, attrs)

        def handle_endtag(self, tag):
            if tag == "table" and self.depth:
                self.depth -= 1

    reports_root, archive_root, _ = _prepare_confirmable_weekly_issue(repo_root, tmp_path)
    programs_root = reports_root.parent / "programs"
    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    edition_path = programs_root / "acme" / "editions" / f"{EDITION_NAME}.yaml"
    edition = yaml.safe_load(edition_path.read_text(encoding="utf-8"))
    edition["layout_mode"] = "dashboard"
    edition_path.write_text(yaml.safe_dump(edition), encoding="utf-8")
    generate_report_draft(
        edition_name=EDITION_NAME, reports_root=reports_root, archive_root=archive_root,
        programs_root=programs_root, as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )
    _write_authored_workstream_narratives(reports_root, 1)
    if hide_executive:
        overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
        overrides = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
        overrides["removed_sections"] = ["exec_summary"]
        overrides_path.write_text(yaml.safe_dump(overrides), encoding="utf-8")
    charts = tuple(
        KustoSectionData(
            section_id=name, title=name, query_id=name, render_mode="chart",
            source_label="Synthetic source", confidence="high", columns=(), rows=(), metrics=(),
            image_data_url=None, reference_url="https://example.invalid/chart", caveats=(),
            message="Synthetic trend", is_degraded=False, captured_at=as_of,
            chart_png_base64="c3ludGhldGlj", chart_png_size_bytes=9,
            section_placement=placement,
        )
        for name, placement in (
            ("Executive chart", "exec_summary"),
            ("Workstream chart", "workstream:deployment_readiness"),
            ("Standalone chart", "standalone"),
            ("Second executive chart", "exec_summary"),
            ("Missing workstream chart", "workstream:missing"),
        )
    )
    monkeypatch.setattr(
        "src.core.stages.render_stage.build_kusto_sections",
        lambda *args, **kwargs: (charts, (), ()),
    )
    draft = generate_report_draft(
        edition_name=EDITION_NAME, reports_root=reports_root, archive_root=archive_root,
        programs_root=programs_root, as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )
    for chart in charts:
        expected = 0 if hide_executive and chart.title == "Executive chart" else 1
        assert draft.html_body.count(f"{chart.title} chart: Synthetic trend") == expected
        assert draft.markdown_body.count(
            f"{chart.title}: Synthetic trend. [View chart →](https://example.invalid/chart)"
        ) == expected

    assert draft.manifest_path is not None
    draft_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json"
    original_draft = draft_path.read_bytes()
    original_manifest = draft.manifest_path.read_bytes()
    original_state = json.loads(original_draft)
    original_metadata = json.loads(original_manifest)
    assert len(original_state["routed_chart_sections"]) == 2
    for change in ("image", "missing_routed", "missing_marker", "invalid_marker"):
        state = json.loads(original_draft)
        manifest = json.loads(original_manifest)
        if change == "image":
            state["routed_chart_sections"][0]["chart_png_base64"] = "Y2hhbmdlZA=="
        elif change == "missing_routed":
            state.pop("routed_chart_sections")
        elif change == "missing_marker":
            manifest["metadata"].pop("chart_routing_capture")
        else:
            manifest["metadata"]["chart_routing_capture"] = "v2"
        draft.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with pytest.raises(typer.BadParameter, match="chart.*binding"):
            load_confirm_draft_manifest(EDITION_NAME, 1, draft_state=state, programs_root=programs_root)
    draft.manifest_path.write_bytes(original_manifest)
    assert original_metadata["metadata"]["chart_routing_capture"] == "v1"
    tampered_state = json.loads(original_draft)
    tampered_state["routed_chart_sections"].clear()
    draft_path.write_text(json.dumps(tampered_state), encoding="utf-8")
    with pytest.raises(typer.BadParameter, match="chart.*binding"):
        confirm_issue(
            edition_name=EDITION_NAME, issue_number=1, reports_root=reports_root,
            archive_root=archive_root, programs_root=programs_root, force=True,
        )
    assert not archive_root.exists() or not any(archive_root.rglob("*"))
    program_archive = programs_root / "acme" / "archive"
    assert not program_archive.exists() or not any(program_archive.rglob("*"))
    draft_path.write_bytes(original_draft)

    result = confirm_issue(
        edition_name=EDITION_NAME, issue_number=1, reports_root=reports_root,
        archive_root=archive_root, programs_root=programs_root, force=True,
    )
    assert result.exit_code == 0
    assert result.archive_paths is not None
    html = result.archive_paths.html_path.read_text(encoding="utf-8")
    markdown = result.archive_paths.md_path.read_text(encoding="utf-8")
    for chart in charts:
        expected = 0 if hide_executive and chart.title == "Executive chart" else 1
        assert html.count(f"{chart.title} chart: Synthetic trend") == expected
        assert markdown.count(f"{chart.title}: Synthetic trend. [View chart →](https://example.invalid/chart)") == expected
    for body, workstream_anchor in (
        (draft.html_body, "chapter-deployment_readiness"),
        (html, "Workstream chart"),
    ):
        for anchor, title in (
            ("exec-summary", "Executive chart"),
            (workstream_anchor, "Workstream chart"),
            ("Standalone chart", "Standalone chart"),
            ("Second executive chart", "Second executive chart"),
            ("Missing workstream chart", "Missing workstream chart"),
        ):
            if hide_executive and title == "Executive chart":
                continue
            section = SectionImages(anchor)
            section.feed(body)
            assert [(image.get("src"), image.get("alt")) for image in section.images] == [
                ("data:image/png;base64,c3ludGhldGlj", f"{title} chart: Synthetic trend"),
            ]
        expected_count = len(charts) - int(hide_executive)
        assert body.count('href="https://example.invalid/chart"') >= expected_count
        assert body.count("Source: Synthetic source") == expected_count
        assert body.count("May 05 2026, 18:00 UTC") >= expected_count


@pytest.mark.parametrize("dry_run", (True, False))
@pytest.mark.parametrize("placement", ("standalone", "exec_summary", "pre_fallback"))
def test_confirm_blocks_retained_blocking_chart_that_expires_after_draft(
    repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dry_run: bool, placement: str,
) -> None:
    from src.core.view_models import KustoSectionData

    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = reports_root.parent / "programs"
    disable_kusto_in_report_copy(reports_root)
    as_of = datetime.now(timezone.utc)
    section = KustoSectionData(
        section_id="synthetic-chart", title="Synthetic chart", query_id="synthetic-chart",
        render_mode="chart_image", source_label="Synthetic", confidence="high",
        columns=(), rows=(), metrics=(), image_data_url=None, reference_url=None,
        caveats=(), message=None, is_degraded=True, captured_at=as_of,
        cache_captured_at=as_of, chart_blocks_publish=True, chart_cache_ttl_hours=26,
        section_placement="standalone" if placement == "pre_fallback" else placement,
    )
    monkeypatch.setattr(
        "src.core.stages.render_stage.build_kusto_sections",
        lambda *args, **kwargs: ((section,), (), ()),
    )
    if placement == "pre_fallback":
        monkeypatch.setattr(
            "src.core.stages.render_stage._apply_kusto_fallbacks",
            lambda *, ctx, kusto_sections, telemetry_observations, kusto_warnings, ado_vitality_available:
                ((), telemetry_observations, kusto_warnings),
        )
    draft = generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_confirmable_items(timestamp), 0),
        open_browser=False,
    )
    assert draft.manifest.qg_results["QG-22"] is True
    assert [row["query_id"] for row in draft.manifest.metadata["chart_freshness_evidence"]] == ["synthetic-chart"]
    _seed_high_risk_signal_coverage(programs_root, captured_at=as_of)
    overrides_path = get_overrides_path(EDITION_NAME, reports_root, issue_number=1)
    overrides_payload = yaml.safe_load(overrides_path.read_text(encoding="utf-8"))
    for scorecard in overrides_payload["scorecards"].values():
        for dimension in scorecard.values():
            dimension["risk"] = "low"
    _ack_decision_strip(overrides_payload)
    overrides_path.write_text(yaml.safe_dump(overrides_payload, sort_keys=False, allow_unicode=True), encoding="utf-8")
    (get_narratives_dir(EDITION_NAME, 1, reports_root) / "exec_summary.md").write_text(
        "Confirmed executive summary.\n", encoding="utf-8",
    )
    _write_authored_workstream_narratives(reports_root, 1, edition_name=EDITION_NAME)

    fresh = confirm_issue(
        edition_name=EDITION_NAME, issue_number=1, reports_root=reports_root,
        archive_root=archive_root, programs_root=programs_root, force=True, dry_run=True,
    )
    assert fresh.exit_code == 0
    assert "QG-22" not in fresh.manifest.qg_results

    if placement == "exec_summary":
        draft_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json"
        original_draft = draft_path.read_bytes()
        changed_edition = json.loads(original_draft)
        assert draft.manifest.metadata["chart_binding_edition_type"] == changed_edition["edition_type"]
        changed_edition["edition_type"] = "lookback"
        draft_path.write_text(json.dumps(changed_edition), encoding="utf-8")
        with pytest.raises(typer.BadParameter, match="chart.*binding"):
            confirm_issue(
                edition_name=EDITION_NAME, issue_number=1, reports_root=reports_root,
                archive_root=archive_root, programs_root=programs_root, force=True, dry_run=dry_run,
            )
        assert not archive_root.exists() or not any(archive_root.rglob("*"))
        draft_path.write_bytes(original_draft)

    if placement in ("exec_summary", "pre_fallback"):
        assert draft.manifest_path is not None
        original_manifest = draft.manifest_path.read_bytes()
        substituted_manifest = json.loads(original_manifest)
        substituted_manifest["metadata"]["chart_freshness_evidence"] = []
        draft.manifest_path.write_text(json.dumps(substituted_manifest), encoding="utf-8")
        with pytest.raises(typer.BadParameter, match="chart.*binding"):
            confirm_issue(
                edition_name=EDITION_NAME, issue_number=1, reports_root=reports_root,
                archive_root=archive_root, programs_root=programs_root, force=True, dry_run=dry_run,
            )
        assert not archive_root.exists() or not any(archive_root.rglob("*"))
        draft.manifest_path.write_bytes(original_manifest)

        draft_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json"
        original_draft = draft_path.read_bytes()
        unbound_draft = json.loads(original_draft)
        unbound_draft.pop("chart_gate_evidence_sha256")
        unbound_manifest = json.loads(original_manifest)
        unbound_manifest["metadata"].pop("chart_freshness_evidence")
        draft_path.write_text(json.dumps(unbound_draft), encoding="utf-8")
        draft.manifest_path.write_text(json.dumps(unbound_manifest), encoding="utf-8")
        with pytest.raises(typer.BadParameter, match="chart.*binding"):
            confirm_issue(
                edition_name=EDITION_NAME, issue_number=1, reports_root=reports_root,
                archive_root=archive_root, programs_root=programs_root, force=True, dry_run=dry_run,
            )
        assert not archive_root.exists() or not any(archive_root.rglob("*"))
        draft_path.write_bytes(original_draft)
        draft.manifest_path.write_bytes(original_manifest)

    draft_path = programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.draft.json"
    original_draft = draft_path.read_bytes()
    invalid_draft = json.loads(original_draft)
    if placement == "standalone":
        retained_chart = next(s for s in invalid_draft["kusto_sections"] if s["query_id"] == "synthetic-chart")
        retained_chart["chart_cache_ttl_hours"] = 0
        assert draft.manifest_path is not None
        _rebind_chart_test_artifacts(invalid_draft, json.loads(draft.manifest_path.read_bytes()))
        draft_path.write_text(json.dumps(invalid_draft), encoding="utf-8")
        with pytest.raises(typer.BadParameter, match="blocking chart freshness evidence"):
            confirm_issue(
                edition_name=EDITION_NAME, issue_number=1, reports_root=reports_root,
                archive_root=archive_root, programs_root=programs_root, force=True, dry_run=dry_run,
            )
        assert not archive_root.exists() or not any(archive_root.rglob("*"))
        draft_path.write_bytes(original_draft)
    else:
        assert not any(s["query_id"] == "synthetic-chart" for s in invalid_draft["kusto_sections"])
        assert draft.manifest_path is not None
        original_manifest = draft.manifest_path.read_bytes()
        invalid_manifest = json.loads(original_manifest)
        invalid_manifest["metadata"]["chart_freshness_evidence"][0]["chart_cache_ttl_hours"] = 0
        _rebind_chart_test_artifacts(invalid_draft, invalid_manifest)
        draft_path.write_text(json.dumps(invalid_draft), encoding="utf-8")
        draft.manifest_path.write_text(json.dumps(invalid_manifest), encoding="utf-8")
        with pytest.raises(typer.BadParameter, match="blocking chart freshness evidence"):
            confirm_issue(
                edition_name=EDITION_NAME, issue_number=1, reports_root=reports_root,
                archive_root=archive_root, programs_root=programs_root, force=True, dry_run=dry_run,
            )
        assert not archive_root.exists() or not any(archive_root.rglob("*"))
        draft.manifest_path.write_bytes(original_manifest)
        draft_path.write_bytes(original_draft)

    if placement == "exec_summary" and not dry_run:
        original_revalidate = confirm_module._revalidate_evidence
        checkpoint = Mock()
        snapshot_write = Mock(side_effect=AssertionError("snapshot write reached after chart expiry"))

        with monkeypatch.context() as fresh_check:
            fresh_writer = Mock(side_effect=RuntimeError("fresh chart reached snapshot writer"))
            fresh_check.setattr(
                "src.commands.confirm_stages.archive_transaction.create_checkpoint_snapshot", checkpoint,
            )
            fresh_check.setattr(confirm_module, "write_confirmed", fresh_writer)
            with pytest.raises(RuntimeError, match="fresh chart reached snapshot writer"):
                confirm_issue(
                    edition_name=EDITION_NAME, issue_number=1, reports_root=reports_root,
                    archive_root=archive_root, programs_root=programs_root, force=True,
                )
            checkpoint.assert_called_once()
            fresh_writer.assert_called_once()
        checkpoint.reset_mock()

        class ExpiredDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return as_of + timedelta(hours=27)

        with monkeypatch.context() as write_check:
            def expire_after_evidence_recheck(**kwargs):
                original_revalidate(**kwargs)
                write_check.setattr(confirm_module, "datetime", ExpiredDatetime)

            write_check.setattr(confirm_module, "_revalidate_evidence", expire_after_evidence_recheck)
            write_check.setattr(
                "src.commands.confirm_stages.archive_transaction.create_checkpoint_snapshot", checkpoint,
            )
            write_check.setattr(confirm_module, "write_confirmed", snapshot_write)
            with pytest.raises(ConfigError, match="QG-22"):
                confirm_issue(
                    edition_name=EDITION_NAME, issue_number=1, reports_root=reports_root,
                    archive_root=archive_root, programs_root=programs_root, force=True,
                )
            checkpoint.assert_not_called()
            snapshot_write.assert_not_called()
        with monkeypatch.context() as cli_check:
            def expire_for_cli(**kwargs):
                original_revalidate(**kwargs)
                cli_check.setattr(confirm_module, "datetime", ExpiredDatetime)

            cli_check.setattr(confirm_module, "_revalidate_evidence", expire_for_cli)
            cli_check.setattr(confirm_module, "REPORTS_ROOT", reports_root)
            cli_check.setattr(confirm_module, "ARCHIVE_ROOT", archive_root)
            cli_check.setattr(confirm_module, "_CONFIRM_PROGRAMS_ROOT", programs_root)
            cli_check.setattr(
                "src.commands.confirm_stages.archive_transaction.create_checkpoint_snapshot", checkpoint,
            )
            cli_check.setattr(confirm_module, "write_confirmed", snapshot_write)
            cli_result = runner.invoke(app, ["confirm", "--edition", EDITION_NAME, "--issue", "1", "--force"])
            assert cli_result.exit_code == 3
            assert "QG-22" in cli_result.output
            checkpoint.assert_not_called()
            snapshot_write.assert_not_called()

    class LaterDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return as_of + timedelta(hours=27)

    monkeypatch.setattr(confirm_module, "datetime", LaterDatetime)
    result = confirm_issue(
        edition_name=EDITION_NAME, issue_number=1, reports_root=reports_root,
        archive_root=archive_root, programs_root=programs_root, force=True, dry_run=dry_run,
    )

    assert result.exit_code == 3
    assert result.archive_paths is None
    assert result.manifest.qg_results["QG-22"] is False
    assert not archive_root.exists() or not any(archive_root.rglob("*"))


def _rebind_chart_test_artifacts(draft: dict, manifest: dict) -> None:
    from src.core.manifest_writer import chart_gate_evidence_sha256

    draft["chart_gate_evidence_sha256"] = chart_gate_evidence_sha256(
        manifest["manifest_id"], manifest["qg_results"],
        manifest["metadata"]["chart_freshness_evidence"], draft["kusto_sections"],
        edition_type=manifest["metadata"].get("chart_binding_edition_type"),
        routed_chart_sections=draft.get("routed_chart_sections"),
    )


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


def _with_neutral_authors(items):
    return tuple(
        replace(
            item,
            assigned_to="Maintainer",
            revisions=[replace(revision, changed_by="Maintainer") for revision in item.revisions],
            comments=[replace(comment, created_by="Maintainer") for comment in item.comments],
        )
        for item in items
    )


def _sample_items(as_of):
    return _with_neutral_authors(_report_sample_items(as_of))


def _forecast_items(as_of):
    return _with_neutral_authors(_report_forecast_items(as_of))


def _confirmable_items(as_of):
    items = list(_sample_items(as_of))
    return tuple(
        replace(
            item,
            target_date=as_of.date() + __import__("datetime", fromlist=["timedelta"]).timedelta(days=10 + index),
            tags=[*item.tags, "EXAMPLETAG1"] if "EXAMPLETAG1" not in item.tags else item.tags,
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
                "IncidentUrl": "https://incident.example.invalid/items/12345",
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
    if query.id == "artifact-service-coverage":
        return [{"CoveragePct": 95.0}]
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
