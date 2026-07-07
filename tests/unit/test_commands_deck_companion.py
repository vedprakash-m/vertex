from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from typer.testing import CliRunner
import yaml

from cli import app
from src.commands import deck_companion
from src.commands.deck_companion import generate_deck_companion
from src.core.forecast_engine import ETAForecast
from src.commands.report import generate_report_draft
from src.core.assumption_tracker import save_assumptions
from src.core.archive_store import write_confirmed_issue
from src.core.claim_tracker import append_claim_entry
from src.core.decision_register import save_decisions
from src.core.models import Confidence
from src.core.overrides_store import OverridesDocument, Top3NowEntry, load_overrides, save_overrides
from src.core.models_v2 import Assumption, AssumptionStatus, ClaimEntry, DecisionEntry, DecisionStatus, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus, Signal
from src.core.risk_register_engine import save_risk_register
from src.core.sqlite_stores import SQLiteSignalStore
from tests.support.report_test_setup import disable_kusto_in_report_copy, stage_v2_report_workspace
from tests.unit.test_commands_report import _append_approved_v2_signal, _manifest, _sample_items, _snapshot_with_item


runner = CliRunner()
EDITION_NAME = "acme_weekly"


def test_generate_deck_companion_renders_markdown(repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)

    monkeypatch.setattr(
        "src.commands.deck_companion._load_eta_forecasts",
        lambda **kwargs: {
            900001: ETAForecast(
                work_item_id=900001,
                ado_target_date=as_of.date() + timedelta(days=5),
                predicted_target_date=as_of.date() + timedelta(days=10),
                confidence=Confidence.LOW,
                slip_probability=0.78,
                reasoning="2 prior slips in 90 days -> 78% miss probability",
                prior_slips=2,
                p50_date=as_of.date() + timedelta(days=7),
                p80_date=as_of.date() + timedelta(days=10),
                p95_date=as_of.date() + timedelta(days=13),
            )
        },
    )

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    program_path = reports_root.parent / "programs" / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["charter"] = {
        "scope_statement": "Deliver Acme ramp readiness for the current LT gate.",
        "success_criteria": [
            "Green-light LT review without timeline slip.",
        ],
        "constraints": [
            "Do not widen partner pilot scope before SCHIE signoff.",
        ],
    }
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_snapshot_with_item(as_of, risk_level=__import__("src.core.models", fromlist=["RiskLevel"]).RiskLevel.LOW),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001",
        manifest=_manifest(issue_number=1, as_of=as_of),
        archive_root=archive_root,
    )

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    overrides_document = load_overrides(EDITION_NAME, reports_root=reports_root)
    assert overrides_document is not None
    save_overrides(
        EDITION_NAME,
        OverridesDocument(
            issue_number=overrides_document.issue_number,
            top_3_now=(
                Top3NowEntry(
                    type="risk",
                    text="Fleet pilot dependency on capacity allocation",
                    owner="Vertex Maintainer",
                    ado_link="https://dev.azure.com/your-org/One/_workitems/edit/900002",
                    anchor="fleet-pilot",
                ),
            ),
            scorecards=overrides_document.scorecards,
            removed_dimensions=overrides_document.removed_dimensions,
        ),
        reports_root=reports_root,
    )
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
                decision_date=as_of.date() - __import__("datetime", fromlist=["timedelta"]).timedelta(days=15),
                status=DecisionStatus.PROPOSED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id=None,
                linked_action_ids=(),
                workstream_id="deployment_readiness",
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=reports_root.parent / "programs",
    )
    save_assumptions(
        "acme",
        (
            Assumption(
                id="assumption-1",
                program_id="acme",
                text="Partner schema contract stays stable through Q4.",
                validation_method="Validate in monthly partner review",
                validation_due=as_of.date() - timedelta(days=2),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id="m3-code-complete",
                owner_alias="operator",
                identified_date=as_of.date() - timedelta(days=30),
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=reports_root.parent / "programs",
    )
    append_claim_entry(
        ClaimEntry(
            id="claim-1",
            program_id="acme",
            edition_id=EDITION_NAME,
            issue_number=2,
            workstream_id="deployment_readiness",
            text="Deployment velocity telemetry stabilizes before the LT review.",
            entity_refs=("WI:900001",),
            claim_date=as_of.date() - timedelta(days=5),
            owner_alias="operator",
            due_date=as_of.date() + timedelta(days=7),
        ),
        programs_root=reports_root.parent / "programs",
    )
    _append_approved_v2_signal(
        reports_root.parent / "programs",
        signal=Signal(
            id="sig-deck-analytics-1",
            timestamp=datetime(2026, 5, 5, 17, 35, tzinfo=timezone.utc),
            source="ado/analytics",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Analytics snapshot for deck companion telemetry.",
            raw_ref="ado-analytics:sig-deck-analytics-1",
            confidence=Confidence.HIGH,
            metadata={
                "snapshot_item_count": 5,
                "completed_item_count": 2,
                "open_delta_count": -1,
                "average_cycle_time_days": 4.5,
                "average_lead_time_days": 7.0,
            },
        ),
    )
    _append_approved_v2_signal(
        reports_root.parent / "programs",
        signal=Signal(
            id="sig-deck-sprint-1",
            timestamp=datetime(2026, 5, 5, 17, 40, tzinfo=timezone.utc),
            source="ado/sprint",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="Sprint snapshot for deck companion telemetry.",
            raw_ref="ado-sprint:sig-deck-sprint-1",
            confidence=Confidence.HIGH,
            metadata={
                "iteration_name": "Sprint 42",
                "completion_pct": 60,
                "open_item_count": 2,
            },
        ),
    )
    save_risk_register(
        "acme",
        (
            RiskEntry(
                id="risk-1",
                program_id="acme",
                title="Deployment telemetry may miss the LT gate",
                description="The telemetry stabilization work could slip the review prep timeline.",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.TECHNICAL,
                owner_alias="operator",
                mitigation_plan="Track the telemetry fix daily until the blocker clears.",
                mitigation_due_date=as_of.date() + timedelta(days=5),
                linked_workstream_ids=("deployment_readiness",),
                linked_work_item_ids=(900001,),
                linked_milestone_ids=(),
                linked_claim_ids=("claim-1",),
                linked_action_ids=(),
                status=RiskStatus.OPEN,
                identified_date=as_of.date() - timedelta(days=7),
                identified_in_vertex_issue=1,
                last_reviewed_date=as_of.date() - timedelta(days=1),
                entity_refs=("WI:900001",),
            ),
        ),
        programs_root=reports_root.parent / "programs",
    )
    programs_root = reports_root.parent / "programs"
    _write_deck_dependency_proposals(programs_root)

    artifacts = generate_deck_companion(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
    )

    deck_markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert artifacts.markdown_path == programs_root / "acme" / "publications" / EDITION_NAME / "issue_002" / "issue_002.deck.md"
    assert "# Issue 2 — May 5, 2026" in deck_markdown
    assert "## Health" in deck_markdown
    assert "Deployment Velocity:" in deck_markdown
    assert "## Top Risks" in deck_markdown
    assert "\n---\n\n## Top Risks" in deck_markdown
    assert "Fleet pilot dependency on capacity allocation — 🔴" in deck_markdown
    assert "← #900002" in deck_markdown
    assert "## What Changed This Week" in deck_markdown
    assert "\n---\n\n## What Changed This Week" in deck_markdown
    assert "2 new items" in deck_markdown
    assert "## Data" in deck_markdown
    assert "## Telemetry" in deck_markdown
    assert "analytics, 5 scope, 2 completed, open down 1, cycle 4.5d / lead 7.0d; sprint, Sprint 42, 60% complete, 2 open (high confidence)" in deck_markdown
    assert "## Charter" in deck_markdown
    assert "\n---\n\n## Charter" in deck_markdown
    assert "Scope: Deliver Acme ramp readiness for the current LT gate." in deck_markdown
    assert "Success criterion: Green-light LT review without timeline slip." in deck_markdown
    assert "Constraint: Do not widen partner pilot scope before SCHIE signoff." in deck_markdown
    assert "## Open Issues" in deck_markdown
    assert "\n---\n\n## Open Issues" in deck_markdown
    assert "Deployment velocity telemetry stabilization" in deck_markdown
    assert "[WI:900001 \"Deployment velocity telemetry stabilization\" — Target date is within 3 business day(s).](https://dev.azure.com/your-org/One/_workitems/edit/900001)" in deck_markdown
    assert "freshness block | BLOCK | high confidence" in deck_markdown
    assert "low confidence — 2 prior slips, 78% miss probability | forecast p50 May 12, p80 May 15, p95 May 18" in deck_markdown
    assert "linked claim-1, risk-1" in deck_markdown
    assert "## Open Risks" in deck_markdown
    assert "Deployment telemetry may miss the LT gate — OPEN | score 9 | current | owner operator | mitigation due 2026-05-10 | linked workstreams deployment_readiness | claims claim-1 | Track the telemetry fix daily until the blocker clears." in deck_markdown
    assert "## Dependency Proposals" in deck_markdown
    assert "dep-proposal-1: deployment_readiness:1001 -> platform_readiness:1002 — comment_language | 2 signal(s) | medium confidence | accept via vertex dependencies accept --program acme --id dep-proposal-1" in deck_markdown
    assert "dep-proposal-accepted" not in deck_markdown
    assert "## Key Decisions" in deck_markdown
    assert "SCHIE timeline approval — Await LT approval before locking external target." in deck_markdown
    assert "PROPOSED | stale | owner lt" in deck_markdown
    assert "## Key Assumptions" in deck_markdown
    assert "Partner schema contract stays stable through Q4." in deck_markdown
    assert "UNVALIDATED | overdue | due 2026-05-03 | owner operator | milestone m3-code-complete" in deck_markdown
    assert "Source: ADO your-org/One" in deck_markdown


def _write_deck_dependency_proposals(programs_root: Path) -> None:
    proposals_path = programs_root / "acme" / "_feedback" / "dependency_proposals.yaml"
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "updated_at": "2026-05-05T18:00:00+00:00",
                "proposals": [
                    {
                        "id": "dep-proposal-1",
                        "program_id": "acme",
                        "from_workstream_id": "deployment_readiness",
                        "to_workstream_id": "platform_readiness",
                        "from_item_id": 1001,
                        "to_item_id": 1002,
                        "from_item_title": "Covered item",
                        "to_item_title": "Blocked item",
                        "suggested_dependency_type": "shares_resource",
                        "rationale": "Repeated blocked-by phrasing indicates a missing dependency.",
                        "evidence_refs": ["sig-1", "sig-2"],
                        "detection_method": "comment_language",
                        "occurrence_count": 2,
                        "first_seen_at": "2026-05-03T18:00:00+00:00",
                        "last_seen_at": "2026-05-05T18:00:00+00:00",
                        "confidence": "medium",
                        "status": "proposed",
                    },
                    {
                        "id": "dep-proposal-accepted",
                        "program_id": "acme",
                        "from_workstream_id": "deployment_readiness",
                        "to_workstream_id": "platform_readiness",
                        "from_item_id": 1003,
                        "to_item_id": 1004,
                        "from_item_title": "Already promoted item",
                        "to_item_title": "Already linked item",
                        "suggested_dependency_type": "blocks",
                        "rationale": "Already accepted.",
                        "evidence_refs": ["sig-3"],
                        "detection_method": "co_mention",
                        "occurrence_count": 3,
                        "first_seen_at": "2026-05-01T18:00:00+00:00",
                        "last_seen_at": "2026-05-02T18:00:00+00:00",
                        "confidence": "medium",
                        "status": "accepted",
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_generate_deck_companion_uses_trusted_baseline_for_previous_snapshot(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    generate_report_draft(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    baseline_call: dict[str, int | None] = {}
    snapshot_call: dict[str, int | None] = {}
    original_load_previous_snapshot = deck_companion._load_previous_snapshot

    def _fake_load_trusted_baseline_issue(*args, **kwargs):
        del args
        baseline_call["before_issue_number"] = kwargs.get("before_issue_number")
        return 77

    def _capturing_load_previous_snapshot(*args, **kwargs):
        snapshot_call["trusted_issue_number"] = kwargs.get("trusted_issue_number")
        return original_load_previous_snapshot(*args, **kwargs)

    monkeypatch.setattr(deck_companion, "load_trusted_baseline_issue", _fake_load_trusted_baseline_issue)
    monkeypatch.setattr(deck_companion, "_load_previous_snapshot", _capturing_load_previous_snapshot)

    generate_deck_companion(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
    )

    assert baseline_call["before_issue_number"] == 1
    assert snapshot_call["trusted_issue_number"] == 77


def test_generate_deck_companion_tolerates_malformed_draft_manifest(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    artifacts = generate_report_draft(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    assert artifacts.manifest_path is not None
    artifacts.manifest_path.write_text("{malformed", encoding="utf-8")

    deck_artifacts = generate_deck_companion(
        edition_name=EDITION_NAME,
        issue_number=1,
        reports_root=reports_root,
        archive_root=archive_root,
    )

    deck_markdown = deck_artifacts.markdown_path.read_text(encoding="utf-8")

    assert deck_artifacts.markdown_path.exists()
    assert "Manifest" in deck_markdown
    assert "unknown" in deck_markdown


def test_generate_deck_companion_reads_sqlite_backed_icm_signals(repo_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    disable_kusto_in_report_copy(reports_root)
    programs_root = reports_root.parent / "programs"
    _set_v2_program_storage_backend(programs_root, program_id="acme", storage_backend="sqlite")

    monkeypatch.setattr("src.commands.deck_companion._load_eta_forecasts", lambda **kwargs: {})

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    write_confirmed_issue(
        edition=EDITION_NAME,
        issue_number=1,
        snapshot=_snapshot_with_item(as_of, risk_level=__import__("src.core.models", fromlist=["RiskLevel"]).RiskLevel.LOW),
        html_body="<html><body>Issue 001</body></html>",
        markdown_body="# Issue 001",
        manifest=_manifest(issue_number=1, as_of=as_of),
        archive_root=archive_root,
    )

    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    signal_store = SQLiteSignalStore(programs_root=programs_root)
    signal_store.append(
        Signal(
            id="sig-deck-icm-1",
            timestamp=datetime(2026, 5, 5, 17, 45, tzinfo=timezone.utc),
            source="icm/incident",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("ICM:12345",),
            text="IcM 12345: Sev2 incident active for deployment readiness.",
            raw_ref="icm:12345",
            confidence=Confidence.HIGH,
            metadata={"severity": 2},
        )
    )

    artifacts = generate_deck_companion(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
    )

    deck_markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert "IcM 12345: Sev2 incident active for deployment readiness. — icm incident | BLOCK | high confidence | workstream deployment_readiness" in deck_markdown


def test_deck_companion_cli_writes_markdown(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root = stage_v2_report_workspace(repo_root, tmp_path)
    archive_root = tmp_path / "archive"
    programs_root = tmp_path / "programs"
    disable_kusto_in_report_copy(reports_root)

    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    generate_report_draft(
        edition_name=EDITION_NAME,
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    monkeypatch.setattr("src.commands.deck_companion.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.deck_companion.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["deck-companion", "--edition", EDITION_NAME])

    assert result.exit_code == 0
    assert "Deck companion generated for Issue 001." in result.stdout
    assert "Markdown:" in result.stdout
    assert (programs_root / "acme" / "publications" / EDITION_NAME / "issue_001" / "issue_001.deck.md").exists()


def test_deck_companion_cli_supports_json_and_csv(monkeypatch, tmp_path: Path) -> None:
    markdown_path = tmp_path / "issue_077.deck.md"
    monkeypatch.setattr(
        "src.commands.deck_companion.generate_deck_companion",
        lambda edition_name, issue_number=None: deck_companion.DeckCompanionArtifacts(
            issue_number=issue_number or 77,
            markdown_path=markdown_path,
        ),
    )

    json_result = runner.invoke(app, ["deck-companion", "--edition", EDITION_NAME, "--issue", "77", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["edition_name"] == EDITION_NAME
    assert payload["issue_number"] == 77
    assert payload["markdown_path"] == str(markdown_path)

    csv_result = runner.invoke(app, ["deck-companion", "--edition", EDITION_NAME, "--issue", "77", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "edition_name,issue_number,markdown_path"
    assert lines[1] == f"{EDITION_NAME},77,{markdown_path}"


def _set_v2_program_storage_backend(programs_root: Path, *, program_id: str, storage_backend: str) -> None:
    program_path = programs_root / program_id / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    program_document["storage_backend"] = storage_backend
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False, allow_unicode=False), encoding="utf-8")

