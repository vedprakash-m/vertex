from __future__ import annotations

import csv
import json
import shutil
from datetime import date
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

from typer.testing import CliRunner
import yaml

from src.commands.gather import gather_program
from cli import app
from src.commands.report import generate_report_draft
from src.commands.status import build_status_report
from src.core.gather_state_store import write_gather_state
from src.core.journal import append_review_decision, append_signal
from src.core.models import Confidence
from src.core.models_v2 import IntegrationError, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus
from src.core.models_v2 import Signal, SignalReviewDecision
from src.core.risk_register_engine import save_risk_register
from src.core.sqlite_stores import SQLiteSignalStore
from src.core.snapshot_store import get_archive_root
from tests.support.ado_cassettes import load_cassette_work_items
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace


runner = CliRunner()
EDITION_NAME = "acme_weekly"
FROZEN_NOW = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
FROZEN_MANIFEST_ID = UUID("11111111-1111-1111-1111-111111111111")


def test_build_status_report_uses_latest_draft_artifacts(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
    )

    assert report.display_name == "Program Hygiene"
    assert report.issue_number == 1
    assert report.readiness_percent == 81
    assert report.blocker_count == 5
    assert report.milestone_summary == "1 at risk, 4 on track"
    assert report.last_gathered_at is not None
    assert report.last_confirmed_at is None
    assert report.cadence == "weekly"
    assert report.cadence_status == "no confirmed issues yet"
    assert report.next_due_edition == "nova_daily"
    assert report.next_due_status == "no confirmed issues yet"
    assert report.next_due_context is not None
    assert "working team and DRIs; via teams; owner maintainer" in report.next_due_context
    assert report.source_manifest_path is not None


def test_build_status_report_prefers_gather_state_timestamp_over_draft_snapshot(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    gather_time = datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc)

    gather_program(
        "acme",
        as_of=gather_time,
        programs_root=tmp_path / "programs",
        loader=lambda program, workstreams, timestamp: load_cassette_work_items("cold_start", timestamp),
        freshness_loader=lambda program, workstreams, timestamp: load_cassette_work_items("cold_start", timestamp),
    )

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
    )

    assert report.last_gathered_at == gather_time


def test_build_status_report_surfaces_latest_gather_integration_summary(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    programs_root = tmp_path / "programs"

    write_gather_state(
        "acme",
        gathered_at=datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc),
        scanned_items=4,
        discovered_signals=2,
        new_signals=1,
        pending_review=1,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=3,
        archived_journal_files=0,
        background_proposals=0,
        integration_errors=1,
        integration_error_details=(
            IntegrationError(
                source="kusto",
                stage="gather",
                retryable=True,
                message="kusto unavailable",
                operator_action="Run 'vertex admin auth setup' and verify Kusto cluster access before retrying gather.",
            ),
        ),
        programs_root=programs_root,
    )

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
    )

    assert report.gather_integration_summary == (
        "1 optional integration failure(s); kusto/gather: kusto unavailable. "
        "Next: Run 'vertex admin auth setup' and verify Kusto cluster access before retrying gather."
    )
    assert report.to_payload()["gather_integration_summary"] == report.gather_integration_summary
    assert report.to_payload()["gather_integration_details"] == [
        {
            "source": "kusto",
            "stage": "gather",
            "retryable": True,
            "message": "kusto unavailable",
            "operator_action": "Run 'vertex admin auth setup' and verify Kusto cluster access before retrying gather.",
        }
    ]


def test_build_status_report_prefers_projected_blockers_over_manifest_failures_when_snapshot_exists(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    manifest_path = tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["qg_results"] = {key: False for key in payload["qg_results"]}
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
    )

    assert report.readiness_percent == 0
    assert report.blocker_count == 5


def test_build_status_report_falls_back_to_manifest_failures_without_snapshot(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    snapshot_path = tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.snapshot.json"
    snapshot_path.unlink()
    manifest_path = tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["qg_results"] = {"QG-1": False, "QG-8": False, "QG-9": True}
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
    )

    assert report.readiness_percent == 33
    assert report.blocker_count == 2


def test_status_cli_supports_human_and_json(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    program_path = tmp_path / "programs" / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["charter"] = {
        "scope_statement": "Deliver Acme ramp readiness for the current LT gate.",
    }
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")
    save_risk_register(
        "acme",
        (
            RiskEntry(
                id="risk-1",
                program_id="acme",
                title="Deployment telemetry may miss the weekly gate",
                description="Telemetry stabilization could slip the weekly decision checkpoint.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.TECHNICAL,
                owner_alias="operator",
                mitigation_plan="Track the blocker daily until telemetry is stable.",
                mitigation_due_date=date(2026, 5, 2),
                linked_workstream_ids=("deployment_readiness",),
                linked_work_item_ids=(900001,),
                linked_milestone_ids=("m3-code-complete",),
                linked_claim_ids=(),
                linked_action_ids=(),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 4, 1),
                identified_in_vertex_issue=0,
                last_reviewed_date=date(2026, 4, 1),
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=tmp_path / "programs",
    )

    monkeypatch.setattr("src.commands.status.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr("src.commands.status.ARCHIVE_ROOT", tmp_path / "archive")

    human_result = runner.invoke(app, ["status", "--edition", EDITION_NAME])
    json_result = runner.invoke(app, ["status", "--edition", EDITION_NAME, "--format", "json"])

    assert human_result.exit_code == 0
    assert "Program Hygiene — Issue 001" in human_result.stdout
    assert "Readiness:    81%" in human_result.stdout
    assert "Last confirm: unknown (weekly — no confirmed issues yet)" in human_result.stdout
    assert "Next due:     nova_daily (no confirmed issues yet; Acme platform-migration working team and DRIs; via teams; owner maintainer)" in human_result.stdout
    assert "Milestones:   1 at risk, 4 on track" in human_result.stdout
    assert "AI Safety:    disabled" in human_result.stdout
    assert "Scope:        Deliver Acme ramp readiness for the current LT gate." in human_result.stdout

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["edition"] == EDITION_NAME
    assert payload["display_name"] == "Program Hygiene"
    assert payload["issue_number"] == 1
    assert payload["readiness_percent"] == 81
    assert payload["blocker_count"] == 5
    assert payload["cadence_status"] == "no confirmed issues yet"
    assert payload["next_due_edition"] == "nova_daily"
    assert payload["next_due_status"] == "no confirmed issues yet"
    assert "Acme platform-migration working team and DRIs; via teams; owner maintainer" in payload["next_due_context"]
    assert payload["risk_register_summary"] == "1 active, 1 stale review"
    assert payload["milestone_summary"] == "1 at risk, 4 on track"
    assert payload["scope_statement"] == "Deliver Acme ramp readiness for the current LT gate."


def test_build_status_report_surfaces_ai_safety_from_latest_manifest(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    manifest_path = tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.setdefault("metadata", {})["ai_safety"] = {
        "enabled": True,
        "trace_run_id": "acme_weekly:issue-001:20260505T180000Z:lookback",
        "budget_usd": 0.5,
        "spent_usd": 0.012,
        "remaining_usd": 0.488,
        "ai_calls": 1,
        "within_budget": True,
        "budget_exceeded": False,
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
    )

    assert report.ai_safety_summary == (
        "1 AI call; $0.012000 / $0.500000 (within budget); "
        "trace acme_weekly:issue-001:20260505T180000Z:lookback"
    )
    assert report.to_payload()["ai_safety_summary"] == report.ai_safety_summary


def test_build_status_report_surfaces_open_ado_breaker_summary(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    _write_ado_breaker_state(
        tmp_path,
        payload={
            "state": "OPEN",
            "failure_count": 3,
            "last_failure_at": FROZEN_NOW.isoformat(),
            "last_opened_at": FROZEN_NOW.isoformat(),
            "last_success_at": None,
        },
    )

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
    )

    assert report.ado_breaker_summary == (
        "OPEN; failure_count=3; last_opened_at=2026-05-05T18:00:00+00:00; "
        "live freshness ADO requests gated"
    )
    assert report.to_payload()["ado_breaker_summary"] == report.ado_breaker_summary


def test_status_cli_surfaces_ai_safety_summary(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    manifest_path = tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.setdefault("metadata", {})["ai_safety"] = {
        "enabled": True,
        "trace_run_id": "acme_weekly:issue-001:20260505T180000Z:lookback",
        "budget_usd": 0.5,
        "spent_usd": 0.012,
        "remaining_usd": 0.488,
        "ai_calls": 1,
        "within_budget": True,
        "budget_exceeded": False,
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    monkeypatch.setattr("src.commands.status.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr("src.commands.status.ARCHIVE_ROOT", tmp_path / "archive")

    human_result = runner.invoke(app, ["status", "--edition", EDITION_NAME])
    json_result = runner.invoke(app, ["status", "--edition", EDITION_NAME, "--format", "json"])

    assert human_result.exit_code == 0
    assert "AI Safety:    1 AI call; $0.012000 / $0.500000 (within budget); trace acme_weekly:issue-001:20260505T180000Z:lookback" in human_result.stdout

    assert json_result.exit_code == 0
    assert json.loads(json_result.stdout)["ai_safety_summary"] == (
        "1 AI call; $0.012000 / $0.500000 (within budget); "
        "trace acme_weekly:issue-001:20260505T180000Z:lookback"
    )


def test_status_cli_surfaces_malformed_ai_safety_summary(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    manifest_path = tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload["metadata"] = {"ai_safety": "broken"}
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    monkeypatch.setattr("src.commands.status.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr("src.commands.status.ARCHIVE_ROOT", tmp_path / "archive")

    human_result = runner.invoke(app, ["status", "--edition", EDITION_NAME])
    json_result = runner.invoke(app, ["status", "--edition", EDITION_NAME, "--format", "json"])

    assert human_result.exit_code == 0
    assert f"AI Safety:    malformed manifest at {manifest_path}" in human_result.stdout

    assert json_result.exit_code == 0
    assert json.loads(json_result.stdout)["ai_safety_summary"] == f"malformed manifest at {manifest_path}"


def test_status_cli_tolerates_fully_malformed_latest_manifest(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    manifest_path = tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.manifest.json"
    manifest_path.write_text("{malformed", encoding="utf-8")

    monkeypatch.setattr("src.commands.status.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr("src.commands.status.ARCHIVE_ROOT", tmp_path / "archive")

    human_result = runner.invoke(app, ["status", "--edition", EDITION_NAME])
    json_result = runner.invoke(app, ["status", "--edition", EDITION_NAME, "--format", "json"])

    assert human_result.exit_code == 0
    assert f"AI Safety:    malformed manifest at {manifest_path}" in human_result.stdout

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["issue_number"] == 1
    assert payload["ai_safety_summary"] == f"malformed manifest at {manifest_path}"
    assert payload["readiness_percent"] is None


def test_status_cli_surfaces_malformed_ado_breaker_summary(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    breaker_path = tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / ".ado_breaker.json"
    breaker_path.write_text("{malformed", encoding="utf-8")

    monkeypatch.setattr("src.commands.status.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr("src.commands.status.ARCHIVE_ROOT", tmp_path / "archive")

    human_result = runner.invoke(app, ["status", "--edition", EDITION_NAME])
    json_result = runner.invoke(app, ["status", "--edition", EDITION_NAME, "--format", "json"])

    assert human_result.exit_code == 0
    assert f"ADO Breaker:  malformed state at {breaker_path}" in human_result.stdout

    assert json_result.exit_code == 0
    assert json.loads(json_result.stdout)["ado_breaker_summary"] == f"malformed state at {breaker_path}"


def test_status_cli_supports_csv(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    write_gather_state(
        "acme",
        gathered_at=FROZEN_NOW,
        scanned_items=0,
        discovered_signals=0,
        new_signals=0,
        pending_review=0,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=0,
        archived_journal_files=0,
        background_proposals=0,
        integration_errors=0,
        integration_error_details=(),
        programs_root=tmp_path / "programs",
    )
    manifest_path = tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.setdefault("metadata", {})["ai_safety"] = {
        "enabled": True,
        "trace_run_id": "acme_weekly:issue-001:20260505T180000Z:lookback",
        "budget_usd": 0.5,
        "spent_usd": 0.012,
        "remaining_usd": 0.488,
        "ai_calls": 1,
        "within_budget": True,
        "budget_exceeded": False,
    }
    manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    _write_ado_breaker_state(
        tmp_path,
        payload={
            "state": "OPEN",
            "failure_count": 3,
            "last_failure_at": FROZEN_NOW.isoformat(),
            "last_opened_at": FROZEN_NOW.isoformat(),
            "last_success_at": None,
        },
    )

    monkeypatch.setattr("src.commands.status.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr("src.commands.status.ARCHIVE_ROOT", tmp_path / "archive")

    result = runner.invoke(app, ["status", "--edition", EDITION_NAME, "--format", "csv"])
    rows = list(csv.DictReader(result.stdout.splitlines()))

    assert result.exit_code == 0
    assert rows[0]["edition"] == EDITION_NAME
    assert rows[0]["display_name"] == "Program Hygiene"
    assert rows[0]["readiness_percent"] == "81"
    assert rows[0]["blocker_count"] == "5"
    assert rows[0]["cadence_status"] == "no confirmed issues yet"
    assert rows[0]["ai_safety_summary"] == (
        "1 AI call; $0.012000 / $0.500000 (within budget); "
        "trace acme_weekly:issue-001:20260505T180000Z:lookback"
    )
    assert rows[0]["ado_breaker_summary"] == (
        "OPEN; failure_count=3; last_opened_at=2026-05-05T18:00:00+00:00; "
        "live freshness ADO requests gated"
    )
    assert rows[0]["capability_review_summary"] == "latest 2026-05-19"
    assert rows[0]["capability_verification_summary"] == (
        "live verification pending: Graph app-only auth"
    )
    assert rows[0]["gather_integration_details_json"] == "[]"
    capabilities = json.loads(rows[0]["capabilities_json"])
    assert capabilities[0]["capability_id"] == "ado_activation"
    assert capabilities[3]["status"] == "deferred"


def test_status_csv_includes_gather_integration_details_json(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    programs_root = tmp_path / "programs"

    write_gather_state(
        "acme",
        gathered_at=datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc),
        scanned_items=4,
        discovered_signals=2,
        new_signals=1,
        pending_review=1,
        trajectory_updates=0,
        auto_reviews_written=0,
        ado_calls=3,
        archived_journal_files=0,
        background_proposals=0,
        integration_errors=1,
        integration_error_details=(
            IntegrationError(
                source="workiq",
                stage="gather",
                retryable=True,
                message="workiq unavailable",
                operator_action="Verify Agency CLI WorkIQ support before retrying gather.",
            ),
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr("src.commands.status.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr("src.commands.status.ARCHIVE_ROOT", tmp_path / "archive")

    result = runner.invoke(app, ["status", "--edition", EDITION_NAME, "--format", "csv"])
    rows = list(csv.DictReader(result.stdout.splitlines()))

    assert result.exit_code == 0
    details = json.loads(rows[0]["gather_integration_details_json"])
    assert details == [
        {
            "message": "workiq unavailable",
            "operator_action": "Verify Agency CLI WorkIQ support before retrying gather.",
            "retryable": True,
            "source": "workiq",
            "stage": "gather",
        }
    ]


def test_build_status_report_surfaces_program_capability_status(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    capability_path = tmp_path / "programs" / "acme" / "capability_status.yaml"
    capability_path.write_text(
        "\n".join(
            (
                "schema_version: '1.0'",
                "capabilities:",
                "  - id: kusto_activation",
                "    status: in_progress",
                "    summary: Kusto activation is explicitly in progress for this test workspace.",
                "    degradation: Live cluster validation is still pending.",
                "    last_reviewed_on: 2026-05-17",
                "  - id: m365_activation",
                "    status: deferred",
                "    summary: M365 activation is explicitly deferred for this test workspace.",
                "    degradation: WorkIQ enrichment remains inactive.",
                "    last_reviewed_on: 2026-05-15",
                "  - id: graph_app_only_auth",
                "    status: deferred",
                "    summary: Graph app-only auth is explicitly deferred for this test workspace.",
                "    degradation: L2 governance graduation remains unavailable.",
                "    last_reviewed_on: 2026-05-10",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
    )

    assert report.capability_summary == "Kusto activation in progress; M365 activation deferred; Graph app-only auth deferred"
    assert report.capability_review_summary == "latest 2026-05-17"
    assert report.capability_verification_summary == "live verification pending: Kusto activation, M365 activation, Graph app-only auth"
    assert report.latest_capability_reviewed_on is not None
    assert report.latest_capability_reviewed_on.isoformat() == "2026-05-17"
    assert [entry.capability_id for entry in report.capabilities] == ["kusto_activation", "m365_activation", "graph_app_only_auth"]
    assert report.to_payload()["capabilities"][0]["status"] == "in_progress"
    assert report.to_payload()["capabilities"][2]["status"] == "deferred"
    assert report.to_payload()["capability_review_summary"] == "latest 2026-05-17"
    assert report.to_payload()["capability_verification_summary"] == report.capability_verification_summary
    assert report.to_payload()["latest_capability_reviewed_on"] == "2026-05-17"


def test_build_status_report_surfaces_ado_activation_as_pending_verification(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    capability_path = tmp_path / "programs" / "acme" / "capability_status.yaml"
    capability_path.write_text(
        "\n".join(
            (
                "schema_version: '1.0'",
                "capabilities:",
                "  - id: ado_activation",
                "    status: unknown",
                "    summary: ADO activation is waiting on a live auth confirmation in this test workspace.",
                "    degradation: Operator-visible promotion remains pending.",
                "  - id: kusto_activation",
                "    status: complete",
                "    summary: Kusto activation is complete in this test workspace.",
                "    last_reviewed_on: 2026-05-17",
                "  - id: m365_activation",
                "    status: complete",
                "    summary: M365 activation is complete in this test workspace.",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
    )

    assert [entry.capability_id for entry in report.capabilities] == [
        "ado_activation",
        "kusto_activation",
        "m365_activation",
        "graph_app_only_auth",
    ]
    assert report.capabilities[0].label == "ADO activation"
    assert report.capability_summary == "ADO activation unknown; Kusto activation complete; M365 activation complete; Graph app-only auth unavailable"
    assert report.capability_review_summary == "latest 2026-05-17; missing review dates: ADO activation, M365 activation"
    assert report.capability_verification_summary == "live verification pending: ADO activation"


def test_build_status_report_surfaces_missing_capability_review_dates(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    capability_path = tmp_path / "programs" / "acme" / "capability_status.yaml"
    capability_path.write_text(
        "\n".join(
            (
                "schema_version: '1.0'",
                "capabilities:",
                "  - id: kusto_activation",
                "    status: in_progress",
                "    summary: Kusto activation is explicitly in progress for this test workspace.",
                "    degradation: Live cluster validation is still pending.",
                "    last_reviewed_on: 2026-05-17",
                "  - id: m365_activation",
                "    status: deferred",
                "    summary: M365 activation is explicitly deferred for this test workspace.",
                "    degradation: WorkIQ enrichment remains inactive.",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
    )

    assert report.capability_review_summary == "latest 2026-05-17; missing review dates: M365 activation"
    assert report.capability_verification_summary == "live verification pending: Kusto activation, M365 activation"
    assert report.to_payload()["capability_review_summary"] == report.capability_review_summary
    assert report.to_payload()["capability_verification_summary"] == report.capability_verification_summary


def test_build_status_report_surfaces_latest_approved_telemetry_summary(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    from src.commands import status as status_module

    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    analytics_signal = Signal(
        id="analytics-1",
        timestamp=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260505:20260421:20260505",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 5,
            "completed_item_count": 2,
            "scope_delta_count": 2,
            "open_delta_count": -1,
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
    )
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-05",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "team_member_count": 3,
            "total_capacity_per_day": 24.0,
        },
    )
    pipeline_signal = Signal(
        id="pipeline-1",
        timestamp=datetime(2026, 5, 5, 11, 30, tzinfo=timezone.utc),
        source="ado/pipeline",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: pipeline summary",
        raw_ref="ado-pipeline:deployment_readiness:42:2026-05-05",
        confidence=Confidence.HIGH,
        metadata={
            "pipelines": [
                {
                    "pipeline_name": "Build Validation",
                    "recent_run_count": 3,
                    "failed_run_count": 1,
                    "latest_failure_run_id": 104,
                    "latest_run_id": 105,
                    "latest_run_result": "succeeded",
                }
            ]
        },
    )
    dismissed_signal = Signal(
        id="analytics-2",
        timestamp=datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Dismissed analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260505:20260421:20260505:dismissed",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 9,
            "completed_item_count": 9,
            "open_delta_count": -4,
        },
    )

    for signal in (analytics_signal, sprint_signal, pipeline_signal, dismissed_signal):
        append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="analytics-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 10, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="pipeline-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 11, 35, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="analytics-2",
            decision="dismissed",
            reviewed_at=datetime(2026, 5, 5, 12, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
    )

    assert report.telemetry_summary == (
        "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; "
        "sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members; "
        "pipeline, Build Validation, 1/3 failed, latest fail #104, latest #105 succeeded"
    )
    assert report.telemetry_confidence == "high"
    assert report.to_payload()["telemetry_confidence"] == "high"
    assert "Telemetry:    analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members; pipeline, Build Validation, 1/3 failed, latest fail #104, latest #105 succeeded (high confidence)" in status_module.render_status_report(report)


def test_build_status_report_surfaces_latest_approved_telemetry_summary_from_sqlite_backend(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    program_path = programs_root / "acme" / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_document["storage_backend"] = "sqlite"
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False, allow_unicode=False), encoding="utf-8")

    store = SQLiteSignalStore(programs_root=programs_root)
    analytics_signal = Signal(
        id="analytics-1",
        timestamp=datetime(2026, 5, 5, 10, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260505:20260421:20260505",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 5,
            "completed_item_count": 2,
            "scope_delta_count": 2,
            "open_delta_count": -1,
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
    )
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-05",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "team_member_count": 3,
            "total_capacity_per_day": 24.0,
        },
    )
    pipeline_signal = Signal(
        id="pipeline-1",
        timestamp=datetime(2026, 5, 5, 11, 30, tzinfo=timezone.utc),
        source="ado/pipeline",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: pipeline summary",
        raw_ref="ado-pipeline:deployment_readiness:42:2026-05-05",
        confidence=Confidence.HIGH,
        metadata={
            "pipelines": [
                {
                    "pipeline_name": "Build Validation",
                    "recent_run_count": 3,
                    "failed_run_count": 1,
                    "latest_failure_run_id": 104,
                    "latest_run_id": 105,
                    "latest_run_result": "succeeded",
                }
            ]
        },
    )
    dismissed_signal = Signal(
        id="analytics-2",
        timestamp=datetime(2026, 5, 5, 12, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Dismissed analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260505:20260421:20260505:dismissed",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 9,
            "completed_item_count": 9,
            "open_delta_count": -4,
        },
    )

    for signal in (analytics_signal, sprint_signal, pipeline_signal, dismissed_signal):
        store.append(signal)
    for decision in (
        SignalReviewDecision(
            signal_id="analytics-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 10, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        SignalReviewDecision(
            signal_id="pipeline-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 11, 35, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        SignalReviewDecision(
            signal_id="analytics-2",
            decision="dismissed",
            reviewed_at=datetime(2026, 5, 5, 12, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
    ):
        store.append_review("acme", decision)

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
    )

    assert report.telemetry_summary == (
        "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; "
        "sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members; "
        "pipeline, Build Validation, 1/3 failed, latest fail #104, latest #105 succeeded"
    )
    assert report.telemetry_confidence == "high"


def test_build_status_report_surfaces_snapshot_backed_previous_sprint_throughput_comparison(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-05",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "recent_completion_per_business_day": 1.0,
            "recent_completion_snapshot_count": 3,
            "previous_iteration_completion_per_business_day": 0.5,
        },
    )

    append_signal(sprint_signal, programs_root=programs_root, partition_at=sprint_signal.timestamp)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
    )

    assert report.telemetry_summary == (
        "sprint, Sprint 24, 50% complete, 1 open, recent 1.0/day over 3 snapshots, 0.5/day faster vs last sprint"
    )


def test_build_status_report_surfaces_snapshot_backed_previous_sprint_history(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-05",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 100,
            "open_item_count": 0,
            "recent_completion_per_business_day": 1.0,
            "recent_completion_snapshot_count": 3,
            "previous_iteration_open_item_count": 1,
            "previous_iteration_open_history": {
                "2026-05-06": 2,
                "2026-05-07": 1,
                "2026-05-08": 1,
            },
            "previous_iteration_completed_history": {
                "2026-05-06": 0,
                "2026-05-07": 1,
                "2026-05-08": 1,
            },
            "previous_iteration_completion_per_business_day": 0.5,
        },
    )

    append_signal(sprint_signal, programs_root=programs_root, partition_at=sprint_signal.timestamp)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
    )

    assert report.telemetry_summary == (
        "sprint, Sprint 24, 100% complete, 0 open, recent 1.0/day over 3 snapshots, "
        "1 fewer open vs last sprint, last sprint burndown 2->1->1 open, "
        "last sprint completion 0->1->1 done, 0.5/day faster vs last sprint"
    )


def test_build_status_report_surfaces_snapshot_backed_three_sprint_history_summaries(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-05",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 100,
            "open_item_count": 0,
            "three_iteration_average_completion_per_business_day": 1.0,
            "three_iteration_completion_per_business_day_history": (0.5, 1.0, 1.5),
            "three_iteration_completed_history_series": ((0, 1, 1), (0, 2, 2), (0, 2, 3)),
            "three_iteration_throughput_trend_direction": "up",
            "three_iteration_throughput_trend_delta_per_business_day": 1.0,
            "three_iteration_average_open_item_count": 1,
            "three_iteration_open_item_count_history": (2, 1, 0),
            "three_iteration_open_history_series": ((3, 2, 2), (3, 1, 1), (3, 1, 0)),
            "three_iteration_open_trend_direction": "down",
            "three_iteration_open_trend_delta_count": -2,
        },
    )

    append_signal(sprint_signal, programs_root=programs_root, partition_at=sprint_signal.timestamp)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
    )

    assert report.telemetry_summary == (
        "sprint, Sprint 24, 100% complete, 0 open, 3-sprint avg 1.0/day, 3-sprint throughput 0.5->1.0->1.5/day, "
        "throughput trend up 1.0/day over 3 sprints, 3-sprint open avg 1, 3-sprint open 2->1->0, "
        "3-sprint burndown 3->2->2 | 3->1->1 | 3->1->0 open, 3-sprint completion 0->1->1 | 0->2->2 | 0->2->3 done, "
        "open trend down 2 over 3 sprints"
    )


def test_build_status_report_surfaces_snapshot_backed_broader_historical_sprint_window(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    programs_root = tmp_path / "programs"
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 5, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-05",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 100,
            "open_item_count": 0,
            "three_iteration_average_completion_per_business_day": 1.0,
            "three_iteration_completion_per_business_day_history": (0.5, 1.0, 1.5),
            "three_iteration_completed_history_series": ((0, 1, 1), (0, 2, 2), (0, 2, 3)),
            "three_iteration_throughput_trend_direction": "up",
            "three_iteration_throughput_trend_delta_per_business_day": 1.0,
            "three_iteration_average_open_item_count": 1,
            "three_iteration_open_item_count_history": (2, 1, 0),
            "three_iteration_open_history_series": ((3, 2, 2), (3, 1, 1), (3, 1, 0)),
            "three_iteration_open_trend_direction": "down",
            "three_iteration_open_trend_delta_count": -2,
            "historical_iteration_window_count": 4,
            "historical_completion_per_business_day_history": (1.0, 0.5, 1.0, 1.5),
            "historical_completed_history_series": ((0, 1, 2), (0, 1, 1), (0, 2, 2), (0, 2, 3)),
            "historical_throughput_trend_direction": None,
            "historical_throughput_trend_delta_per_business_day": None,
            "historical_open_item_count_history": (1, 2, 1, 0),
            "historical_open_history_series": ((3, 2, 1), (3, 2, 2), (3, 1, 1), (3, 1, 0)),
            "historical_open_trend_direction": None,
            "historical_open_trend_delta_count": None,
        },
    )

    append_signal(sprint_signal, programs_root=programs_root, partition_at=sprint_signal.timestamp)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 5, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=programs_root,
        archive_root=tmp_path / "archive",
    )

    assert report.telemetry_summary == (
        "sprint, Sprint 24, 100% complete, 0 open, 3-sprint avg 1.0/day, 3-sprint throughput 0.5->1.0->1.5/day, "
        "throughput trend up 1.0/day over 3 sprints, 4-sprint throughput 1.0->0.5->1.0->1.5/day, "
        "3-sprint open avg 1, 3-sprint open 2->1->0, 4-sprint open 1->2->1->0, 3-sprint burndown 3->2->2 | 3->1->1 | 3->1->0 open, "
        "3-sprint completion 0->1->1 | 0->2->2 | 0->2->3 done, 4-sprint burndown 3->2->1 | 3->2->2 | 3->1->1 | 3->1->0 open, "
        "4-sprint completion 0->1->2 | 0->1->1 | 0->2->2 | 0->2->3 done, open trend down 2 over 3 sprints"
    )


def test_build_status_report_uses_communication_plan_for_next_due(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    shutil.copy2(repo_root / "programs" / "acme" / "editions" / "nova_daily.yaml", tmp_path / "programs" / "acme" / "editions" / "nova_daily.yaml")
    program_path = tmp_path / "programs" / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["communication_plan"] = [
        {
            "edition": "nova_daily",
            "audience": "Eng leads + DRIs",
            "channel": "teams",
            "cadence": "daily",
            "owner": "maintainer",
        }
    ]
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")
    _write_confirmed_archive_index(
        tmp_path / "archive",
        edition_name="nova_daily",
        generated_at=FROZEN_NOW - timedelta(days=2),
    )

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
    )

    assert report.next_due_edition == "nova_daily"
    assert report.next_due_status == "overdue by 1 day"
    assert report.next_due_context == "Eng leads + DRIs; via teams; owner maintainer"


def test_build_status_report_prioritizes_overdue_communication_plan_entries_over_unconfirmed_editions(
    monkeypatch,
    repo_root: Path,
    tmp_path: Path,
) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    shutil.copy2(repo_root / "programs" / "acme" / "editions" / "nova_daily.yaml", tmp_path / "programs" / "acme" / "editions" / "nova_daily.yaml")
    program_path = tmp_path / "programs" / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["communication_plan"] = [
        {
            "edition": "acme_weekly",
            "audience": "LT + partner PMs",
            "channel": "email",
            "cadence": "weekly",
            "owner": "maintainer",
        },
        {
            "edition": "nova_daily",
            "audience": "Eng leads + DRIs",
            "channel": "teams",
            "cadence": "daily",
            "owner": "maintainer",
        },
    ]
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")
    _write_confirmed_archive_index(
        tmp_path / "archive",
        edition_name="nova_daily",
        generated_at=FROZEN_NOW - timedelta(days=2),
    )

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
    )

    assert report.next_due_edition == "nova_daily"
    assert report.next_due_status == "overdue by 1 day"
    assert report.next_due_context == "Eng leads + DRIs; via teams; owner maintainer"


def test_build_status_report_preserves_communication_plan_context_for_duplicate_editions(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    _seed_status_workspace(monkeypatch, repo_root, tmp_path)
    program_path = tmp_path / "programs" / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["communication_plan"] = [
        {
            "edition": "acme_weekly",
            "audience": "LT + partner PMs",
            "channel": "email",
            "cadence": "weekly",
            "owner": "maintainer",
        },
        {
            "edition": "acme_weekly",
            "audience": "DRIs",
            "channel": "teams",
            "cadence": "weekly",
            "owner": "demo",
        },
    ]
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")

    report = build_status_report(
        EDITION_NAME,
        as_of=FROZEN_NOW,
        programs_root=tmp_path / "programs",
        archive_root=tmp_path / "archive",
    )

    assert report.next_due_edition == EDITION_NAME
    assert report.next_due_context == "LT + partner PMs; via email; owner maintainer"


def _seed_status_workspace(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    monkeypatch.setattr("uuid.uuid4", lambda: FROZEN_MANIFEST_ID)

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=tmp_path / "programs",
        as_of=FROZEN_NOW,
        work_item_loader=lambda bundle, timestamp: load_cassette_work_items("cold_start", timestamp),
        open_browser=False,
    )


def _write_confirmed_archive_index(archive_root: Path, *, edition_name: str, generated_at: datetime) -> None:
    edition_root = get_archive_root(edition_name, archive_root=archive_root)
    edition_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "edition": edition_name,
        "issues": [
            {
                "issue_number": 1,
                "generated_at": generated_at.isoformat(),
                "kind": "confirmed",
            }
        ],
    }
    (edition_root / "index.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_ado_breaker_state(tmp_path: Path, *, payload: dict[str, object]) -> None:
    breaker_path = tmp_path / "programs" / "acme" / "publications" / EDITION_NAME / ".ado_breaker.json"
    breaker_path.parent.mkdir(parents=True, exist_ok=True)
    breaker_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
