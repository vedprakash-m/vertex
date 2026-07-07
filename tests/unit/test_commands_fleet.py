from __future__ import annotations

import csv
import json
import shutil
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner
import yaml

from cli import app
from src.commands import fleet as fleet_module
from src.commands.fleet import _format_fleet_issue_detail, build_fleet_report, render_fleet_csv, render_fleet_markdown
from src.core.claim_tracker import append_claim_entry
from src.core.action_tracker import append_action
from src.core.claim_tracker import append_decision_ask
from src.core.exceptions import ConfigError
from src.core.gather_state_store import write_gather_state
from src.core.issue_projection import IssueProjection
from src.core.journal import append_review_decision, append_signal
from src.core.models import Confidence
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, ClaimEntry, DecisionAsk, Dependency, DependencyStatus, DependencyType, IntegrationError, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus, Signal, SignalReviewDecision
from src.core.risk_register_engine import save_risk_register
from src.core.sqlite_stores import SQLiteSignalStore


runner = CliRunner()
FROZEN_NOW = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)


def test_format_fleet_issue_detail_includes_confidence() -> None:
    detail = _format_fleet_issue_detail(
        IssueProjection(
            work_item_id=900001,
            source_type="ado_blocked",
            severity="block",
            summary="Blocked ramp readiness",
            owner_alias="maintainer",
            workstream_id=None,
            ado_url=None,
            linked_entity_ids=("ask-1",),
            confidence=Confidence.HIGH,
        )
    )

    assert detail == "ado blocked | BLOCK | high confidence | owner maintainer | linked ask-1"


def test_build_fleet_report_prefers_weekly_and_surfaces_cross_program_state(tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    assert len(report.programs) == 2
    acme = next(program for program in report.programs if program.program_id == "acme")
    fabrikam = next(program for program in report.programs if program.program_id == "fabrikam")

    assert acme.primary_edition == "acme_weekly"
    assert acme.latest_issue_number == 3
    assert acme.overall_risk.value == "high"
    assert acme.active_issue_count == 0
    assert acme.risk_register.active_count == 0
    assert acme.risk_register.stale_count == 0
    assert acme.risk_register.highlight is None
    assert acme.dependency_health.total_count == 1
    assert acme.dependency_health.outbound_count == 1
    assert acme.dependency_health.inbound_count == 0
    assert acme.dependency_health.broken_count == 1
    assert acme.dependency_health.highlight == "outbound fabrikam | BROKEN informs | m3-code-complete -> fabrikam:buildouts | Fabrikam buildout planning stays provisional until Acme code-complete is held."
    assert acme.trend == "worsening"
    assert acme.top_items == (
        "BIOS compliance remains the hard gate for ramp readiness.",
        "Networking validation closes next week's release decision.",
    )
    assert acme.stale is False
    assert any(dependency.direction == "outbound" and dependency.counterpart_program_id == "fabrikam" for dependency in acme.cross_program_dependencies)

    assert fabrikam.primary_edition == "fabrikam_weekly"
    assert fabrikam.stale is True
    assert fabrikam.dependency_health.total_count == 1
    assert fabrikam.dependency_health.outbound_count == 0
    assert fabrikam.dependency_health.inbound_count == 1
    assert fabrikam.dependency_health.broken_count == 1
    assert any(dependency.direction == "inbound" and dependency.counterpart_program_id == "acme" for dependency in fabrikam.cross_program_dependencies)


def test_fleet_cli_supports_all_formats_and_program_filter(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    monkeypatch.setattr("src.commands.fleet.PROGRAMS_ROOT", programs_root)

    class _FrozenDT(datetime):
        @classmethod
        def now(cls, tz=None):
            return FROZEN_NOW if tz is None else FROZEN_NOW.astimezone(tz)

    monkeypatch.setattr("src.commands.fleet.datetime", _FrozenDT)

    human_result = runner.invoke(app, ["fleet", "--programs", "acme"])
    json_result = runner.invoke(app, ["fleet", "--programs", "acme", "--format", "json"])
    csv_result = runner.invoke(app, ["fleet", "--programs", "acme", "--format", "csv"])
    md_result = runner.invoke(app, ["fleet", "--programs", "acme", "--format", "md"])
    html_result = runner.invoke(app, ["fleet", "--programs", "acme", "--format", "html"])

    assert human_result.exit_code == 0
    assert "Fleet Summary — 1 program" in human_result.stdout
    assert "Adventure + DD on PF (acme)" in human_result.stdout
    assert "Primary edition: acme_weekly" in human_result.stdout
    assert "Active issues: 0" in human_result.stdout
    assert "Risk register: 0 active, 0 stale" in human_result.stdout
    assert "Dependency health: 1 linked, 1 outbound, 0 inbound, 1 broken" in human_result.stdout
    assert "Dependency highlight: outbound fabrikam | BROKEN informs | m3-code-complete -> fabrikam:buildouts | Fabrikam buildout planning stays provisional until Acme code-complete is held." in human_result.stdout

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["program_count"] == 1
    assert payload["programs"][0]["program_id"] == "acme"
    assert payload["programs"][0]["primary_edition"] == "acme_weekly"
    assert payload["programs"][0]["active_issue_count"] == 0
    assert payload["programs"][0]["risk_register"]["active_count"] == 0
    assert payload["programs"][0]["dependency_health"]["broken_count"] == 1

    assert csv_result.exit_code == 0
    rows = list(csv.DictReader(csv_result.stdout.splitlines()))
    assert rows[0]["program_id"] == "acme"
    assert rows[0]["primary_edition"] == "acme_weekly"
    assert rows[0]["dependency_broken_count"] == "1"
    assert rows[0]["stale"] == "false"

    assert md_result.exit_code == 0
    assert "# Fleet Summary" in md_result.stdout
    assert "## Adventure + DD on PF (acme)" in md_result.stdout
    assert "- Active issues: 0" in md_result.stdout
    assert "- Risk register: 0 active, 0 stale" in md_result.stdout
    assert "- Dependency health: 1 linked, 1 outbound, 0 inbound, 1 broken" in md_result.stdout

    assert html_result.exit_code == 0
    assert "<html>" in html_result.stdout
    assert "Fleet Summary" in html_result.stdout
    assert "Adventure + DD on PF" in html_result.stdout
    assert "Risk register" in html_result.stdout
    assert "1 linked, 1 outbound, 0 inbound, 1 broken" in html_result.stdout


def test_build_fleet_report_surfaces_program_capability_status(tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    (programs_root / "acme" / "capability_status.yaml").write_text(
        "\n".join(
            (
                "schema_version: '1.0'",
                "capabilities:",
                "  - id: kusto_activation",
                "    status: in_progress",
                "    summary: Kusto activation is explicitly in progress for the Acme fleet test.",
                "    degradation: Live cluster validation is still pending.",
                "    last_reviewed_on: 2026-05-17",
                "  - id: m365_activation",
                "    status: deferred",
                "    summary: M365 activation is explicitly deferred for the Acme fleet test.",
                "    degradation: WorkIQ enrichment remains inactive.",
                "    last_reviewed_on: 2026-05-15",
                "  - id: graph_app_only_auth",
                "    status: deferred",
                "    summary: Graph app-only auth is explicitly deferred for the Acme fleet test.",
                "    degradation: L2 governance graduation remains unavailable.",
                "    last_reviewed_on: 2026-05-10",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.capability_summary == "Kusto activation in progress; M365 activation deferred; Graph app-only auth deferred"
    assert acme.capability_review_summary == "latest 2026-05-17"
    assert acme.capability_verification_summary == "live verification pending: Kusto activation, M365 activation, Graph app-only auth"
    assert acme.latest_capability_reviewed_on is not None
    assert acme.latest_capability_reviewed_on.isoformat() == "2026-05-17"
    assert [entry.capability_id for entry in acme.capabilities] == ["kusto_activation", "m365_activation", "graph_app_only_auth"]
    payload = next(program for program in report.to_payload()["programs"] if program["program_id"] == "acme")
    assert payload["capabilities"][0]["status"] == "in_progress"
    assert payload["capabilities"][2]["status"] == "deferred"
    assert payload["capability_review_summary"] == "latest 2026-05-17"
    assert payload["capability_verification_summary"] == acme.capability_verification_summary
    assert payload["latest_capability_reviewed_on"] == "2026-05-17"
    csv_rows = list(csv.DictReader(render_fleet_csv(report).splitlines()))
    nova_row = next(row for row in csv_rows if row["program_id"] == "acme")
    assert nova_row["capability_review_summary"] == "latest 2026-05-17"
    assert nova_row["capability_verification_summary"] == acme.capability_verification_summary
    capabilities = json.loads(nova_row["capabilities_json"])
    assert capabilities[0]["capability_id"] == "kusto_activation"
    assert capabilities[2]["status"] == "deferred"


def test_build_fleet_report_surfaces_gather_integration_summary_and_csv_details(tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    write_gather_state(
        "acme",
        gathered_at=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
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

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.gather_integration_summary == (
        "1 optional integration failure(s); workiq/gather: workiq unavailable. "
        "Next: Verify Agency CLI WorkIQ support before retrying gather."
    )
    payload = next(program for program in report.to_payload()["programs"] if program["program_id"] == "acme")
    assert payload["gather_integration_details"] == [
        {
            "source": "workiq",
            "stage": "gather",
            "retryable": True,
            "message": "workiq unavailable",
            "operator_action": "Verify Agency CLI WorkIQ support before retrying gather.",
        }
    ]
    csv_rows = list(csv.DictReader(render_fleet_csv(report).splitlines()))
    nova_row = next(row for row in csv_rows if row["program_id"] == "acme")
    assert nova_row["gather_integration_summary"] == acme.gather_integration_summary
    assert json.loads(nova_row["gather_integration_details_json"]) == payload["gather_integration_details"]
    rendered = render_fleet_markdown(report)
    assert "- Gather: 1 optional integration failure(s); workiq/gather: workiq unavailable." in rendered


def test_build_fleet_report_surfaces_missing_capability_review_dates(tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    (programs_root / "acme" / "capability_status.yaml").write_text(
        "\n".join(
            (
                "schema_version: '1.0'",
                "capabilities:",
                "  - id: kusto_activation",
                "    status: in_progress",
                "    summary: Kusto activation is explicitly in progress for the Acme fleet test.",
                "    degradation: Live cluster validation is still pending.",
                "    last_reviewed_on: 2026-05-17",
                "  - id: m365_activation",
                "    status: deferred",
                "    summary: M365 activation is explicitly deferred for the Acme fleet test.",
                "    degradation: WorkIQ enrichment remains inactive.",
            )
        )
        + "\n",
        encoding="utf-8",
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.capability_review_summary == "latest 2026-05-17; missing review dates: M365 activation"
    assert acme.capability_verification_summary == "live verification pending: Kusto activation, M365 activation"
    payload = next(program for program in report.to_payload()["programs"] if program["program_id"] == "acme")
    assert payload["capability_review_summary"] == acme.capability_review_summary
    assert payload["capability_verification_summary"] == acme.capability_verification_summary


def test_build_fleet_report_surfaces_open_ado_breaker_summary(tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    _write_ado_breaker_state(
        programs_root,
        program_id="acme",
        edition_name="acme_weekly",
        payload={
            "state": "OPEN",
            "failure_count": 3,
            "last_failure_at": FROZEN_NOW.isoformat(),
            "last_opened_at": FROZEN_NOW.isoformat(),
            "last_success_at": None,
        },
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.ado_breaker_summary == (
        "OPEN; failure_count=3; last_opened_at=2026-05-10T18:00:00+00:00; "
        "live freshness ADO requests gated"
    )
    nova_payload = next(program for program in report.to_payload()["programs"] if program["program_id"] == "acme")
    assert nova_payload["ado_breaker_summary"] == acme.ado_breaker_summary

    csv_rows = list(csv.DictReader(render_fleet_csv(report).splitlines()))
    nova_row = next(row for row in csv_rows if row["program_id"] == "acme")
    assert nova_row["ado_breaker_summary"] == acme.ado_breaker_summary

    rendered = render_fleet_markdown(report)
    assert (
        "- ADO Breaker: OPEN; failure_count=3; last_opened_at=2026-05-10T18:00:00+00:00; "
        "live freshness ADO requests gated"
    ) in rendered


def test_build_fleet_report_surfaces_ai_safety_summary_from_latest_manifest(tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    _write_ai_safety_manifest(
        programs_root,
        program_id="acme",
        edition_name="acme_weekly",
        issue_number=4,
        ai_safety={
            "enabled": True,
            "trace_run_id": "acme_weekly:issue-004:20260510T180000Z:report",
            "budget_usd": 0.5,
            "spent_usd": 0.012,
            "remaining_usd": 0.488,
            "ai_calls": 1,
            "within_budget": True,
            "budget_exceeded": False,
        },
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.ai_safety_summary == (
        "1 AI call; $0.012000 / $0.500000 (within budget); "
        "trace acme_weekly:issue-004:20260510T180000Z:report"
    )
    nova_payload = next(program for program in report.to_payload()["programs"] if program["program_id"] == "acme")
    assert nova_payload["ai_safety_summary"] == acme.ai_safety_summary

    csv_rows = list(csv.DictReader(render_fleet_csv(report).splitlines()))
    nova_row = next(row for row in csv_rows if row["program_id"] == "acme")
    assert nova_row["ai_safety_summary"] == acme.ai_safety_summary

    rendered = render_fleet_markdown(report)
    assert (
        "- AI Safety: 1 AI call; $0.012000 / $0.500000 (within budget); "
        "trace acme_weekly:issue-004:20260510T180000Z:report"
    ) in rendered


def test_fleet_cli_surfaces_malformed_ado_breaker_summary(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    breaker_path = programs_root / "acme" / "publications" / "acme_weekly" / ".ado_breaker.json"
    breaker_path.parent.mkdir(parents=True, exist_ok=True)
    breaker_path.write_text("{malformed", encoding="utf-8")

    monkeypatch.setattr("src.commands.fleet.PROGRAMS_ROOT", programs_root)

    human_result = runner.invoke(app, ["fleet", "--programs", "acme"])
    json_result = runner.invoke(app, ["fleet", "--programs", "acme", "--format", "json"])
    html_result = runner.invoke(app, ["fleet", "--programs", "acme", "--format", "html"])

    assert human_result.exit_code == 0
    assert "ADO Breaker: malformed state at " in human_result.stdout
    assert ".ado_breaker.json" in human_result.stdout

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["programs"][0]["ado_breaker_summary"] is not None
    assert payload["programs"][0]["ado_breaker_summary"].startswith("malformed state at ")
    assert payload["programs"][0]["ado_breaker_summary"].endswith(".ado_breaker.json")

    assert html_result.exit_code == 0
    assert "ADO breaker" in html_result.stdout
    assert "malformed state at " in html_result.stdout
    assert ".ado_breaker.json" in html_result.stdout


def test_fleet_cli_surfaces_ai_safety_summary(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    _write_ai_safety_manifest(
        programs_root,
        program_id="acme",
        edition_name="acme_weekly",
        issue_number=4,
        ai_safety={
            "enabled": True,
            "trace_run_id": "acme_weekly:issue-004:20260510T180000Z:report",
            "budget_usd": 0.5,
            "spent_usd": 0.012,
            "remaining_usd": 0.488,
            "ai_calls": 1,
            "within_budget": True,
            "budget_exceeded": False,
        },
    )

    monkeypatch.setattr("src.commands.fleet.PROGRAMS_ROOT", programs_root)

    human_result = runner.invoke(app, ["fleet", "--programs", "acme"])
    json_result = runner.invoke(app, ["fleet", "--programs", "acme", "--format", "json"])
    html_result = runner.invoke(app, ["fleet", "--programs", "acme", "--format", "html"])

    assert human_result.exit_code == 0
    assert (
        "AI Safety:   1 AI call; $0.012000 / $0.500000 (within budget); "
        "trace acme_weekly:issue-004:20260510T180000Z:report"
    ) in human_result.stdout

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["programs"][0]["ai_safety_summary"] == (
        "1 AI call; $0.012000 / $0.500000 (within budget); "
        "trace acme_weekly:issue-004:20260510T180000Z:report"
    )

    assert html_result.exit_code == 0
    assert "AI safety" in html_result.stdout
    assert "acme_weekly:issue-004:20260510T180000Z:report" in html_result.stdout


def test_fleet_cli_surfaces_malformed_ai_safety_summary(monkeypatch, tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    manifest_path = programs_root / "acme" / "publications" / "acme_weekly" / "issue_004" / "issue_004.manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "issue_number": 4,
                "metadata": {"ai_safety": "broken"},
                "qg_results": {},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr("src.commands.fleet.PROGRAMS_ROOT", programs_root)

    human_result = runner.invoke(app, ["fleet", "--programs", "acme"])
    json_result = runner.invoke(app, ["fleet", "--programs", "acme", "--format", "json"])
    html_result = runner.invoke(app, ["fleet", "--programs", "acme", "--format", "html"])

    assert human_result.exit_code == 0
    assert "AI Safety:   malformed manifest at " in human_result.stdout
    assert "issue_004.manifest.json" in human_result.stdout

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["programs"][0]["ai_safety_summary"] is not None
    assert payload["programs"][0]["ai_safety_summary"].startswith("malformed manifest at ")
    assert payload["programs"][0]["ai_safety_summary"].endswith("issue_004.manifest.json")

    assert html_result.exit_code == 0
    assert "AI safety" in html_result.stdout
    assert "malformed manifest at " in html_result.stdout
    assert "issue_004.manifest.json" in html_result.stdout


def test_build_fleet_report_surfaces_active_issue_count_when_snapshot_and_registers_exist(tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    nova_dir = programs_root / "acme"
    edition_root = nova_dir / "archive" / "acme_weekly"
    snapshot_path = edition_root / "snapshots" / "issue_003.snapshot.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "issue_number": 3,
                "generated_at": "2026-05-08T18:00:00+00:00",
                "ado_data_as_of": "2026-05-08T18:00:00+00:00",
                "edition_type": "detailed",
                "items": [
                    {
                        "id": 900001,
                        "type": "Feature",
                        "title": "Blocked ramp readiness",
                        "state": "Blocked",
                        "assigned_to": "maintainer",
                        "area_path": "One\\Adventure\\Acme",
                        "target_date": "2026-05-25",
                        "risk_level": "high",
                        "tags": ["blocked"],
                    }
                ],
                "scorecards": [],
                "schema_version": "1.0",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    index_path = edition_root / "index.json"
    index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    index_payload["issues"][0]["snapshot_path"] = str(snapshot_path)
    index_path.write_text(json.dumps(index_payload, indent=2), encoding="utf-8")

    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=3,
            text="Need LT decision on ramp fallback.",
            entity_refs=("WI:900001",),
            ask_date=datetime(2026, 5, 9, tzinfo=timezone.utc).date(),
            owner_alias="lt",
        ),
        programs_root=programs_root,
    )
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=3,
            workstream_id="ramp",
            text="Ramp blocker closes before the fallback review.",
            entity_refs=("WI:900001",),
            claim_date=datetime(2026, 5, 9, tzinfo=timezone.utc).date(),
            owner_alias="maintainer",
            due_date=datetime(2026, 5, 12, tzinfo=timezone.utc).date(),
        ),
        programs_root=programs_root,
    )
    append_action(
        "acme",
        ActionItem(
            id="action-1",
            program_id="acme",
            text="Close blocked dependency",
            owner_alias="maintainer",
            due_date=datetime(2026, 5, 9, tzinfo=timezone.utc).date(),
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(900001,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="ramp",
            created_at=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )
    save_risk_register(
        "acme",
        (
            RiskEntry(
                id="risk-1",
                program_id="acme",
                title="Ramp fallback may miss the review",
                description="The blocked readiness item can slip the fallback plan.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.SCHEDULE,
                owner_alias="maintainer",
                mitigation_plan="Track the blocker daily until the fallback is ready.",
                mitigation_due_date=datetime(2026, 5, 12, tzinfo=timezone.utc).date(),
                linked_workstream_ids=("ramp",),
                linked_work_item_ids=(900001,),
                linked_milestone_ids=(),
                linked_claim_ids=("claim-1",),
                linked_action_ids=("action-1",),
                status=RiskStatus.OPEN,
                identified_date=datetime(2026, 5, 8, tzinfo=timezone.utc).date(),
                identified_in_vertex_issue=3,
                last_reviewed_date=datetime(2026, 5, 10, tzinfo=timezone.utc).date(),
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=programs_root,
    )
    append_signal(
        Signal(
            id="icm-1",
            timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
            source="icm/incident",
            program_id="acme",
            workstream_id="ramp",
            entity_refs=("ICM:12345",),
            text="IcM 12345: Sev2 incident active for ramp readiness.",
            raw_ref="icm:12345",
            confidence=Confidence.HIGH,
            metadata={"severity": 2},
            thread_id=None,
        ),
        programs_root=programs_root,
        partition_at=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)
    rendered = render_fleet_markdown(report)

    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.active_issue_count == 4
    assert acme.risk_register.active_count == 1
    assert acme.risk_register.stale_count == 0
    assert acme.risk_register.highlight == "Ramp fallback may miss the review — OPEN | score 9 | current | owner maintainer"
    assert len(acme.active_issue_summaries) == 2
    assert acme.active_issue_summaries[0].href == "https://dev.azure.com/your-org/One/_workitems/edit/900001"
    assert acme.active_issue_summaries[0].detail == "ado blocked | BLOCK | high confidence | owner maintainer | linked ask-1, action-1, claim-1, risk-1"
    assert "- Risk register: 1 active, 0 stale" in rendered
    assert "- Risk highlight: Ramp fallback may miss the review — OPEN | score 9 | current | owner maintainer" in rendered
    assert "- Issue highlights:" in rendered
    assert "[WI:900001 \"Blocked ramp readiness\" blocked in ADO (Blocked)](https://dev.azure.com/your-org/One/_workitems/edit/900001) — ado blocked | BLOCK | high confidence | owner maintainer | linked ask-1, action-1, claim-1, risk-1" in rendered


def test_build_fleet_report_reads_sqlite_backed_icm_signals_for_active_issue_projection(tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    _set_program_storage_backend(programs_root, program_id="acme", storage_backend="sqlite")
    nova_dir = programs_root / "acme"
    edition_root = nova_dir / "archive" / "acme_weekly"
    snapshot_path = edition_root / "snapshots" / "issue_003.snapshot.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps(
            {
                "issue_number": 3,
                "generated_at": "2026-05-08T18:00:00+00:00",
                "ado_data_as_of": "2026-05-08T18:00:00+00:00",
                "edition_type": "detailed",
                "items": [
                    {
                        "id": 900001,
                        "type": "Feature",
                        "title": "Blocked ramp readiness",
                        "state": "Blocked",
                        "assigned_to": "maintainer",
                        "area_path": "One\\Adventure\\Acme",
                        "target_date": "2026-05-25",
                        "risk_level": "high",
                        "tags": ["blocked"],
                    }
                ],
                "scorecards": [],
                "schema_version": "1.0",
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    index_path = edition_root / "index.json"
    index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    index_payload["issues"][0]["snapshot_path"] = str(snapshot_path)
    index_path.write_text(json.dumps(index_payload, indent=2), encoding="utf-8")

    append_decision_ask(
        DecisionAsk(
            id="ask-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=3,
            text="Need LT decision on ramp fallback.",
            entity_refs=("WI:900001",),
            ask_date=datetime(2026, 5, 9, tzinfo=timezone.utc).date(),
            owner_alias="lt",
        ),
        programs_root=programs_root,
    )
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=3,
            workstream_id="ramp",
            text="Ramp blocker closes before the fallback review.",
            entity_refs=("WI:900001",),
            claim_date=datetime(2026, 5, 9, tzinfo=timezone.utc).date(),
            owner_alias="maintainer",
            due_date=datetime(2026, 5, 12, tzinfo=timezone.utc).date(),
        ),
        programs_root=programs_root,
    )
    append_action(
        "acme",
        ActionItem(
            id="action-1",
            program_id="acme",
            text="Close blocked dependency",
            owner_alias="maintainer",
            due_date=datetime(2026, 5, 9, tzinfo=timezone.utc).date(),
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(900001,),
            linked_claim_id=None,
            linked_risk_id=None,
            workstream_id="ramp",
            created_at=datetime(2026, 5, 8, 18, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )
    save_risk_register(
        "acme",
        (
            RiskEntry(
                id="risk-1",
                program_id="acme",
                title="Ramp fallback may miss the review",
                description="The blocked readiness item can slip the fallback plan.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.SCHEDULE,
                owner_alias="maintainer",
                mitigation_plan="Track the blocker daily until the fallback is ready.",
                mitigation_due_date=datetime(2026, 5, 12, tzinfo=timezone.utc).date(),
                linked_workstream_ids=("ramp",),
                linked_work_item_ids=(900001,),
                linked_milestone_ids=(),
                linked_claim_ids=("claim-1",),
                linked_action_ids=("action-1",),
                status=RiskStatus.OPEN,
                identified_date=datetime(2026, 5, 8, tzinfo=timezone.utc).date(),
                identified_in_vertex_issue=3,
                last_reviewed_date=datetime(2026, 5, 10, tzinfo=timezone.utc).date(),
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=programs_root,
    )
    SQLiteSignalStore(programs_root=programs_root).append(
        Signal(
            id="icm-1",
            timestamp=datetime(2026, 5, 10, 9, 0, tzinfo=timezone.utc),
            source="icm/incident",
            program_id="acme",
            workstream_id="ramp",
            entity_refs=("ICM:12345",),
            text="IcM 12345: Sev2 incident active for ramp readiness.",
            raw_ref="icm:12345",
            confidence=Confidence.HIGH,
            metadata={"severity": 2},
            thread_id=None,
        )
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.active_issue_count == 4
    assert any(summary.title == "IcM 12345: Sev2 incident active for ramp readiness." for summary in acme.active_issue_summaries)


def _set_program_storage_backend(programs_root: Path, *, program_id: str, storage_backend: str) -> None:
    program_path = programs_root / program_id / "program.yaml"
    payload = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    payload["storage_backend"] = storage_backend
    program_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_build_fleet_report_surfaces_latest_approved_telemetry_summary(tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    analytics_signal = Signal(
        id="analytics-1",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260510:20260426:20260510",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 5,
            "completed_item_count": 2,
            "scope_delta_count": 2,
            "open_delta_count": -1,
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
        thread_id=None,
    )
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "team_member_count": 3,
            "members_with_capacity": 2,
            "total_capacity_per_day": 24.0,
            "days_off_entry_count": 1,
        },
        thread_id=None,
    )
    dismissed_signal = Signal(
        id="analytics-2",
        timestamp=datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Dismissed analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260510:20260426:20260510:dismissed",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 9,
            "completed_item_count": 9,
            "open_delta_count": -4,
        },
        thread_id=None,
    )

    for signal in (analytics_signal, sprint_signal, dismissed_signal):
        append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="analytics-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 10, 10, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 10, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="analytics-2",
            decision="dismissed",
            reviewed_at=datetime(2026, 5, 10, 12, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    acme = next(program for program in report.programs if program.program_id == "acme")
    fabrikam = next(program for program in report.programs if program.program_id == "fabrikam")
    assert acme.telemetry_summary == (
        "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; "
        "sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members, 2 with cap, 1 day off"
    )
    assert fabrikam.telemetry_summary is None
    nova_payload = next(program for program in report.to_payload()["programs"] if program["program_id"] == "acme")
    assert nova_payload["telemetry_summary"] == (
        "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; "
        "sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members, 2 with cap, 1 day off"
    )

    csv_rows = list(csv.DictReader(render_fleet_csv(report).splitlines()))
    nova_row = next(row for row in csv_rows if row["program_id"] == "acme")
    assert nova_row["telemetry_summary"] == (
        "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; "
        "sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members, 2 with cap, 1 day off"
    )

    rendered = render_fleet_markdown(report)
    assert (
        "- Telemetry: analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; "
        "sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members, 2 with cap, 1 day off"
    ) in rendered


def test_build_fleet_report_surfaces_analytics_burndown_history_in_telemetry_summary(
    tmp_path: Path,
) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    analytics_signal = Signal(
        id="analytics-1",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260510:20260426:20260510",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 5,
            "completed_item_count": 2,
            "open_delta_count": -2,
            "open_history": {
                "2026-05-08": 2,
                "2026-05-09": 1,
                "2026-05-10": 0,
            },
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
        thread_id=None,
    )

    append_signal(analytics_signal, programs_root=programs_root, partition_at=analytics_signal.timestamp)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="analytics-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 10, 10, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.telemetry_summary == (
        "analytics, 5 scope, 2 completed, open down 2, burndown 2->1->0 open, cycle 5.0d / lead 8.0d"
    )


def test_build_fleet_report_surfaces_sprint_burndown_history_in_telemetry_summary(
    tmp_path: Path,
) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "committed_item_count": 8,
            "completed_item_count": 6,
            "completion_pct": 75,
            "open_item_count": 2,
            "open_history": {
                "2026-05-08": 4,
                "2026-05-09": 3,
                "2026-05-10": 2,
            },
            "completed_history": {
                "2026-05-08": 4,
                "2026-05-09": 5,
                "2026-05-10": 6,
            },
            "recent_completion_per_business_day": 1.0,
            "recent_completion_snapshot_count": 3,
        },
        thread_id=None,
    )

    append_signal(sprint_signal, programs_root=programs_root, partition_at=sprint_signal.timestamp)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 10, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.telemetry_summary == (
        "sprint, Sprint 24, 8 committed, 6 completed, 75% complete, 2 open, burndown 4->3->2 open, completion 4->5->6 done, recent 1.0/day over 3 snapshots"
    )


def test_build_fleet_report_keeps_telemetry_summary_workstream_coherent(tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    focused_analytics_signal = Signal(
        id="analytics-1",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260510:20260426:20260510",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 5,
            "completed_item_count": 2,
            "scope_delta_count": 2,
            "open_delta_count": -1,
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
        thread_id=None,
    )
    unrelated_analytics_signal = Signal(
        id="analytics-2",
        timestamp=datetime(2026, 5, 10, 10, 30, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="platform_readiness",
        entity_refs=(),
        text="Platform Readiness: analytics summary",
        raw_ref="ado-analytics:platform_readiness:20260510:20260426:20260510",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 9,
            "completed_item_count": 4,
            "scope_delta_count": 3,
        },
        thread_id=None,
    )
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "committed_item_count": 2,
            "completed_item_count": 1,
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "elapsed_business_days": 5,
            "total_business_days": 10,
            "remaining_business_days": 5,
            "expected_completion_pct": 50,
            "pace_status": "behind",
            "pace_delta_pct": -20,
            "projection_status": "at_risk",
            "projected_completion_pct": 75,
            "observed_completion_per_business_day": 0.5,
            "required_completion_per_business_day": 1.0,
        },
        thread_id=None,
    )

    for signal in (focused_analytics_signal, unrelated_analytics_signal, sprint_signal):
        append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
    for signal_id, reviewed_at in (
        ("analytics-1", datetime(2026, 5, 10, 10, 5, tzinfo=timezone.utc)),
        ("analytics-2", datetime(2026, 5, 10, 10, 35, tzinfo=timezone.utc)),
        ("sprint-1", datetime(2026, 5, 10, 11, 5, tzinfo=timezone.utc)),
    ):
        append_review_decision(
            "acme",
            SignalReviewDecision(
                signal_id=signal_id,
                decision="approved",
                reviewed_at=reviewed_at,
                reviewed_by="system",
            ),
            programs_root=programs_root,
        )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    expected_summary = (
        "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; "
        "sprint, Sprint 24, 2 committed, 1 completed, 50% complete, 1 open, 5/10 bd elapsed, 5 bd left, pace 20pts behind 50% elapsed, ~75% by close at 0.5/day (1.0/day needed)"
    )
    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.telemetry_summary == expected_summary


def test_build_fleet_report_surfaces_three_sprint_open_average_in_telemetry_summary(
    tmp_path: Path,
) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    analytics_signal = Signal(
        id="analytics-1",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260510:20260426:20260510",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 5,
            "completed_item_count": 2,
            "scope_delta_count": 2,
            "open_delta_count": -1,
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
        thread_id=None,
    )
    older_sprint_signal = Signal(
        id="sprint-0",
        timestamp=datetime(2026, 4, 26, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: older sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-22:2026-04-26",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 22",
            "completion_pct": 20,
            "open_item_count": 5,
            "observed_completion_per_business_day": 0.1,
        },
        thread_id=None,
    )
    previous_sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 3, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: prior sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-23:2026-05-03",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 23",
            "completion_pct": 35,
            "open_item_count": 3,
            "observed_completion_per_business_day": 0.2,
        },
        thread_id=None,
    )
    current_sprint_signal = Signal(
        id="sprint-2",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "observed_completion_per_business_day": 0.3,
        },
        thread_id=None,
    )

    for signal in (analytics_signal, older_sprint_signal, previous_sprint_signal, current_sprint_signal):
        append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
    for signal_id, reviewed_at in (
        ("analytics-1", datetime(2026, 5, 10, 10, 5, tzinfo=timezone.utc)),
        ("sprint-0", datetime(2026, 4, 26, 11, 5, tzinfo=timezone.utc)),
        ("sprint-1", datetime(2026, 5, 3, 11, 5, tzinfo=timezone.utc)),
        ("sprint-2", datetime(2026, 5, 10, 11, 5, tzinfo=timezone.utc)),
    ):
        append_review_decision(
            "acme",
            SignalReviewDecision(
                signal_id=signal_id,
                decision="approved",
                reviewed_at=reviewed_at,
                reviewed_by="system",
            ),
            programs_root=programs_root,
        )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    expected_summary = (
        "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; "
        "sprint, Sprint 24, 50% complete, 1 open, 2 fewer open vs last sprint, 0.1/day faster vs last sprint, 3-sprint avg 0.2/day, throughput trend up 0.2/day over 3 sprints, 3-sprint open avg 3, open trend down 4 over 3 sprints"
    )
    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.telemetry_summary == expected_summary

    nova_payload = next(program for program in report.to_payload()["programs"] if program["program_id"] == "acme")
    assert nova_payload["telemetry_summary"] == expected_summary

    csv_rows = list(csv.DictReader(render_fleet_csv(report).splitlines()))
    nova_row = next(row for row in csv_rows if row["program_id"] == "acme")
    assert nova_row["telemetry_summary"] == expected_summary

    rendered = render_fleet_markdown(report)
    assert f"- Telemetry: {expected_summary}" in rendered


def test_build_fleet_report_surfaces_sprint_commitment_counts_in_telemetry_summary(
    tmp_path: Path,
) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    analytics_signal = Signal(
        id="analytics-1",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260510:20260426:20260510",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 5,
            "completed_item_count": 2,
            "scope_delta_count": 2,
            "open_delta_count": -1,
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
        thread_id=None,
    )
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "committed_item_count": 8,
            "completed_item_count": 4,
            "completion_pct": 50,
            "open_item_count": 4,
        },
        thread_id=None,
    )

    for signal in (analytics_signal, sprint_signal):
        append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
    for signal_id, reviewed_at in (
        ("analytics-1", datetime(2026, 5, 10, 10, 5, tzinfo=timezone.utc)),
        ("sprint-1", datetime(2026, 5, 10, 11, 5, tzinfo=timezone.utc)),
    ):
        append_review_decision(
            "acme",
            SignalReviewDecision(
                signal_id=signal_id,
                decision="approved",
                reviewed_at=reviewed_at,
                reviewed_by="system",
            ),
            programs_root=programs_root,
        )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    expected_summary = (
        "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; "
        "sprint, Sprint 24, 8 committed, 4 completed, 50% complete, 4 open"
    )
    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.telemetry_summary == expected_summary

    nova_payload = next(program for program in report.to_payload()["programs"] if program["program_id"] == "acme")
    assert nova_payload["telemetry_summary"] == expected_summary

    csv_rows = list(csv.DictReader(render_fleet_csv(report).splitlines()))
    nova_row = next(row for row in csv_rows if row["program_id"] == "acme")
    assert nova_row["telemetry_summary"] == expected_summary

    rendered = render_fleet_markdown(report)
    assert f"- Telemetry: {expected_summary}" in rendered


def test_build_fleet_report_surfaces_snapshot_backed_previous_sprint_open_comparison(
    tmp_path: Path,
) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "previous_iteration_open_item_count": 2,
        },
        thread_id=None,
    )

    append_signal(sprint_signal, programs_root=programs_root, partition_at=sprint_signal.timestamp)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 10, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.telemetry_summary == (
        "sprint, Sprint 24, 50% complete, 1 open, 1 fewer open vs last sprint"
    )


def test_build_fleet_report_surfaces_snapshot_backed_previous_sprint_throughput_comparison(
    tmp_path: Path,
) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 50,
            "open_item_count": 1,
            "recent_completion_per_business_day": 1.0,
            "recent_completion_snapshot_count": 3,
            "previous_iteration_completion_per_business_day": 0.5,
        },
        thread_id=None,
    )

    append_signal(sprint_signal, programs_root=programs_root, partition_at=sprint_signal.timestamp)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 10, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.telemetry_summary == (
        "sprint, Sprint 24, 50% complete, 1 open, recent 1.0/day over 3 snapshots, 0.5/day faster vs last sprint"
    )


def test_build_fleet_report_surfaces_snapshot_backed_three_sprint_history_summaries(
    tmp_path: Path,
) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
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
        thread_id=None,
    )

    append_signal(sprint_signal, programs_root=programs_root, partition_at=sprint_signal.timestamp)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 10, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    acme = next(program for program in report.programs if program.program_id == "acme")
    expected_summary = (
        "sprint, Sprint 24, 100% complete, 0 open, 3-sprint avg 1.0/day, 3-sprint throughput 0.5->1.0->1.5/day, "
        "throughput trend up 1.0/day over 3 sprints, 3-sprint open avg 1, 3-sprint open 2->1->0, 3-sprint burndown 3->2->2 | 3->1->1 | 3->1->0 open, 3-sprint completion 0->1->1 | 0->2->2 | 0->2->3 done, open trend down 2 over 3 sprints"
    )
    assert acme.telemetry_summary == expected_summary

    nova_payload = next(program for program in report.to_payload()["programs"] if program["program_id"] == "acme")
    assert nova_payload["telemetry_summary"] == expected_summary

    csv_rows = list(csv.DictReader(render_fleet_csv(report).splitlines()))
    nova_row = next(row for row in csv_rows if row["program_id"] == "acme")
    assert nova_row["telemetry_summary"] == expected_summary

    rendered = render_fleet_markdown(report)
    assert f"- Telemetry: {expected_summary}" in rendered


def test_build_fleet_report_surfaces_snapshot_backed_broader_historical_sprint_window(
    tmp_path: Path,
) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: current sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "completion_pct": 100,
            "open_item_count": 0,
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
        thread_id=None,
    )

    append_signal(sprint_signal, programs_root=programs_root, partition_at=sprint_signal.timestamp)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 10, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    acme = next(program for program in report.programs if program.program_id == "acme")
    expected_summary = (
        "sprint, Sprint 24, 100% complete, 0 open, 4-sprint throughput 1.0->0.5->1.0->1.5/day, 4-sprint open 1->2->1->0, 4-sprint burndown 3->2->1 | 3->2->2 | 3->1->1 | 3->1->0 open, 4-sprint completion 0->1->2 | 0->1->1 | 0->2->2 | 0->2->3 done"
    )
    assert acme.telemetry_summary == expected_summary

    nova_payload = next(program for program in report.to_payload()["programs"] if program["program_id"] == "acme")
    assert nova_payload["telemetry_summary"] == expected_summary

    csv_rows = list(csv.DictReader(render_fleet_csv(report).splitlines()))
    nova_row = next(row for row in csv_rows if row["program_id"] == "acme")
    assert nova_row["telemetry_summary"] == expected_summary

    rendered = render_fleet_markdown(report)
    assert f"- Telemetry: {expected_summary}" in rendered


def test_build_fleet_report_surfaces_sprint_projection_rate_context_in_telemetry_summary(
    tmp_path: Path,
) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    analytics_signal = Signal(
        id="analytics-1",
        timestamp=datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc),
        source="ado/analytics",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: analytics summary",
        raw_ref="ado-analytics:deployment_readiness:20260510:20260426:20260510",
        confidence=Confidence.HIGH,
        metadata={
            "snapshot_item_count": 5,
            "completed_item_count": 2,
            "scope_delta_count": 2,
            "open_delta_count": -1,
            "average_cycle_time_days": 5.0,
            "average_lead_time_days": 8.0,
        },
        thread_id=None,
    )
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "committed_item_count": 2,
            "completed_item_count": 1,
            "completion_pct": 50,
            "open_item_count": 1,
            "elapsed_business_days": 5,
            "total_business_days": 10,
            "remaining_business_days": 5,
            "expected_completion_pct": 50,
            "pace_status": "behind",
            "pace_delta_pct": -20,
            "projection_status": "at_risk",
            "projected_completion_pct": 75,
            "observed_completion_per_business_day": 0.5,
            "required_completion_per_business_day": 1.0,
        },
        thread_id=None,
    )

    for signal in (analytics_signal, sprint_signal):
        append_signal(signal, programs_root=programs_root, partition_at=signal.timestamp)
    for signal_id, reviewed_at in (
        ("analytics-1", datetime(2026, 5, 10, 10, 5, tzinfo=timezone.utc)),
        ("sprint-1", datetime(2026, 5, 10, 11, 5, tzinfo=timezone.utc)),
    ):
        append_review_decision(
            "acme",
            SignalReviewDecision(
                signal_id=signal_id,
                decision="approved",
                reviewed_at=reviewed_at,
                reviewed_by="system",
            ),
            programs_root=programs_root,
        )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    expected_summary = (
        "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; "
        "sprint, Sprint 24, 2 committed, 1 completed, 50% complete, 1 open, 5/10 bd elapsed, 5 bd left, pace 20pts behind 50% elapsed, ~75% by close at 0.5/day (1.0/day needed)"
    )
    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.telemetry_summary == expected_summary

    nova_payload = next(program for program in report.to_payload()["programs"] if program["program_id"] == "acme")
    assert nova_payload["telemetry_summary"] == expected_summary

    csv_rows = list(csv.DictReader(render_fleet_csv(report).splitlines()))
    nova_row = next(row for row in csv_rows if row["program_id"] == "acme")
    assert nova_row["telemetry_summary"] == expected_summary

    rendered = render_fleet_markdown(report)
    assert f"- Telemetry: {expected_summary}" in rendered


def test_build_fleet_report_surfaces_sprint_finish_projection_rate_context_in_telemetry_summary(
    tmp_path: Path,
) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "committed_item_count": 8,
            "completed_item_count": 6,
            "completion_pct": 75,
            "open_item_count": 2,
            "elapsed_business_days": 8,
            "total_business_days": 10,
            "remaining_business_days": 2,
            "projection_status": "finish",
            "projected_completion_pct": 100,
            "observed_completion_per_business_day": 1.0,
            "required_completion_per_business_day": 1.0,
        },
        thread_id=None,
    )

    append_signal(sprint_signal, programs_root=programs_root, partition_at=sprint_signal.timestamp)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 10, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    expected_summary = (
        "sprint, Sprint 24, 8 committed, 6 completed, 75% complete, 2 open, 8/10 bd elapsed, 2 bd left, track to finish at 1.0/day (1.0/day needed)"
    )
    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.telemetry_summary == expected_summary

    nova_payload = next(program for program in report.to_payload()["programs"] if program["program_id"] == "acme")
    assert nova_payload["telemetry_summary"] == expected_summary

    csv_rows = list(csv.DictReader(render_fleet_csv(report).splitlines()))
    nova_row = next(row for row in csv_rows if row["program_id"] == "acme")
    assert nova_row["telemetry_summary"] == expected_summary

    rendered = render_fleet_markdown(report)
    assert f"- Telemetry: {expected_summary}" in rendered


def test_build_fleet_report_surfaces_sprint_complete_projection_status_in_telemetry_summary(
    tmp_path: Path,
) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    sprint_signal = Signal(
        id="sprint-1",
        timestamp=datetime(2026, 5, 10, 11, 0, tzinfo=timezone.utc),
        source="ado/sprint",
        program_id="acme",
        workstream_id="deployment_readiness",
        entity_refs=(),
        text="Deployment Readiness: sprint summary",
        raw_ref="ado-sprint:deployment_readiness:iteration-24:2026-05-10",
        confidence=Confidence.HIGH,
        metadata={
            "iteration_name": "Sprint 24",
            "committed_item_count": 8,
            "completed_item_count": 8,
            "completion_pct": 100,
            "open_item_count": 0,
            "elapsed_business_days": 10,
            "total_business_days": 10,
            "remaining_business_days": 0,
            "projection_status": "complete",
            "projected_completion_pct": 100,
            "observed_completion_per_business_day": 1.0,
            "required_completion_per_business_day": 0.8,
        },
        thread_id=None,
    )

    append_signal(sprint_signal, programs_root=programs_root, partition_at=sprint_signal.timestamp)
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sprint-1",
            decision="approved",
            reviewed_at=datetime(2026, 5, 10, 11, 5, tzinfo=timezone.utc),
            reviewed_by="system",
        ),
        programs_root=programs_root,
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    expected_summary = (
        "sprint, Sprint 24, 8 committed, 8 completed, 100% complete, 0 open, 10/10 bd elapsed, 0 bd left, finished"
    )
    acme = next(program for program in report.programs if program.program_id == "acme")
    assert acme.telemetry_summary == expected_summary

    nova_payload = next(program for program in report.to_payload()["programs"] if program["program_id"] == "acme")
    assert nova_payload["telemetry_summary"] == expected_summary

    csv_rows = list(csv.DictReader(render_fleet_csv(report).splitlines()))
    nova_row = next(row for row in csv_rows if row["program_id"] == "acme")
    assert nova_row["telemetry_summary"] == expected_summary

    rendered = render_fleet_markdown(report)
    assert f"- Telemetry: {expected_summary}" in rendered


def test_build_fleet_report_uses_communication_plan_cadence_for_staleness(tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    _write_program(
        programs_root / "portfolio",
        name="Portfolio",
        current_phase="Monthly review",
        archive_payloads={
            "portfolio_digest": {
                "latest_issue": 2,
                "generated_at": "2026-04-25T18:00:00+00:00",
                "scorecards": [
                    {"issue_number": 1, "scorecard_name": "Portfolio", "dimension": "Readiness", "risk": "medium"},
                    {"issue_number": 2, "scorecard_name": "Portfolio", "dimension": "Readiness", "risk": "medium"},
                ],
                "top_items": (),
            },
        },
        dependencies_text=None,
    )
    portfolio_program = programs_root / "portfolio" / "program.yaml"
    portfolio_program.write_text(
        portfolio_program.read_text(encoding="utf-8")
        + "\ncommunication_plan:\n"
        + "  - edition: portfolio_digest\n"
        + "    cadence: monthly\n",
        encoding="utf-8",
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    portfolio = next(program for program in report.programs if program.program_id == "portfolio")
    assert portfolio.primary_edition == "portfolio_digest"
    assert portfolio.stale is False
    assert portfolio.staleness_reason == "current"


def test_render_fleet_markdown_handles_empty_report(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True, exist_ok=True)

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)
    rendered = render_fleet_markdown(report)

    assert rendered.startswith("# Fleet Summary")
    assert "No confirmed program archives found." in rendered


def test_build_fleet_report_surfaces_onboarding_program_without_confirmed_archive(tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    shutil.rmtree(programs_root / "fabrikam" / "archive")
    armada_program = programs_root / "fabrikam" / "program.yaml"
    armada_program.write_text(
        armada_program.read_text(encoding="utf-8")
        + "\ncommunication_plan:\n"
        + "  - edition: fabrikam_weekly\n"
        + "    cadence: weekly\n",
        encoding="utf-8",
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    assert len(report.programs) == 2
    fabrikam = next(program for program in report.programs if program.program_id == "fabrikam")
    assert fabrikam.lifecycle_state == "onboarding"
    assert fabrikam.primary_edition == "fabrikam_weekly"
    assert fabrikam.latest_issue_number is None
    assert fabrikam.latest_confirmed_at is None
    assert fabrikam.overall_risk.value == "unknown"
    assert fabrikam.stale is False
    assert fabrikam.staleness_reason == "onboarding — no confirmed issue yet"
    assert fabrikam.active_issue_count == 0
    assert fabrikam.dependency_health.inbound_count == 1
    assert any(dependency.direction == "inbound" and dependency.counterpart_program_id == "acme" for dependency in fabrikam.cross_program_dependencies)

    payload = report.to_payload()
    armada_payload = next(program for program in payload["programs"] if program["program_id"] == "fabrikam")
    assert armada_payload["lifecycle_state"] == "onboarding"
    assert armada_payload["latest_issue_number"] is None
    assert armada_payload["latest_confirmed_at"] is None

    rendered = render_fleet_markdown(report)
    assert "## Fabrikam (fabrikam)" in rendered
    assert "- State: onboarding" in rendered
    assert "- Latest confirmed: none yet" in rendered


def test_build_fleet_report_surfaces_transitive_dependency_heat(tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    _write_program(
        programs_root / "portfolio",
        name="Portfolio",
        current_phase="Coordinated rollout",
        archive_payloads={
            "portfolio_weekly": {
                "latest_issue": 2,
                "generated_at": "2026-05-09T18:00:00+00:00",
                "scorecards": [
                    {"issue_number": 2, "scorecard_name": "Portfolio", "dimension": "Rollout", "risk": "medium"},
                ],
                "top_items": (),
            },
        },
        dependencies_text=None,
    )
    (programs_root / "fabrikam" / "dependencies.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                'dependencies:',
                '  - id: fabrikam-buildouts-blocks-portfolio-rollout',
                '    from_workstream_id: buildouts',
                '    to_workstream_id: portfolio:rollout',
                '    dependency_type: blocks',
                '    risk_if_broken: Portfolio rollout cannot close while Fabrikam buildouts remain blocked.',
                '    status: active',
                '    owner_alias: fabrikam-owner',
            )
        ),
        encoding="utf-8",
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    acme = next(program for program in report.programs if program.program_id == "acme")
    assert len(acme.dependency_heat_chains) == 1
    assert acme.dependency_heat_chains[0].program_path == ("acme", "fabrikam", "portfolio")
    assert acme.dependency_heat_chains[0].broken_hop_count == 1

    rendered = render_fleet_markdown(report)
    assert "- Dependency heat:" in rendered
    assert "acme -> fabrikam -> portfolio | 2 hop(s), 1 broken" in rendered
    assert "m3-code-complete -> fabrikam:buildouts => buildouts -> portfolio:rollout" in rendered


def test_build_fleet_report_surfaces_first_confirmed_issue_without_scorecard_history(tmp_path: Path) -> None:
    programs_root = _seed_fleet_workspace(tmp_path)
    portfolio_dir = programs_root / "portfolio"
    _write_program(
        portfolio_dir,
        name="Portfolio",
        current_phase="Initial proving",
        archive_payloads={
            "portfolio_weekly": {
                "latest_issue": 1,
                "generated_at": "2026-05-09T18:00:00+00:00",
                "scorecards": [],
                "top_items": (),
            },
        },
        dependencies_text=None,
    )

    report = build_fleet_report(programs_root=programs_root, as_of=FROZEN_NOW)

    portfolio = next(program for program in report.programs if program.program_id == "portfolio")
    assert portfolio.primary_edition == "portfolio_weekly"
    assert portfolio.lifecycle_state == "active"
    assert portfolio.latest_issue_number == 1
    assert portfolio.overall_risk.value == "unknown"
    assert portfolio.staleness_reason == "current"


def test_build_active_issue_projections_reads_actions_and_risks_from_program_facts(monkeypatch) -> None:
    captured: dict[str, object] = {}
    overdue_action = ActionItem(
        id="act-1",
        program_id="acme",
        text="Follow up",
        owner_alias="operator",
        due_date=date(2026, 5, 1),
        status=ActionStatus.OPEN,
        source_signal_id=None,
        source_type=ActionSourceType.MANUAL,
        linked_work_item_ids=(),
        linked_claim_id=None,
        linked_risk_id=None,
        workstream_id=None,
        created_at=datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc),
        resolved_at=None,
        resolution_note=None,
    )
    risk_entry = RiskEntry(
        id="risk-1",
        program_id="acme",
        title="Launch gate",
        description="Blocked",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.DEPENDENCY,
        owner_alias="operator",
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=(),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 5, 1),
        identified_in_vertex_issue=None,
        last_reviewed_date=date(2026, 5, 2),
        entity_refs=(),
    )

    monkeypatch.setattr(fleet_module, "_load_snapshot_work_items", lambda latest_confirmed: ("item-1",))
    monkeypatch.setattr(fleet_module, "load_program_facts", lambda program_id, programs_root: "facts")
    monkeypatch.setattr(fleet_module, "project_action_items", lambda snapshot: (overdue_action,))
    monkeypatch.setattr(fleet_module, "project_risk_entries", lambda snapshot: (risk_entry,))
    monkeypatch.setattr(fleet_module, "build_freshness_report", lambda **kwargs: "freshness")
    monkeypatch.setattr(
        fleet_module,
        "build_signal_store_for_program_id",
        lambda program_id, programs_root: SimpleNamespace(read=lambda *_args, **_kwargs: ()),
    )
    monkeypatch.setattr(fleet_module, "load_open_decision_asks", lambda program_id, programs_root: ("ask",))
    monkeypatch.setattr(fleet_module, "load_open_claims", lambda program_id, programs_root: ("claim",))
    monkeypatch.setattr(fleet_module, "_ado_item_base_url_from_program", lambda program_document: "https://ado")

    def _capture_build_issue_projection(**kwargs):
        captured.update(kwargs)
        return ("projection",)

    monkeypatch.setattr(fleet_module, "build_issue_projection", _capture_build_issue_projection)

    result = fleet_module._build_active_issue_projections(
        program_document={"name": "Acme"},
        program_id="acme",
        latest_confirmed=SimpleNamespace(issue_number=78),
        as_of=datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc),
        programs_root=Path("Q:\\stub"),
    )

    assert result == ("projection",)
    assert captured["overdue_actions"] == (overdue_action,)
    assert captured["risk_entries"] == (risk_entry,)


def test_build_fleet_risk_register_summary_uses_program_fact_projection(monkeypatch) -> None:
    escalated = RiskEntry(
        id="risk-esc",
        program_id="acme",
        title="Escalated blocker",
        description="Escalated",
        probability=RiskProbability.LIKELY,
        impact=RiskImpact.HIGH,
        category=RiskCategory.DEPENDENCY,
        owner_alias="operator",
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=(),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.ESCALATED,
        identified_date=date(2026, 5, 1),
        identified_in_vertex_issue=None,
        last_reviewed_date=date(2026, 4, 1),
        entity_refs=(),
    )
    open_risk = RiskEntry(
        id="risk-open",
        program_id="acme",
        title="Open issue",
        description="Open",
        probability=RiskProbability.POSSIBLE,
        impact=RiskImpact.MEDIUM,
        category=RiskCategory.DEPENDENCY,
        owner_alias="operator",
        mitigation_plan=None,
        mitigation_due_date=None,
        linked_workstream_ids=(),
        linked_work_item_ids=(),
        linked_milestone_ids=(),
        linked_claim_ids=(),
        linked_action_ids=(),
        status=RiskStatus.OPEN,
        identified_date=date(2026, 5, 1),
        identified_in_vertex_issue=None,
        last_reviewed_date=date(2026, 5, 28),
        entity_refs=(),
    )
    monkeypatch.setattr(fleet_module, "load_program_facts", lambda program_id, programs_root: "facts")
    monkeypatch.setattr(fleet_module, "project_risk_entries", lambda snapshot: (open_risk, escalated))

    summary = fleet_module._build_fleet_risk_register_summary(
        program_id="acme",
        as_of=datetime(2026, 6, 3, 12, 0, tzinfo=timezone.utc),
        programs_root=Path("Q:\\stub"),
    )

    assert summary.active_count == 2
    assert summary.stale_count == 1
    assert summary.highlight is not None
    assert "Escalated blocker" in summary.highlight


def test_load_cross_program_dependencies_reads_from_program_facts(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    for program_id in ("alpha", "beta", "gamma"):
        program_dir = programs_root / program_id
        program_dir.mkdir(parents=True, exist_ok=True)
        (program_dir / "program.yaml").write_text(
            f"schema_version: '1.0'\nid: {program_id}\nname: {program_id}\n",
            encoding="utf-8",
        )
    (programs_root / "dependencies.yaml").write_text('schema_version: "1.0"\ndependencies: []\n', encoding="utf-8")

    def _load_program_facts(program_id: str, programs_root: Path):
        if program_id == "gamma":
            raise ConfigError("bad config")
        return program_id

    def _project_dependencies(snapshot: str) -> tuple[Dependency, ...]:
        if snapshot == "alpha":
            return (
                Dependency(
                    id="dep-cross",
                    from_program_id="alpha",
                    from_workstream_id="ws-source",
                    from_item_id=123,
                    from_milestone_id=None,
                    to_program_id="beta",
                    to_workstream_id="ws-target",
                    to_item_id=456,
                    to_milestone_id=None,
                    dependency_type=DependencyType.BLOCKS,
                    risk_if_broken="Slip",
                    mitigation=None,
                    status=DependencyStatus.ACTIVE,
                    owner_alias="operator",
                    resolution_path=None,
                    planned_resolution_date=None,
                    schedule_status=None,
                ),
                Dependency(
                    id="dep-local",
                    from_program_id="alpha",
                    from_workstream_id="ws-source",
                    from_item_id=123,
                    from_milestone_id=None,
                    to_program_id="alpha",
                    to_workstream_id="ws-target",
                    to_item_id=456,
                    to_milestone_id=None,
                    dependency_type=DependencyType.BLOCKS,
                    risk_if_broken="Slip",
                    mitigation=None,
                    status=DependencyStatus.ACTIVE,
                    owner_alias="operator",
                    resolution_path=None,
                    planned_resolution_date=None,
                    schedule_status=None,
                ),
            )
        return ()

    monkeypatch.setattr(fleet_module, "load_program_facts", _load_program_facts)
    monkeypatch.setattr(fleet_module, "project_dependencies", _project_dependencies)

    dependencies = fleet_module._load_cross_program_dependencies(programs_root)

    assert [dependency.id for dependency in dependencies] == ["dep-cross"]


def _seed_fleet_workspace(tmp_path: Path) -> Path:
    programs_root = tmp_path / "programs"
    programs_root.mkdir(parents=True, exist_ok=True)

    _write_program(
        programs_root / "acme",
        name="Adventure + DD on PF",
        current_phase="Ramp readiness",
        archive_payloads={
            "acme_weekly": {
                "latest_issue": 3,
                "generated_at": "2026-05-08T18:00:00+00:00",
                "scorecards": [
                    {"issue_number": 2, "scorecard_name": "Acme Readiness", "dimension": "Deployment Safety", "risk": "medium"},
                    {"issue_number": 2, "scorecard_name": "Acme Readiness", "dimension": "Networking", "risk": "medium"},
                    {"issue_number": 3, "scorecard_name": "Acme Readiness", "dimension": "Deployment Safety", "risk": "high"},
                    {"issue_number": 3, "scorecard_name": "Acme Readiness", "dimension": "Networking", "risk": "medium"},
                ],
                "top_items": (
                    "BIOS compliance remains the hard gate for ramp readiness.",
                    "Networking validation closes next week's release decision.",
                ),
            },
            "nova_daily": {
                "latest_issue": 5,
                "generated_at": "2026-05-09T08:00:00+00:00",
                "scorecards": [
                    {"issue_number": 4, "scorecard_name": "Acme Daily", "dimension": "Deployment Safety", "risk": "low"},
                    {"issue_number": 5, "scorecard_name": "Acme Daily", "dimension": "Deployment Safety", "risk": "low"},
                ],
                "top_items": (),
            },
        },
        dependencies_text=(
            'schema_version: "1.0"\n'
            'dependencies:\n'
            '  - id: acme-ramp-informs-fabrikam-buildouts\n'
            '    from_milestone_id: m3-code-complete\n'
            '    to_workstream_id: fabrikam:buildouts\n'
            '    dependency_type: informs\n'
            '    risk_if_broken: Fabrikam buildout planning stays provisional until Acme code-complete is held.\n'
            '    status: broken\n'
            '    owner_alias: maintainer\n'
        ),
    )
    _write_program(
        programs_root / "fabrikam",
        name="Fabrikam",
        current_phase="Buildout planning",
        archive_payloads={
            "fabrikam_weekly": {
                "latest_issue": 1,
                "generated_at": "2026-04-20T18:00:00+00:00",
                "scorecards": [
                    {"issue_number": 1, "scorecard_name": "Fabrikam Buildout", "dimension": "Buildouts", "risk": "medium"},
                ],
                "top_items": (),
            },
        },
        dependencies_text=None,
    )
    return programs_root


def _write_program(
    program_dir: Path,
    *,
    name: str,
    current_phase: str,
    archive_payloads: dict[str, dict[str, object]],
    dependencies_text: str | None,
) -> None:
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "\n".join(
            (
                'schema_version: "2.0"',
                f"id: {program_dir.name}",
                f"name: {name}",
                f"current_phase: {current_phase}",
                "ado:",
                "  organization: your-org",
                "  project: One",
            )
        ),
        encoding="utf-8",
    )
    if dependencies_text is not None:
        (program_dir / "dependencies.yaml").write_text(dependencies_text, encoding="utf-8")

    archive_root = program_dir / "archive"
    for edition_name, payload in archive_payloads.items():
        edition_root = archive_root / edition_name
        (edition_root / "overrides").mkdir(parents=True, exist_ok=True)
        latest_issue = int(payload["latest_issue"])
        generated_at = str(payload["generated_at"])
        (edition_root / "index.json").write_text(
            json.dumps(
                {
                    "edition": edition_name,
                    "issues": [
                        {
                            "issue_number": latest_issue,
                            "generated_at": generated_at,
                            "kind": "confirmed",
                            "html_path": None,
                            "md_path": None,
                            "snapshot_path": None,
                            "manifest_path": None,
                        }
                    ],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        (edition_root / "scorecards.json").write_text(
            json.dumps({"schema_version": "1.0", "entries": payload["scorecards"]}, indent=2),
            encoding="utf-8",
        )
        top_items = tuple(str(item) for item in payload.get("top_items", ()))
        if top_items:
            top_items_yaml = "\n".join(
                (
                    "issue_number: %03d" % latest_issue,
                    "top_3_now:",
                    *[
                        "  - type: ask\n    text: %s\n    owner: maintainer\n    ado_link: ''\n    anchor: acme" % json.dumps(item)[1:-1]
                        for item in top_items
                    ],
                )
            )
            (edition_root / "overrides" / f"issue_{latest_issue:03d}.yaml").write_text(top_items_yaml, encoding="utf-8")


def _write_ado_breaker_state(
    programs_root: Path,
    *,
    program_id: str,
    edition_name: str,
    payload: dict[str, object],
) -> None:
    breaker_path = programs_root / program_id / "publications" / edition_name / ".ado_breaker.json"
    breaker_path.parent.mkdir(parents=True, exist_ok=True)
    breaker_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_ai_safety_manifest(
    programs_root: Path,
    *,
    program_id: str,
    edition_name: str,
    issue_number: int,
    ai_safety: dict[str, object],
) -> None:
    manifest_path = programs_root / program_id / "publications" / edition_name / f"issue_{issue_number:03d}" / f"issue_{issue_number:03d}.manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "issue_number": issue_number,
                "metadata": {
                    "ai_safety": ai_safety,
                },
                "qg_results": {},
            },
            indent=2,
        ),
        encoding="utf-8",
    )
