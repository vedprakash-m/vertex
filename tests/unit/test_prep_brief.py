from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner
import yaml

from cli import app
from src.commands import prep as prep_module
from src.core.action_tracker import append_action
from src.core.assumption_tracker import save_assumptions
from src.commands.prep import generate_prep_brief
from src.core.claim_tracker import append_decision_ask
from src.core.issue_projection import IssueProjection
from src.core.journal import append_review_decision, append_signal
from src.core.models import Confidence, RiskLevel
from src.core.models_v2 import ActionItem, ActionSourceType, ActionStatus, Assumption, AssumptionStatus, DecisionAsk, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus, Signal, SignalReviewDecision, TrajectoryPoint
from src.core.risk_register_engine import save_risk_register
from src.core.sqlite_stores import SQLiteSignalStore
from src.core.trajectory import backfill_trajectory_points
from tests.unit.test_commands_report import _append_approved_v2_signal, _sample_items, _seed_v2_report_layout
from src.commands.report import generate_report_draft


runner = CliRunner()


def test_format_prep_issue_line_includes_confidence() -> None:
    line = prep_module._format_prep_issue_line(
        IssueProjection(
            work_item_id=900001,
            source_type="decision_ask",
            severity="warn",
            summary="Need LT decision on rollout guardrail timing.",
            owner_alias="maintainer",
            workstream_id="deployment_readiness",
            ado_url="https://example/900001",
            linked_entity_ids=("ask-001",),
            confidence=Confidence.HIGH,
        )
    )

    assert line == (
        "- Need LT decision on rollout guardrail timing. | decision ask | WARN | high confidence | "
        "owner maintainer | workstream deployment_readiness | linked ask-001 | [ADO](https://example/900001)"
    )


def test_generate_prep_brief_writes_satellite_markdown(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )
    programs_root = reports_root.parent / "programs"
    _patch_m3_linked_wi(programs_root, work_item_id=900001)
    program_path = programs_root / "acme" / "program.yaml"
    program_doc = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    program_doc["charter"] = {
        "scope_statement": "Deliver Acme LT-ready ramp decisions grounded in current execution risk.",
        "success_criteria": [
            "Ramp gate approved on the scheduled LT review.",
            "No unresolved Sev-1 blockers remain on the path to ramp.",
        ],
        "constraints": [
            "No new Northwind regions can be added before LT approval.",
        ],
        "stakeholder_register": [
            {
                "alias": "maintainer",
                "role": "accountable",
                "interest": "Decision-ready LT materials and blocker clarity.",
            },
        ],
    }
    program_path.write_text(yaml.safe_dump(program_doc, sort_keys=False, allow_unicode=False), encoding="utf-8")
    (programs_root / "acme" / "dependencies.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "dependencies:",
                "  - id: acme-fabrikam-buildouts",
                "    from_item_id: 900001",
                "    to_workstream_id: fabrikam:buildouts",
                "    dependency_type: informs",
                "    risk_if_broken: Fabrikam buildout planning remains provisional until Acme lands the freeze date.",
                "    mitigation: Review the dependency in the weekly cross-program checkpoint.",
                "    status: active",
                "    owner_alias: maintainer",
            )
        ),
        encoding="utf-8",
    )

    backfill_trajectory_points(
        "acme",
        900001,
        (
            TrajectoryPoint(date=date(2026, 2, 5), state="Active", assigned_to="maintainer@example.com", target_date=date(2026, 5, 1), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
            TrajectoryPoint(date=date(2026, 3, 5), state="Active", assigned_to="maintainer@example.com", target_date=date(2026, 5, 5), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
            TrajectoryPoint(date=date(2026, 4, 5), state="Active", assigned_to="maintainer@example.com", target_date=date(2026, 5, 10), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
            TrajectoryPoint(date=date(2026, 5, 1), state="Active", assigned_to="maintainer@example.com", target_date=date(2026, 5, 15), risk_level=RiskLevel.MEDIUM, area_path="One\\Adventure\\Acme\\Deployment"),
        ),
        programs_root=programs_root,
    )
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
        programs_root=programs_root,
    )
    append_action(
        "acme",
        ActionItem(
            id="action-1",
            program_id="acme",
            text="Review deployment guardrail owners",
            owner_alias="operator",
            due_date=date(2026, 5, 4),
            status=ActionStatus.OPEN,
            source_signal_id=None,
            source_type=ActionSourceType.MANUAL,
            linked_work_item_ids=(900001,),
            linked_claim_id=None,
            linked_risk_id="risk-1",
            workstream_id="deployment_readiness",
            created_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
            resolved_at=None,
            resolution_note=None,
        ),
        programs_root=programs_root,
    )
    save_assumptions(
        "acme",
        (
            Assumption(
                id="assumption-1",
                program_id="acme",
                text="Deployment telemetry will stabilize before the next LT checkpoint.",
                validation_method="Validate dashboard parity against the weekly gate review.",
                validation_due=date(2026, 5, 3),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id="risk-1",
                linked_milestone_id="m3-code-complete",
                owner_alias="operator",
                identified_date=date(2026, 4, 15),
                entity_refs=("WI:900001",),
                resolved_date=None,
            ),
        ),
        programs_root=programs_root,
    )

    draft_as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)
    generate_report_draft(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=draft_as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    recent_signal_time = datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc)
    append_signal(
        Signal(
            id="sig-001",
            timestamp=recent_signal_time,
            source="manual",
            program_id="acme",
            workstream_id="acme",
            entity_refs=("WI:900001",),
            text="Manual note after deck draft highlighted renewed deployment risk.",
            raw_ref=None,
            confidence=Confidence.HIGH,
            metadata={"author": "maintainer"},
        ),
        programs_root=programs_root,
        partition_at=recent_signal_time,
    )
    append_review_decision(
        "acme",
        SignalReviewDecision(
            signal_id="sig-001",
            decision="approved",
            reviewed_at=recent_signal_time,
            reviewed_by="maintainer",
            note=None,
        ),
        programs_root=programs_root,
    )
    append_decision_ask(
        DecisionAsk(
            id="ask-001",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            text="Need leadership decision on rollout guardrail timing.",
            entity_refs=("WI:900001",),
            ask_date=date(2026, 5, 6),
            owner_alias="maintainer",
        ),
        programs_root=programs_root,
    )
    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
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
            thread_id=None,
        ),
    )
    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
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
            thread_id=None,
        ),
    )

    generate_report_draft(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    artifacts = generate_prep_brief(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
    )

    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert artifacts.markdown_path == programs_root / "acme" / "publications" / "nova_lt_deck" / "prep_brief.md"
    assert "## Latest Draft Summary" in markdown
    assert "## Charter Context" in markdown
    assert "Scope: Deliver Acme LT-ready ramp decisions grounded in current execution risk." in markdown
    assert "- Success criterion: Ramp gate approved on the scheduled LT review." in markdown
    assert "- Constraint: No new Northwind regions can be added before LT approval." in markdown
    assert "- Stakeholder: maintainer | accountable | Decision-ready LT materials and blocker clarity." in markdown
    assert "## Telemetry" in markdown
    assert "analytics, 5 scope, 2 completed, scope up 2, open down 1, cycle 5.0d / lead 8.0d; sprint, Sprint 24, 50% complete, 1 open, team cap 24.0h/day across 3 members" in markdown
    assert "## Open Risks" in markdown
    assert "Deployment telemetry may miss the weekly gate | OPEN | score 9 | stale | owner operator" in markdown
    assert "## Active Issues" in markdown
    assert "Overdue action: Review deployment guardrail owners (due 2026-05-04) | overdue action | WARN | high confidence | owner operator | workstream deployment_readiness | linked action-1, risk-1 | [ADO](https://dev.azure.com/your-org/One/_workitems/edit/900001)" in markdown
    assert "Issue #077 ask: Need leadership decision on rollout guardrail timing. (owner maintainer) | decision ask | WARN | high confidence | owner maintainer | linked ask-001" in markdown
    assert "## Open Actions" in markdown
    assert "Review deployment guardrail owners | OPEN | due 2026-05-04 | overdue | owner operator | WI:900001 | risk risk-1" in markdown
    assert "## Dependency Cascades" in markdown
    assert "WI#900001 can impact fabrikam:buildouts: Fabrikam buildout planning remains provisional until Acme lands the freeze date. Trigger: drift on WI 900001." in markdown
    assert "## Milestone Health" in markdown
    assert "M3 - Code Complete | at risk | target 2026-05-18" in markdown
    assert "## Open Assumptions" in markdown
    assert "Deployment telemetry will stabilize before the next LT checkpoint. | UNVALIDATED | overdue | due 2026-05-03 | owner operator | method Validate dashboard parity against the weekly gate review. | milestone m3-code-complete | risk risk-1" in markdown


def test_render_prep_brief_surfaces_anticipated_question_confidence() -> None:
    markdown = prep_module._render_prep_brief(
        edition_name="nova_lt_deck",
        issue_number=1,
        generated_at=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
        draft_as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        exec_summary_text="Summary",
        charter_context_lines=(),
        telemetry_lines=("- telemetry",),
        risk_lines=("- risk",),
        issue_lines=("- issue",),
        action_lines=("- action",),
        dependency_lines=("- dependency",),
        dependency_diagram_lines=(),
        milestone_lines=("- milestone",),
        assumption_lines=("- assumption",),
        reference_doc_lines=("- reference",),
        anticipated_questions=(
            SimpleNamespace(
                reader="Jordan Lee",
                question="Why has deployment telemetry stabilization slipped 3 times?",
                suggested_response="Explain the current blocker and next checkpoint.",
                confidence=Confidence.HIGH,
            ),
        ),
        drift_patterns=(),
        recent_signals=(),
        open_decision_asks=(),
        scorecard_trends=(),
    )

    assert "## Anticipated Questions" in markdown
    assert "- Jordan Lee: Why has deployment telemetry stabilization slipped 3 times? | high confidence" in markdown
    assert "Suggested response: Explain the current blocker and next checkpoint." in markdown


def test_generate_prep_brief_surfaces_engms_reference_docs(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )
    programs_root = reports_root.parent / "programs"
    knowledge_dir = programs_root / "acme" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "engms_pages.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "pages:",
                "  - id: acme-readiness-spec",
                "    title: Acme Readiness Spec",
                "    url: https://eng.ms/acme-readiness",
                "    program_ids: [acme]",
                "    workstream_ids: [acme]",
                "    description: Canonical readiness design notes.",
            )
        ),
        encoding="utf-8",
    )

    generate_report_draft(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    artifacts = generate_prep_brief(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
    )

    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert "## Reference Docs" in markdown
    assert "Acme Readiness Spec | https://eng.ms/acme-readiness | Canonical readiness design notes." in markdown


def test_generate_prep_brief_uses_fetched_engms_summary(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )
    programs_root = reports_root.parent / "programs"
    knowledge_dir = programs_root / "acme" / "knowledge"
    knowledge_dir.mkdir(parents=True, exist_ok=True)
    (knowledge_dir / "engms_pages.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "pages:",
                "  - id: acme-readiness-spec",
                "    title: Acme Readiness Spec",
                "    url: https://eng.ms/acme-readiness",
                "    program_ids: [acme]",
                "    workstream_ids: [acme]",
                "    description: Canonical readiness design notes.",
            )
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("src.commands.prep.summarize_engms_page", lambda page: "Canonical readiness design notes. Fetched prep-context summary.")

    generate_report_draft(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        open_browser=False,
    )

    artifacts = generate_prep_brief(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc),
    )

    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert "Acme Readiness Spec | https://eng.ms/acme-readiness | Canonical readiness design notes. Fetched prep-context summary." in markdown


def test_generate_prep_brief_surfaces_pending_dependency_proposals(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )
    programs_root = reports_root.parent / "programs"
    _write_prep_dependency_proposals(programs_root)

    generate_report_draft(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    artifacts = generate_prep_brief(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
    )

    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert "## Dependency Cascades" in markdown
    assert (
        "Proposed dependency dep-proposal-1: deployment_readiness:1001 -> platform_readiness:1002 | "
        "comment_language | 2 signal(s) | medium confidence | accept via vertex dependencies accept --program acme --id dep-proposal-1"
        in markdown
    )
    assert "dep-proposal-accepted" not in markdown
    assert "### Dependency Diagram" in markdown
    assert "```mermaid" in markdown
    assert 'm3_code_complete["m3-code-complete"]' in markdown
    assert 'armada_buildouts["fabrikam:buildouts"]' in markdown
    assert "m3_code_complete -->|ACTIVE informs| armada_buildouts" in markdown


def test_generate_prep_brief_surfaces_transitive_dependency_diagram(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )
    programs_root = reports_root.parent / "programs"
    _write_prep_armada_portfolio_dependency(programs_root)

    generate_report_draft(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    artifacts = generate_prep_brief(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
    )

    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert 'portfolio_rollout["portfolio:rollout"]' in markdown
    assert "armada_buildouts -->|ACTIVE blocks| portfolio_rollout" in markdown


def test_generate_prep_brief_uses_trusted_baseline_for_previous_snapshot(
    repo_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )
    programs_root = reports_root.parent / "programs"
    as_of = datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc)

    generate_report_draft(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=as_of,
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    baseline_call: dict[str, int | None] = {}
    snapshot_call: dict[str, int | None] = {}
    original_load_previous_snapshot = prep_module._load_previous_snapshot

    def _fake_load_trusted_baseline_issue(*args, **kwargs):
        del args
        baseline_call["before_issue_number"] = kwargs.get("before_issue_number")
        return 77

    def _capturing_load_previous_snapshot(*args, **kwargs):
        snapshot_call["trusted_issue_number"] = kwargs.get("trusted_issue_number")
        return original_load_previous_snapshot(*args, **kwargs)

    monkeypatch.setattr(prep_module, "load_trusted_baseline_issue", _fake_load_trusted_baseline_issue)
    monkeypatch.setattr(prep_module, "_load_previous_snapshot", _capturing_load_previous_snapshot)

    generate_prep_brief(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=as_of,
    )

    assert baseline_call["before_issue_number"] == 1
    assert snapshot_call["trusted_issue_number"] == 77


def test_generate_prep_brief_tolerates_malformed_draft_manifest(repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )
    programs_root = reports_root.parent / "programs"

    artifacts = generate_report_draft(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    assert artifacts.manifest_path is not None
    artifacts.manifest_path.write_text("{malformed", encoding="utf-8")

    prep_artifacts = generate_prep_brief(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
    )

    markdown = prep_artifacts.markdown_path.read_text(encoding="utf-8")

    assert prep_artifacts.markdown_path.exists()
    assert "## Milestone Health" in markdown
    assert "No cached milestone assessments are available from the latest draft." in markdown


def test_load_recent_unincorporated_signals_reads_sqlite_backed_reviews(repo_root: Path, tmp_path: Path) -> None:
    reports_root, _ = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )
    programs_root = reports_root.parent / "programs"
    _set_v2_program_storage_backend(programs_root, program_id="acme", storage_backend="sqlite")
    signal_store = SQLiteSignalStore(programs_root=programs_root)
    signal_time = datetime(2026, 5, 6, 9, 0, tzinfo=timezone.utc)
    signal_store.append(
        Signal(
            id="sqlite-prep-1",
            timestamp=signal_time,
            source="manual",
            program_id="acme",
            workstream_id="deployment_readiness",
            entity_refs=("WI:900001",),
            text="SQLite-backed prep signal.",
            raw_ref="WI:900001",
            confidence=Confidence.HIGH,
            metadata={"author": "maintainer"},
        )
    )
    signal_store.append_review(
        "acme",
        SignalReviewDecision(
            signal_id="sqlite-prep-1",
            decision="approved",
            reviewed_at=signal_time,
            reviewed_by="maintainer",
            note=None,
        ),
    )

    signals = prep_module._load_recent_unincorporated_signals(
        program_id="acme",
        start=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        end=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
        programs_root=programs_root,
    )

    assert [signal.id for signal in signals] == ["sqlite-prep-1"]



def _patch_m3_linked_wi(programs_root: Path, *, work_item_id: int = 900001) -> None:
    """Patch m3-code-complete in the test workspace to reference the given WI (test fixture only)."""
    milestones_path = programs_root / "acme" / "milestones.yaml"
    if not milestones_path.exists():
        return
    data = yaml.safe_load(milestones_path.read_text(encoding="utf-8")) or {}
    for m in data.get("milestones", []):
        if m.get("id") == "m3-code-complete":
            m["linked_work_item_ids"] = [work_item_id]
    milestones_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _set_v2_program_storage_backend(programs_root: Path, *, program_id: str, storage_backend: str) -> None:
    program_path = programs_root / program_id / "program.yaml"
    program_document = yaml.safe_load(program_path.read_text(encoding="utf-8"))
    assert isinstance(program_document, dict)
    program_document["storage_backend"] = storage_backend
    program_path.write_text(yaml.safe_dump(program_document, sort_keys=False), encoding="utf-8")


def _write_prep_dependency_proposals(programs_root: Path) -> None:
    proposals_path = programs_root / "acme" / "_feedback" / "dependency_proposals.yaml"
    proposals_path.parent.mkdir(parents=True, exist_ok=True)
    proposals_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "updated_at": "2026-05-07T12:00:00+00:00",
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
                        "first_seen_at": "2026-05-05T12:00:00+00:00",
                        "last_seen_at": "2026-05-07T12:00:00+00:00",
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
                        "first_seen_at": "2026-05-01T12:00:00+00:00",
                        "last_seen_at": "2026-05-03T12:00:00+00:00",
                        "confidence": "medium",
                        "status": "accepted",
                    },
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def _write_prep_armada_portfolio_dependency(programs_root: Path) -> None:
    armada_dir = programs_root / "fabrikam"
    armada_dir.mkdir(parents=True, exist_ok=True)
    (armada_dir / "program.yaml").write_text(
        "\n".join(
            (
                'schema_version: "2.0"',
                "id: fabrikam",
                "name: Fabrikam",
            )
        ),
        encoding="utf-8",
    )
    (armada_dir / "dependencies.yaml").write_text(
        "\n".join(
            (
                'schema_version: "1.0"',
                "dependencies:",
                "  - id: fabrikam-buildouts-blocks-portfolio-rollout",
                "    from_workstream_id: buildouts",
                "    to_workstream_id: portfolio:rollout",
                "    dependency_type: blocks",
                "    risk_if_broken: Portfolio rollout cannot close while Fabrikam buildouts remain blocked.",
                "    status: active",
                "    owner_alias: fabrikam-owner",
            )
        ),
        encoding="utf-8",
    )

    portfolio_dir = programs_root / "portfolio"
    portfolio_dir.mkdir(parents=True, exist_ok=True)
    (portfolio_dir / "program.yaml").write_text(
        "\n".join(
            (
                'schema_version: "2.0"',
                "id: portfolio",
                "name: Portfolio",
            )
        ),
        encoding="utf-8",
    )


def test_generate_prep_brief_surfaces_snapshot_backed_previous_sprint_throughput_comparison(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )
    programs_root = reports_root.parent / "programs"

    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
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
            thread_id=None,
        ),
    )

    generate_report_draft(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    artifacts = generate_prep_brief(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
    )

    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert "## Telemetry" in markdown
    assert "sprint, Sprint 24, 50% complete, 1 open, recent 1.0/day over 3 snapshots, 0.5/day faster vs last sprint" in markdown


def test_generate_prep_brief_surfaces_snapshot_backed_three_sprint_history_summaries(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )
    programs_root = reports_root.parent / "programs"

    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
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
            thread_id=None,
        ),
    )

    generate_report_draft(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    artifacts = generate_prep_brief(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
    )

    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert "## Telemetry" in markdown
    assert (
        "sprint, Sprint 24, 100% complete, 0 open, 3-sprint avg 1.0/day, 3-sprint throughput 0.5->1.0->1.5/day, throughput trend up 1.0/day over 3 sprints, 3-sprint open avg 1, 3-sprint open 2->1->0, 3-sprint burndown 3->2->2 | 3->1->1 | 3->1->0 open, 3-sprint completion 0->1->1 | 0->2->2 | 0->2->3 done, open trend down 2 over 3 sprints"
        in markdown
    )


def test_generate_prep_brief_surfaces_snapshot_backed_broader_historical_sprint_window(
    repo_root: Path,
    tmp_path: Path,
) -> None:
    reports_root, archive_root = _seed_v2_report_layout(
        repo_root,
        tmp_path,
        edition_names=("acme_weekly", "nova_lt_deck"),
    )
    programs_root = reports_root.parent / "programs"

    _append_approved_v2_signal(
        programs_root,
        signal=Signal(
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
        ),
    )

    generate_report_draft(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    artifacts = generate_prep_brief(
        edition_name="nova_lt_deck",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=programs_root,
        as_of=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
    )

    markdown = artifacts.markdown_path.read_text(encoding="utf-8")

    assert "## Telemetry" in markdown
    assert (
        "sprint, Sprint 24, 100% complete, 0 open, 4-sprint throughput 1.0->0.5->1.0->1.5/day, 4-sprint open 1->2->1->0, 4-sprint burndown 3->2->1 | 3->2->2 | 3->1->1 | 3->1->0 open, 4-sprint completion 0->1->2 | 0->1->1 | 0->2->2 | 0->2->3 done"
        in markdown
    )


def test_prep_cli_rejects_non_satellite_edition(monkeypatch, repo_root: Path, tmp_path: Path) -> None:
    reports_root, archive_root = _seed_v2_report_layout(repo_root, tmp_path)

    generate_report_draft(
        edition_name="acme_weekly",
        reports_root=reports_root,
        archive_root=archive_root,
        programs_root=(tmp_path / "programs"),
        as_of=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
        work_item_loader=lambda bundle, timestamp: (_sample_items(timestamp), 0),
        kusto_query_executor=lambda query: [],
        open_browser=False,
    )

    monkeypatch.setattr("src.commands.prep.REPORTS_ROOT", reports_root)
    monkeypatch.setattr("src.commands.prep.ARCHIVE_ROOT", archive_root)

    result = runner.invoke(app, ["prep", "--edition", "acme_weekly"])

    assert result.exit_code != 0
    assert "prep is only available for V2 satellite editions." in result.stdout


def test_prep_cli_supports_json_and_csv(monkeypatch, tmp_path: Path) -> None:
    markdown_path = tmp_path / "prep_brief.md"
    monkeypatch.setattr(
        "src.commands.prep.generate_prep_brief",
        lambda edition_name: prep_module.PrepBriefArtifacts(
            issue_number=77,
            markdown_path=markdown_path,
        ),
    )

    json_result = runner.invoke(app, ["prep", "--edition", "nova_lt_deck", "--format", "json"])

    assert json_result.exit_code == 0
    payload = json.loads(json_result.stdout)
    assert payload["edition_name"] == "nova_lt_deck"
    assert payload["issue_number"] == 77
    assert payload["markdown_path"] == str(markdown_path)

    csv_result = runner.invoke(app, ["prep", "--edition", "nova_lt_deck", "--format", "csv"])

    assert csv_result.exit_code == 0
    lines = csv_result.stdout.strip().splitlines()
    assert lines[0] == "edition_name,issue_number,markdown_path"
    assert lines[1] == f"nova_lt_deck,77,{markdown_path}"

