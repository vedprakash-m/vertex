from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email import policy
from email.parser import BytesParser
import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner
import yaml

from cli import app
from src.commands import escalate as escalate_module
from src.core.analytics_store import get_program_autonomy_audit_path
from src.core.incident_journal_store import append_incident_entry
from src.core.exceptions import ConfigError
from src.core.sqlite_stores import SQLiteSignalStore, SQLiteTrajectoryStore
from src.core.trajectory import backfill_trajectory_points
from src.core.models_v2 import DecisionAsk, IncidentEntry, Signal, TrajectoryPoint, VitalityScore
from src.core.claim_tracker import append_decision_ask, load_open_decision_asks
from src.core.journal import read_review_log, read_signals
from src.core.models import Confidence, RiskLevel, WorkItem
from tests.support.report_test_setup import stage_v2_report_workspace


runner = CliRunner()
AS_OF = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)
EDITION_NAME = "acme_weekly"


def test_escalate_cli_dry_run_prefers_raci_accountable(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(programs_root)
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")
    _seed_high_override(programs_root, issue_number=78, dimension_name="Deployment Velocity")
    _seed_chronic_high_history(programs_root, edition_name=EDITION_NAME, dimension_name="Deployment Velocity")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_chronic_high_items(timestamp), 0),
    )

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME, "--dry-run"])

    assert result.exit_code == 0
    assert "ESCALATE PREVIEW" in result.stdout
    assert "To: priya@example.com" in result.stdout
    assert "Dimension: Deployment Velocity" in result.stdout
    assert "Dry run: no escalation drafts written." in result.stdout
    assert not (programs_root / "acme" / "publications" / EDITION_NAME / "escalations").exists()
    assert not read_signals("acme", programs_root=programs_root)


def test_escalate_cli_dry_run_supports_json_and_csv(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(programs_root)
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")
    _seed_high_override(programs_root, issue_number=78, dimension_name="Deployment Velocity")
    _seed_chronic_high_history(programs_root, edition_name=EDITION_NAME, dimension_name="Deployment Velocity")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_chronic_high_items(timestamp), 0),
    )

    json_result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME, "--dry-run", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["edition_name"] == EDITION_NAME
    assert payload["channel"] == "eml"
    assert payload["dry_run"] is True
    assert payload["preview_count"] == 1
    assert payload["previews"][0]["rule_name"] == "consecutive_high"
    assert payload["previews"][0]["preview_type"] == "dimension"
    assert payload["previews"][0]["dimension_name"] == "Deployment Velocity"
    assert payload["previews"][0]["recipients"] == "priya@example.com"

    csv_result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME, "--dry-run", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == (
        "edition_name,channel,dry_run,rule_name,preview_type,dimension_name,recipients,"
        "workstream_ids,workstream_names,consecutive_high,vitality_composite,stale_days,"
        "milestone_id,milestone_status,milestone_days_to_target,decision_ask_id,"
        "decision_ask_status,decision_ask_age_days,incident_refs,incident_summary,escalation_path_label,subject"
    )
    assert any(",consecutive_high,dimension,Deployment Velocity,priya@example.com," in line for line in lines[1:])


def test_escalate_cli_dry_run_uses_cross_org_dependency_escalation_path(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(programs_root)
    _write_dependency_registry(
        programs_root,
        dependencies=(
            {
                "id": "acme-deployment-to-fabrikam-buildouts",
                "from_workstream_id": "acme",
                "to_workstream_id": "fabrikam:buildouts",
                "risk_if_broken": "Fabrikam buildouts can block the Acme deployment review.",
                "resolution_path": "cross_org_compute_pf",
            },
        ),
    )
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")
    _seed_high_override(programs_root, issue_number=78, dimension_name="Deployment Velocity")
    _seed_chronic_high_history(programs_root, edition_name=EDITION_NAME, dimension_name="Deployment Velocity")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_chronic_high_items(timestamp), 0),
    )

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME, "--dry-run", "--format", "json"])

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["preview_count"] == 1
    assert payload["previews"][0]["preview_type"] == "dimension"
    assert payload["previews"][0]["escalation_path_label"] == "Cross-org dependency escalation"
    assert "VP-level CC coverage" in payload["previews"][0]["escalation_guidance"]

    human_result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME, "--dry-run"])

    assert human_result.exit_code == 0
    assert "Escalation Path: Cross-org dependency escalation" in human_result.stdout


def test_build_dimension_escalation_dependency_context_ignores_unrelated_action_loader_failures(
    monkeypatch, repo_root: Path, tmp_path: Path
) -> None:
    _, programs_root, _ = _stage_escalate_workspace(repo_root, tmp_path)
    _write_dependency_registry(
        programs_root,
        dependencies=(
            {
                "id": "acme-deployment-to-fabrikam-buildouts",
                "from_workstream_id": "acme",
                "to_workstream_id": "fabrikam:buildouts",
                "risk_if_broken": "Fabrikam buildouts can block the Acme deployment review.",
                "resolution_path": "cross_org_compute_pf",
            },
        ),
    )

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise ConfigError("actions broken")

    monkeypatch.setattr("src.core.action_tracker.load_actions", _boom)

    context = escalate_module._build_dimension_escalation_dependency_context(
        program_id="acme",
        linked_workstream_ids=("acme",),
        programs_root=programs_root,
    )

    assert context is not None
    assert context.escalation_path_label == "Cross-org dependency escalation"
    assert "VP-level CC coverage" in context.guidance


def test_escalate_cli_writes_cross_org_dependency_escalation_context(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(programs_root)
    _write_dependency_registry(
        programs_root,
        dependencies=(
            {
                "id": "acme-deployment-to-fabrikam-buildouts",
                "from_workstream_id": "acme",
                "to_workstream_id": "fabrikam:buildouts",
                "risk_if_broken": "Fabrikam buildouts can block the Acme deployment review.",
                "resolution_path": "cross_org_compute_pf",
            },
        ),
    )
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")
    _seed_high_override(programs_root, issue_number=78, dimension_name="Deployment Velocity")
    _seed_chronic_high_history(programs_root, edition_name=EDITION_NAME, dimension_name="Deployment Velocity")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_chronic_high_items(timestamp), 0),
    )

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME], input="y\n")

    signals = read_signals("acme", programs_root=programs_root)
    eml_paths = sorted((programs_root / "acme" / "publications" / EDITION_NAME / "escalations").glob("*.eml"))
    html_body = _load_html_body(eml_paths[0])

    assert result.exit_code == 0
    assert len(signals) == 1
    assert len(eml_paths) == 1
    assert "Escalation Path" in html_body
    assert "Cross-org dependency escalation" in html_body
    assert "VP-level CC coverage" in html_body
    assert signals[0].metadata["escalation_path_label"] == "Cross-org dependency escalation"
    assert "VP-level CC coverage" in signals[0].metadata["escalation_guidance"]


def test_escalate_cli_writes_eml_and_signal(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(programs_root)
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")
    _seed_high_override(programs_root, issue_number=78, dimension_name="Deployment Velocity")
    _seed_chronic_high_history(programs_root, edition_name=EDITION_NAME, dimension_name="Deployment Velocity")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_chronic_high_items(timestamp), 0),
    )

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME], input="y\n")

    eml_paths = sorted((programs_root / "acme" / "publications" / EDITION_NAME / "escalations").glob("*.eml"))
    signals = read_signals("acme", programs_root=programs_root)
    html_body = _load_html_body(eml_paths[0])

    assert result.exit_code == 0
    assert len(eml_paths) == 1
    assert "Wrote 1 escalation draft EML(s). Send manually via Outlook." in result.stdout
    assert len(signals) == 1
    assert "Scorecard Trend" in html_body
    assert "Evidence Packet" in html_body
    assert "Recommended Action" in html_body
    assert signals[0].source == "vertex/escalation"
    assert signals[0].workstream_id == "acme"
    assert "Deployment Velocity" in signals[0].text
    assert (programs_root / "acme" / "escalation_state.json").exists()
    assert "priya@example.com" in eml_paths[0].read_text(encoding="utf-8")


def test_escalate_cli_falls_back_to_leadership_reader(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(programs_root)
    _clear_escalation_recipients(programs_root)
    _clear_workstream_accountable(programs_root, workstream_id="acme")
    _seed_people_entry(tmp_path / "knowledge", alias="jordan", email="jordan@example.com", display_name="Jordan Lee")
    _seed_high_override(programs_root, issue_number=78, dimension_name="Deployment Velocity")
    _seed_chronic_high_history(programs_root, edition_name=EDITION_NAME, dimension_name="Deployment Velocity")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_chronic_high_items(timestamp), 0),
    )

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME, "--dry-run"])

    assert result.exit_code == 0
    assert "To: jordan@example.com" in result.stdout


def test_escalate_cli_dry_run_supports_vitality_rule_fields(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(
        programs_root,
        conditions=[
            {"field": "consecutive_high", "op": ">=", "value": 4},
            {"field": "vitality_composite", "op": "<=", "value": 30},
            {"field": "stale_days", "op": ">=", "value": 21},
        ],
    )
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")
    _seed_high_override(programs_root, issue_number=78, dimension_name="Deployment Velocity")
    _seed_chronic_high_history(programs_root, edition_name=EDITION_NAME, dimension_name="Deployment Velocity")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_stale_high_items(timestamp), 0),
    )

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME, "--dry-run"])

    assert result.exit_code == 0
    assert "To: priya@example.com" in result.stdout
    assert "Vitality Composite:" in result.stdout
    assert "Stale Days: 28" in result.stdout
    assert "Dry run: no escalation drafts written." in result.stdout


def test_escalate_cli_dry_run_tolerates_malformed_scorecard_history(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(programs_root)
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")
    _seed_high_override(programs_root, issue_number=78, dimension_name="Deployment Velocity")

    edition_root = programs_root / "acme" / "archive" / EDITION_NAME
    edition_root.mkdir(parents=True, exist_ok=True)
    (edition_root / "scorecards.json").write_text("{malformed", encoding="utf-8")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_chronic_high_items(timestamp), 0),
    )

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME, "--dry-run"])

    assert result.exit_code == 0
    assert "ESCALATE PREVIEW" in result.stdout
    assert "No escalation rules triggered." in result.stdout
    assert "Dry run: no escalation drafts written." in result.stdout


def test_build_workstream_vitality_context_reads_sqlite_backed_signals_and_trajectories(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    editions_root = reports_root.parent / "editions"
    _set_program_storage_backend(programs_root, storage_backend="sqlite")

    trajectory_store = SQLiteTrajectoryStore(programs_root=programs_root)
    trajectory_store.append(
        "acme",
        1201,
        TrajectoryPoint(
            date=(AS_OF - timedelta(days=1)).date(),
            state="Active",
            assigned_to="owner@example.com",
            target_date=AS_OF.date(),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Deployment",
        ),
    )

    resolved = escalate_module.resolve_edition(
        EDITION_NAME,
        editions_root=editions_root,
        programs_root=programs_root,
    )
    assert resolved is not None

    captured = {}

    monkeypatch.setattr(
        escalate_module,
        "load_approved_workiq_signals",
        lambda program_id, *, as_of, programs_root: (
            Signal(
                id="workiq-1",
                timestamp=AS_OF - timedelta(days=2),
                source="workiq/meeting",
                program_id=program_id,
                workstream_id="acme",
                entity_refs=("WI:1201",),
                text="Need to resolve blocker with owner by 2026-05-12.",
                raw_ref="workiq:workiq-1",
                confidence=Confidence.HIGH,
                metadata={"entity_link_confidence": "high"},
                thread_id="thread-1",
            ),
        ),
    )

    def _capture_score_vitality(items, *, as_of, workstream_resolver, leakage, leakage_signal_threshold):
        captured["leakage"] = leakage
        captured["threshold"] = leakage_signal_threshold
        return (
            VitalityScore(
                work_item_id=1201,
                owner_alias="owner",
                workstream_id="acme",
                freshness_days=28,
                freshness_grade="red",
                richness_score=75,
                richness_missing=("recent_comment",),
                leakage_events=leakage.leakage_counts_by_item.get(1201, 0),
                workiq_signal_count=leakage.signal_counts_by_item.get(1201, 0),
                composite_score=61,
                suggested_update="Add an owner comment",
            ),
        )

    monkeypatch.setattr(escalate_module, "score_vitality", _capture_score_vitality)
    monkeypatch.setattr(
        escalate_module,
        "aggregate_vitality",
        lambda scores, *, scope_type, leakage_signal_threshold: (
            SimpleNamespace(scope_id="acme", composite_score=61),
        ),
    )

    composite_by_workstream, stale_days_by_workstream = escalate_module._build_workstream_vitality_context(
        program_id="acme",
        items=(
            WorkItem(
                id=1201,
                type="Feature",
                title="Escalation-worthy rollout follow-up",
                state="Active",
                assigned_to="owner@example.com",
                assigned_to_email="owner@example.com",
                area_path="One\\Adventure\\Acme\\Deployment",
                iteration_path="Sprint 1",
                target_date=AS_OF.date(),
                risk_level=RiskLevel.HIGH,
                tags=["acme"],
                custom_fields={
                    "changed_date": (AS_OF - timedelta(days=28)).isoformat(),
                    "description": "Current blocker needs owner review and resolve action by 2026-05-12 to unblock deployment readiness.",
                },
                revisions=[],
                comments=[],
                fetched_at=AS_OF,
            ),
        ),
        workstreams=resolved.workstreams,
        raw_program={
            **resolved.raw_program,
            "vitality": {
                "sparse_workiq_threshold": 1,
                "surfaces": {},
            },
        },
        knowledge=SimpleNamespace(people_directory=()),
        as_of=AS_OF,
        programs_root=programs_root,
    )

    assert composite_by_workstream == {"acme": 61}
    assert stale_days_by_workstream == {"acme": 28}
    assert captured["threshold"] == 1
    assert captured["leakage"].signal_counts_by_item == {1201: 1}
    assert captured["leakage"].leakage_counts_by_item == {}


def test_escalate_cli_dry_run_supports_milestone_rule_fields(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(
        programs_root,
        rule_name="milestone_at_risk",
        conditions=[
            {"field": "milestone_status", "op": "==", "value": "at_risk"},
            {"field": "milestone_days_to_target", "op": "<=", "value": 14},
        ],
    )
    _write_milestones(programs_root)
    _seed_milestone_archive(programs_root)
    _seed_milestone_trajectory(programs_root)
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_stale_high_items(timestamp), 0),
    )
    _orig_gen = escalate_module.generate_escalations
    monkeypatch.setattr(escalate_module, "generate_escalations", lambda **kw: _orig_gen(as_of=AS_OF, **kw))

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME, "--dry-run"])

    assert result.exit_code == 0
    assert "To: priya@example.com" in result.stdout
    assert "Milestone: M3 Code Complete" in result.stdout
    assert "Milestone Status: at_risk" in result.stdout
    assert "Days to Target:" in result.stdout
    assert "Milestone Schedule: Tracking 2026-05-20 (3 days late vs target)" in result.stdout
    assert "Target History: 2026-05-12 -> 2026-05-17" in result.stdout


def test_escalate_cli_dry_run_supports_sqlite_backed_milestone_rule_fields(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(
        programs_root,
        rule_name="milestone_at_risk",
        conditions=[
            {"field": "milestone_status", "op": "==", "value": "at_risk"},
            {"field": "milestone_days_to_target", "op": "<=", "value": 14},
        ],
    )
    _write_milestones(programs_root)
    _seed_milestone_archive(programs_root)
    _set_program_storage_backend(programs_root, storage_backend="sqlite")
    SQLiteTrajectoryStore(programs_root=programs_root).append(
        "acme",
        1201,
        TrajectoryPoint(
            date=(AS_OF - timedelta(days=4)).date(),
            state="Active",
            assigned_to="Priya Mehta",
            target_date=(AS_OF + timedelta(days=10)).date(),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Deployment",
        ),
    )
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_stale_high_items(timestamp), 0),
    )
    _orig_gen = escalate_module.generate_escalations
    monkeypatch.setattr(escalate_module, "generate_escalations", lambda **kw: _orig_gen(as_of=AS_OF, **kw))

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME, "--dry-run"])

    assert result.exit_code == 0
    assert "To: priya@example.com" in result.stdout
    assert "Milestone: M3 Code Complete" in result.stdout
    assert "Milestone Status: at_risk" in result.stdout
    assert "Days to Target:" in result.stdout
    assert "Milestone Schedule: Tracking 2026-05-20 (3 days late vs target)" in result.stdout
    assert "Target History: 2026-05-12 -> 2026-05-17" in result.stdout


def test_escalate_cli_writes_signal_for_milestone_rule_fields(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(
        programs_root,
        rule_name="milestone_at_risk",
        conditions=[
            {"field": "milestone_status", "op": "==", "value": "at_risk"},
            {"field": "milestone_days_to_target", "op": "<=", "value": 14},
        ],
    )
    _write_milestones(programs_root)
    _seed_milestone_archive(programs_root)
    _seed_milestone_trajectory(programs_root)
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_stale_high_items(timestamp), 0),
    )
    _orig_gen = escalate_module.generate_escalations
    monkeypatch.setattr(escalate_module, "generate_escalations", lambda **kw: _orig_gen(as_of=AS_OF, **kw))

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME], input="y\n")

    signals = read_signals("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert len(signals) == 1
    assert signals[0].metadata["milestone_status"] == "at_risk"
    assert 0 <= int(signals[0].metadata["milestone_days_to_target"]) <= 14
    assert signals[0].metadata["milestone_schedule_summary"] == "Tracking 2026-05-20 (3 days late vs target)"
    assert signals[0].metadata["milestone_target_date_history_summary"] == "Target history 2026-05-12 -> 2026-05-17"
    assert "milestone:m3_code_complete" in signals[0].entity_refs


def test_escalate_cli_writes_sqlite_backed_signal_for_milestone_rule_fields(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(
        programs_root,
        rule_name="milestone_at_risk",
        conditions=[
            {"field": "milestone_status", "op": "==", "value": "at_risk"},
            {"field": "milestone_days_to_target", "op": "<=", "value": 14},
        ],
    )
    _write_milestones(programs_root)
    _seed_milestone_archive(programs_root)
    _set_program_storage_backend(programs_root, storage_backend="sqlite")
    SQLiteTrajectoryStore(programs_root=programs_root).append(
        "acme",
        1201,
        TrajectoryPoint(
            date=(AS_OF - timedelta(days=4)).date(),
            state="Active",
            assigned_to="Priya Mehta",
            target_date=(AS_OF + timedelta(days=10)).date(),
            risk_level=RiskLevel.HIGH,
            area_path="One\\Adventure\\Acme\\Deployment",
        ),
    )
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_stale_high_items(timestamp), 0),
    )
    _orig_gen = escalate_module.generate_escalations
    monkeypatch.setattr(escalate_module, "generate_escalations", lambda **kw: _orig_gen(as_of=AS_OF, **kw))

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME], input="y\n")

    signals = SQLiteSignalStore(programs_root=programs_root).read("acme")
    review_log = read_review_log("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert len(signals) == 1
    assert not read_signals("acme", programs_root=programs_root)
    assert not review_log
    assert signals[0].source == "vertex/escalation"
    assert signals[0].metadata["milestone_status"] == "at_risk"
    assert 0 <= int(signals[0].metadata["milestone_days_to_target"]) <= 14
    assert signals[0].metadata["milestone_schedule_summary"] == "Tracking 2026-05-20 (3 days late vs target)"
    assert signals[0].metadata["milestone_target_date_history_summary"] == "Target history 2026-05-12 -> 2026-05-17"
    assert SQLiteSignalStore(programs_root=programs_root).read_reviews("acme")[signals[0].id].decision == "approved"


def test_escalate_cli_dry_run_supports_decision_ask_rule_fields(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(
        programs_root,
        rule_name="unresolved_ask",
        conditions=[
            {"field": "decision_ask_age_days", "op": ">=", "value": 21},
            {"field": "decision_ask_status", "op": "==", "value": "open"},
        ],
    )
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")
    _append_open_decision_ask(programs_root)

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_stale_high_items(timestamp), 0),
    )

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME, "--dry-run"])

    assert result.exit_code == 0
    assert "To: priya@example.com" in result.stdout
    assert "Decision Ask: ask-1" in result.stdout
    assert "Decision Ask Status: open" in result.stdout
    assert "Decision Ask Age:" in result.stdout


def test_escalate_cli_can_scope_to_single_decision_ask(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(
        programs_root,
        rule_name="unresolved_ask",
        conditions=[
            {"field": "decision_ask_age_days", "op": ">=", "value": 21},
            {"field": "decision_ask_status", "op": "==", "value": "open"},
        ],
    )
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")
    _append_open_decision_ask(programs_root)
    _append_related_incident_entry(programs_root)

    _orig_gen = escalate_module.generate_escalations
    monkeypatch.setattr(escalate_module, "generate_escalations", lambda **kw: _orig_gen(as_of=AS_OF, **kw))
    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_stale_high_items(timestamp), 0),
    )

    result = runner.invoke(
        app,
        ["escalate", "--edition", EDITION_NAME, "--decision-ask", "ask-1", "--dry-run", "--format", "json"],
    )

    payload = json.loads(result.stdout)
    assert result.exit_code == 0
    assert payload["preview_count"] == 1
    assert payload["previews"][0]["preview_type"] == "decision_ask"
    assert payload["previews"][0]["decision_ask_id"] == "ask-1"
    assert payload["previews"][0]["incident_refs"] == "IcM 4321"
    assert payload["previews"][0]["incident_summary"].startswith("WI:1201: rollout sequencing slipped after dependency handoff. Source: IcM 4321.")


def test_escalate_cli_writes_signal_for_decision_ask_rule_fields(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(
        programs_root,
        rule_name="unresolved_ask",
        conditions=[
            {"field": "decision_ask_age_days", "op": ">=", "value": 21},
            {"field": "decision_ask_status", "op": "==", "value": "open"},
        ],
    )
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")
    _append_open_decision_ask(programs_root)
    _append_related_incident_entry(programs_root)

    _orig_gen = escalate_module.generate_escalations
    monkeypatch.setattr(escalate_module, "generate_escalations", lambda **kw: _orig_gen(as_of=AS_OF, **kw))
    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_stale_high_items(timestamp), 0),
    )

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME], input="y\n")

    signals = read_signals("acme", programs_root=programs_root)
    eml_paths = tuple((programs_root / "acme" / "publications" / EDITION_NAME / "escalations").glob("*.eml"))
    html_body = _load_html_body(eml_paths[0])

    assert result.exit_code == 0
    assert len(signals) == 1
    assert len(eml_paths) == 1
    assert "Vertex Escalation Draft" in html_body
    assert "Escalation Facts" in html_body
    assert "Ask Text" in html_body
    assert "Incident-linked" in html_body
    assert signals[0].metadata["decision_ask_status"] == "open"
    assert int(signals[0].metadata["decision_ask_age_days"]) >= 21
    assert signals[0].metadata["incident_refs"] == ["IcM 4321"]
    assert signals[0].metadata["incident_summary"].startswith("WI:1201: rollout sequencing slipped after dependency handoff. Source: IcM 4321.")
    assert "decision_ask:ask-1" in signals[0].entity_refs
    assert "WI:1201" in signals[0].entity_refs
    assert "IcM 4321" in signals[0].entity_refs
    assert load_open_decision_asks("acme", programs_root=programs_root)[0].last_touched_at is not None
    audit_payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("acme", programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert audit_payloads[-1]["action_type"] == "decision_ask_escalation"
    assert audit_payloads[-1]["accepted"] is True
    assert audit_payloads[-1]["policy_rule"] == "unresolved_ask"
    assert audit_payloads[-1]["evidence_refs"] == ["WI:1201", "IcM 4321", "decision_ask:ask-1", "workstream:acme"]
    assert audit_payloads[-1]["subject_alias"] == "priya"


def test_escalate_cli_rejects_live_email_below_l2(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(programs_root)
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")
    _seed_high_override(programs_root, issue_number=78, dimension_name="Deployment Velocity")
    _seed_chronic_high_history(programs_root, edition_name=EDITION_NAME, dimension_name="Deployment Velocity")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_chronic_high_items(timestamp), 0),
    )

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME, "--channel", "email"])

    assert result.exit_code == 2
    assert "requires maturity_level >= 2" in result.stdout
    assert not (programs_root / "acme" / "publications" / EDITION_NAME / "escalations").exists()
    assert not read_signals("acme", programs_root=programs_root)




def test_escalate_cli_records_declined_review_without_writing_outputs(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(programs_root)
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")
    _seed_high_override(programs_root, issue_number=78, dimension_name="Deployment Velocity")
    _seed_chronic_high_history(programs_root, edition_name=EDITION_NAME, dimension_name="Deployment Velocity")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_chronic_high_items(timestamp), 0),
    )

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME], input="n\n")

    audit_payloads = [
        json.loads(line)
        for line in get_program_autonomy_audit_path("acme", programs_root=programs_root).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert result.exit_code == 1
    assert not (programs_root / "acme" / "publications" / EDITION_NAME / "escalations").exists()
    assert not read_signals("acme", programs_root=programs_root)
    assert not (programs_root / "acme" / "escalation_state.json").exists()
    assert audit_payloads[-1]["action_type"] == "dimension_escalation"
    assert audit_payloads[-1]["accepted"] is False
    assert audit_payloads[-1]["rollback_mechanism"] == "No rollback needed; escalation draft was not written."
    assert audit_payloads[-1]["policy_rule"] == "consecutive_high"
    assert audit_payloads[-1]["evidence_refs"] == ["dimension:deployment_velocity", "workstream:acme"]


def test_escalate_cli_sends_live_email_at_l2(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _edition_yaml = tmp_path / "programs" / "acme" / "editions" / f"{EDITION_NAME}.yaml"
    _ed_data = yaml.safe_load(_edition_yaml.read_text())
    _ed_data["author"]["email"] = "maintainer@example.com"
    _edition_yaml.write_text(yaml.dump(_ed_data))
    _write_escalation_rules(programs_root)
    _set_program_maturity_level(programs_root, maturity_level=2)
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")
    _seed_high_override(programs_root, issue_number=78, dimension_name="Deployment Velocity")
    _seed_chronic_high_history(programs_root, edition_name=EDITION_NAME, dimension_name="Deployment Velocity")

    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_chronic_high_items(timestamp), 0),
    )

    sent_subjects: list[str] = []

    def _build_fake_sender(*, author_email: str):
        assert author_email == "maintainer@example.com"

        def _sender(preview):
            sent_subjects.append(preview.subject)
            return f"graph://mail/{preview.cooldown_key}"

        return _sender

    monkeypatch.setattr("src.commands.escalate._build_escalation_email_sender", _build_fake_sender)

    result = runner.invoke(app, ["escalate", "--edition", EDITION_NAME, "--channel", "email"], input="y\n")

    signals = read_signals("acme", programs_root=programs_root)

    assert result.exit_code == 0
    assert len(sent_subjects) == 1
    assert sent_subjects[0].startswith("[Vertex] Escalation:")
    assert "Deployment Velocity" in sent_subjects[0]
    assert "Sent 1 escalation email(s) via Graph." in result.stdout
    assert len(signals) == 1
    assert signals[0].raw_ref.startswith("graph://mail/")
    assert not (programs_root / "acme" / "publications" / EDITION_NAME / "escalations").exists()


def _load_html_body(path: Path) -> str:
    message = BytesParser(policy=policy.default).parsebytes(path.read_bytes())
    return next(
        part.get_content()
        for part in message.walk()
        if part.get_content_type() == "text/html"
    )


def _stage_escalate_workspace(repo_root: Path, tmp_path: Path) -> tuple[Path, Path, Path]:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    return reports_root, tmp_path / "programs", tmp_path / "publications"


def _write_escalation_rules(
    programs_root: Path,
    *,
    rule_name: str = "consecutive_high",
    conditions: list[dict[str, object]] | None = None,
) -> None:
    (programs_root / "acme" / "escalation_rules.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "rules": [
                    {
                        "name": rule_name,
                        "conditions": conditions or [
                            {"field": "consecutive_high", "op": ">=", "value": 4},
                        ],
                        "cooldown_hours": 24,
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_dependency_registry(programs_root: Path, *, dependencies: tuple[dict[str, str], ...]) -> None:
    (programs_root / "acme" / "dependencies.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "dependencies": [
                    {
                        "id": dependency["id"],
                        "from_workstream_id": dependency["from_workstream_id"],
                        "to_workstream_id": dependency["to_workstream_id"],
                        "dependency_type": "blocks",
                        "risk_if_broken": dependency["risk_if_broken"],
                        "status": "active",
                        "resolution_path": dependency["resolution_path"],
                    }
                    for dependency in dependencies
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_milestones(programs_root: Path) -> None:
    (programs_root / "acme" / "milestones.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "milestones": [
                    {
                        "id": "m3_code_complete",
                        "name": "M3 Code Complete",
                        "target_date": (AS_OF + timedelta(days=7)).date().isoformat(),
                        "owner_alias": "priya",
                        "status": "on_track",
                        "exit_criteria": ["Deployment item closed"],
                        "linked_workstream_ids": ["acme"],
                        "linked_work_item_ids": [1201],
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _seed_milestone_archive(programs_root: Path) -> None:
    archive_dir = programs_root / "acme" / "archive" / EDITION_NAME
    archive_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = archive_dir / "issue_001.manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "manifest_id": "manifest-001",
                "issue_number": 1,
                "edition": EDITION_NAME,
                "started_at": "2026-05-03T08:00:00+00:00",
                "ended_at": "2026-05-03T09:00:00+00:00",
                "config_hash": "config",
                "snapshot_hash": "snapshot",
                "html_hash": "html",
                "md_hash": "md",
                "ado_calls": 0,
                "ai_calls": 0,
                "ai_cost_usd": 0.0,
                "freshness_summary": {},
                "qg_results": {},
                "git_sha": None,
                "metadata": {
                    "milestone_assessments": [
                        {
                            "milestone_id": "m3_code_complete",
                            "target_date": "2026-05-12",
                        }
                    ]
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    (archive_dir / "index.json").write_text(
        json.dumps(
            {
                "edition": EDITION_NAME,
                "issues": [
                    {
                        "issue_number": 1,
                        "generated_at": "2026-05-03T09:00:00+00:00",
                        "kind": "confirmed",
                        "manifest_path": str(manifest_path),
                    }
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


def _seed_milestone_trajectory(programs_root: Path) -> None:
    backfill_trajectory_points(
        "acme",
        1201,
        (
            TrajectoryPoint(
                date=(AS_OF - timedelta(days=4)).date(),
                state="Active",
                assigned_to="Priya Mehta",
                target_date=(AS_OF + timedelta(days=10)).date(),
                risk_level=RiskLevel.HIGH,
                area_path="One\\Adventure\\Acme\\Deployment",
            ),
        ),
        programs_root=programs_root,
    )


def _append_open_decision_ask(programs_root: Path) -> None:
    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id=EDITION_NAME,
            issue_number=77,
            text="Need LT decision on WI:1201 rollout sequencing",
            entity_refs=("WI:1201",),
            ask_date=(AS_OF - timedelta(days=30)).date(),
            owner_alias="priya",
        ),
        programs_root=programs_root,
    )


def _append_related_incident_entry(programs_root: Path) -> None:
    append_incident_entry(
        IncidentEntry(
            program_id="acme",
            incident_id="4321",
            signal_id="incident-signal-1",
            observed_at=AS_OF - timedelta(days=3),
            recorded_at=AS_OF - timedelta(days=2),
            belief_change_summary="IcM 4321: rollout sequencing slipped after dependency handoff.",
            workstream_id="acme",
            owning_team="Acme",
            severity=2,
            source_path="icm://4321",
            query_id="query-1",
            linked_work_item_ids=(1201,),
            ado_entity_refs=("WI:1201",),
            raw_ref="ICM:4321",
            confidence=Confidence.HIGH,
        ),
        programs_root=programs_root,
    )


def test_escalate_preview_keeps_repeated_incident_learning_summary_with_shared_synthesizer(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, programs_root, output_root = _stage_escalate_workspace(repo_root, tmp_path)
    _write_escalation_rules(
        programs_root,
        rule_name="unresolved_ask",
        conditions=[
            {"field": "decision_ask_age_days", "op": ">=", "value": 21},
            {"field": "decision_ask_status", "op": "==", "value": "open"},
        ],
    )
    _set_workstream_accountable(programs_root, workstream_id="acme", accountable="priya")
    _seed_people_entry(tmp_path / "knowledge", alias="priya", email="priya@example.com", display_name="Priya Mehta")
    _append_open_decision_ask(programs_root)
    _append_related_incident_entry(programs_root)
    append_incident_entry(
        IncidentEntry(
            program_id="acme",
            incident_id="4322",
            signal_id="incident-signal-2",
            observed_at=AS_OF - timedelta(days=2),
            recorded_at=AS_OF - timedelta(days=1),
            belief_change_summary="IcM 4322: WI:1201 rollout sequencing slipped after dependency handoff again.",
            workstream_id="acme",
            owning_team="Acme",
            severity=2,
            source_path="icm://4322",
            query_id="query-2",
            linked_work_item_ids=(1201,),
            ado_entity_refs=("WI:1201",),
            raw_ref="ICM:4322",
            confidence=Confidence.HIGH,
        ),
        programs_root=programs_root,
    )

    _orig_gen = escalate_module.generate_escalations
    monkeypatch.setattr(escalate_module, "generate_escalations", lambda **kw: _orig_gen(as_of=AS_OF, **kw))
    monkeypatch.setattr("src.commands.escalate.REPORTS_ROOT", reports_root)
    monkeypatch.setattr(
        "src.commands.escalate.report_helpers._load_live_work_items",
        lambda bundle, timestamp: (_sample_stale_high_items(timestamp), 0),
    )

    result = runner.invoke(
        app,
        ["escalate", "--edition", EDITION_NAME, "--decision-ask", "ask-1", "--dry-run"],
    )

    assert result.exit_code == 0
    assert "repeated across 2 incident learnings" in result.stdout
    assert "high confidence" in result.stdout


def _set_program_maturity_level(programs_root: Path, *, maturity_level: int) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    document["maturity_level"] = maturity_level
    program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _set_program_storage_backend(programs_root: Path, *, storage_backend: str) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    document["storage_backend"] = storage_backend
    program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _clear_escalation_recipients(programs_root: Path) -> None:
    program_path = programs_root / "acme" / "program.yaml"
    document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    document.pop("escalation_recipients", None)
    program_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _set_workstream_accountable(programs_root: Path, *, workstream_id: str, accountable: str) -> None:
    workstreams_path = programs_root / "acme" / "workstreams.yaml"
    document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    workstreams = document.get("workstreams")
    assert isinstance(workstreams, list)
    for entry in workstreams:
        if not isinstance(entry, dict) or entry.get("id") != workstream_id:
            continue
        entry["raci"] = {
            "accountable": accountable,
            "responsible": [],
            "consulted": [],
            "informed": [],
        }
        break
    workstreams_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _clear_workstream_accountable(programs_root: Path, *, workstream_id: str) -> None:
    workstreams_path = programs_root / "acme" / "workstreams.yaml"
    document = yaml.safe_load(workstreams_path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    workstreams = document.get("workstreams")
    assert isinstance(workstreams, list)
    for entry in workstreams:
        if not isinstance(entry, dict) or entry.get("id") != workstream_id:
            continue
        raci = entry.get("raci")
        if isinstance(raci, dict):
            raci.pop("accountable", None)
        break
    workstreams_path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _seed_people_entry(knowledge_root: Path, *, alias: str, email: str, display_name: str) -> None:
    path = knowledge_root / "people_directory.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    people = document.setdefault("people", [])
    assert isinstance(people, list)
    people.append(
        {
            "alias": alias,
            "email": email,
            "display_name": display_name,
        }
    )
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")


def _seed_high_override(programs_root: Path, *, issue_number: int, dimension_name: str) -> None:
    overrides_dir = programs_root / "acme" / "overrides"
    overrides_dir.mkdir(parents=True, exist_ok=True)
    (overrides_dir / f"issue_{issue_number:03d}.yaml").write_text(
        yaml.safe_dump(
            {
                "issue_number": issue_number,
                "scorecards": {
                    "Acme Adventure/XIO 100% Ramp Readiness": {
                        dimension_name: {
                            "risk": "high",
                        }
                    }
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _seed_chronic_high_history(programs_root: Path, *, edition_name: str, dimension_name: str) -> None:
    edition_root = programs_root / "acme" / "archive" / edition_name
    edition_root.mkdir(parents=True, exist_ok=True)
    (edition_root / "scorecards.json").write_text(
        json.dumps(
            {
                "entries": [
                    {"issue_number": 75, "dimension": dimension_name, "risk": "high"},
                    {"issue_number": 76, "dimension": dimension_name, "risk": "high"},
                    {"issue_number": 77, "dimension": dimension_name, "risk": "high"},
                ]
            }
        ),
        encoding="utf-8",
    )


def _sample_chronic_high_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=1201,
            type="Feature",
            title="Chronic high rollout risk",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="Sprint 1",
            target_date=AS_OF.date(),
            risk_level=RiskLevel.HIGH,
            tags=[],
            custom_fields={"changed_date": as_of.isoformat()},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )


def _sample_stale_high_items(as_of: datetime) -> tuple[WorkItem, ...]:
    return (
        WorkItem(
            id=1201,
            type="Feature",
            title="Escalation-worthy rollout follow-up",
            state="Active",
            assigned_to="owner@example.com",
            assigned_to_email="owner@example.com",
            area_path="One\\Adventure\\Acme\\Deployment",
            iteration_path="Sprint 1",
            target_date=AS_OF.date(),
            risk_level=RiskLevel.HIGH,
            tags=[],
            custom_fields={"changed_date": (as_of - timedelta(days=28)).isoformat()},
            revisions=[],
            comments=[],
            fetched_at=as_of,
        ),
    )
