from __future__ import annotations

from datetime import date
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.assumption_tracker import save_assumptions
from src.core.claim_tracker import append_claim_entry
from src.core.hypothesis_models import AssertionOperator, HypothesisKind, HypothesisStatus, TelemetryAssertion
from src.core.metric_models import MetricAggregation, ObservationWindow
from src.core.milestone_engine import save_milestones
from src.core.models_v2 import Assumption, AssumptionStatus, ClaimEntry, Milestone, MilestoneStatus
from src.core.reality_store import RealityStore
from src.core.source_models import SourceKind


runner = CliRunner()


def test_bootstrap_command_seeds_claim_and_assumption_proposals_and_is_idempotent(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    append_claim_entry(
        ClaimEntry(
            id="claim-001",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id="ws-platform",
            text="Acme will complete rollout by 2026-06-15.",
            entity_refs=("WI:1234",),
            claim_date=date(2026, 5, 20),
            owner_alias="pm",
            due_date=date(2026, 6, 15),
        ),
        programs_root=programs_root,
    )
    append_claim_entry(
        ClaimEntry(
            id="claim-002",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=78,
            workstream_id=None,
            text="Acme has enough repair capacity for the next wave.",
            entity_refs=(),
            claim_date=date(2026, 5, 21),
            owner_alias="pm",
            due_date=None,
        ),
        programs_root=programs_root,
    )
    save_assumptions(
        "acme",
        (
            Assumption(
                id="assumption-001",
                program_id="acme",
                text="Partner teams will provide daily cutover validation.",
                validation_method="Daily status review",
                validation_due=date(2026, 5, 30),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias="pm",
                identified_date=date(2026, 5, 19),
                entity_refs=(),
            ),
        ),
        programs_root=programs_root,
    )
    save_milestones(
        "acme",
        (
            Milestone(
                id="ms-001",
                program_id="acme",
                name="Wave 1 rollout",
                target_date=date(2026, 6, 15),
                owner_alias="pm",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=("Deployment complete",),
                linked_workstream_ids=("ws-platform",),
                linked_work_item_ids=(1234,),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        [
            "bootstrap",
            "--program",
            "acme",
            "--db-root",
            str(tmp_path / "db"),
            "--programs-root",
            str(programs_root),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    hypotheses = store.list_hypotheses(statuses=(HypothesisStatus.PROPOSED,))

    assert result.exit_code == 0
    assert "Bootstrap created 4 proposal(s) for acme (claims=2, assumptions=1, milestones=1)." in result.stdout
    assert len(hypotheses) == 4
    assert {hypothesis.linked_claim_id for hypothesis in hypotheses if hypothesis.linked_claim_id} == {"claim-001", "claim-002"}
    assert {hypothesis.linked_assumption_id for hypothesis in hypotheses if hypothesis.linked_assumption_id} == {"assumption-001"}
    assert any(hypothesis.kind is HypothesisKind.DELIVERY_DATE for hypothesis in hypotheses)
    assert any(
        hypothesis.kind is HypothesisKind.SCALAR_FACT and hypothesis.linked_claim_id == "claim-002"
        for hypothesis in hypotheses
    )
    assert any(hypothesis.proposed_by == "bootstrap:assumption" for hypothesis in hypotheses)
    assert any(
        hypothesis.proposed_by == "bootstrap:milestone"
        and any(source_ref.ref == "ms-001" for source_ref in hypothesis.source_refs)
        for hypothesis in hypotheses
    )

    repeat_result = runner.invoke(
        app,
        [
            "bootstrap",
            "--program",
            "acme",
            "--db-root",
            str(tmp_path / "db"),
            "--programs-root",
            str(programs_root),
        ],
    )

    assert repeat_result.exit_code == 0
    assert "No new bootstrap proposals created. Program: acme" in repeat_result.stdout
    assert len(store.list_hypotheses(statuses=(HypothesisStatus.PROPOSED,))) == 4


def test_bootstrap_command_dry_run_does_not_persist_proposals(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    append_claim_entry(
        ClaimEntry(
            id="claim-001",
            program_id="acme",
            edition_id="acme_weekly",
            issue_number=77,
            workstream_id="ws-platform",
            text="Acme will complete rollout by 2026-06-15.",
            entity_refs=("WI:1234",),
            claim_date=date(2026, 5, 20),
            owner_alias="pm",
            due_date=date(2026, 6, 15),
        ),
        programs_root=programs_root,
    )
    save_assumptions(
        "acme",
        (
            Assumption(
                id="assumption-001",
                program_id="acme",
                text="Partner teams will provide daily cutover validation.",
                validation_method="Daily status review",
                validation_due=date(2026, 5, 30),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias="pm",
                identified_date=date(2026, 5, 19),
                entity_refs=(),
            ),
        ),
        programs_root=programs_root,
    )

    result = runner.invoke(
        app,
        [
            "bootstrap",
            "--program",
            "acme",
            "--dry-run",
            "--db-root",
            str(tmp_path / "db"),
            "--programs-root",
            str(programs_root),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()

    assert result.exit_code == 0
    assert "Bootstrap dry-run would create 2 proposal(s) for acme (claims=1, assumptions=1, milestones=0)." in result.stdout
    assert store.list_hypotheses() == ()


def test_bootstrap_command_seeds_starter_assumptions_for_empty_program(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"

    result = runner.invoke(
        app,
        [
            "bootstrap",
            "--program",
            "acme",
            "--db-root",
            str(tmp_path / "db"),
            "--programs-root",
            str(programs_root),
        ],
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    assumptions_path = programs_root / "acme" / "assumptions.yaml"

    assert result.exit_code == 0
    assert "No new bootstrap proposals created. Program: acme" in result.stdout
    assert "Seeded starter assumptions template:" in result.stdout
    assert assumptions_path.exists()
    assert "# starter assumption - edit and remove this comment block when you add real entries" in assumptions_path.read_text(encoding="utf-8")
    assert store.list_hypotheses() == ()


def test_bootstrap_command_seeds_query_backed_hypothesis_for_catalog_linked_assertion(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_root = programs_root / "acme"
    program_root.mkdir(parents=True, exist_ok=True)
    (program_root / "kpis.yaml").write_text(
        """schema_version: '1.0'
kpis:
  - id: availability-latest
    label: Availability Latest
    program_ids: [acme]
    workstream_ids: [ws-platform]
    metric_id: acme.availability
    assertion_ids: [assertion-001]
""",
        encoding="utf-8",
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.availability",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=99.9,
            description="Availability should stay at or above 99.9%.",
        )
    )

    result = runner.invoke(
        app,
        [
            "bootstrap",
            "--program",
            "acme",
            "--db-root",
            str(tmp_path / "db"),
            "--programs-root",
            str(programs_root),
        ],
    )

    hypotheses = store.list_hypotheses(statuses=(HypothesisStatus.PROPOSED,))
    assertion = store.get_telemetry_assertion("assertion-001")

    assert result.exit_code == 0
    assert "Bootstrap created 1 proposal(s) for acme (claims=0, assumptions=0, milestones=0, queries=1)." in result.stdout
    assert len(hypotheses) == 1
    hypothesis = hypotheses[0]
    assert hypothesis.telemetry_assertion_id == "assertion-001"
    assert hypothesis.statement == "Availability should stay at or above 99.9%."
    assert hypothesis.expected_value == 99.9
    assert hypothesis.workstream_id == "ws-platform"
    assert hypothesis.proposed_by == "bootstrap:kpi_query"
    assert any(source_ref.kind is SourceKind.KPI_QUERY and source_ref.ref == "availability-latest" for source_ref in hypothesis.source_refs)
    assert assertion is not None
    assert assertion.linked_hypothesis_id == hypothesis.id

    repeat_result = runner.invoke(
        app,
        [
            "bootstrap",
            "--program",
            "acme",
            "--db-root",
            str(tmp_path / "db"),
            "--programs-root",
            str(programs_root),
        ],
    )

    assert repeat_result.exit_code == 0
    assert "No new bootstrap proposals created. Program: acme" in repeat_result.stdout
    assert len(store.list_hypotheses(statuses=(HypothesisStatus.PROPOSED,))) == 1


def test_bootstrap_command_renders_burn_rate_statement_for_catalog_linked_assertion(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_root = programs_root / "acme"
    program_root.mkdir(parents=True, exist_ok=True)
    (program_root / "kpis.yaml").write_text(
        """schema_version: '1.0'
kpis:
  - id: backlog-burndown
    label: Backlog Burndown
    program_ids: [acme]
    workstream_ids: [ws-platform]
    metric_id: acme.backlog_count
    assertion_ids: [assertion-001]
""",
        encoding="utf-8",
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.backlog_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.BURN_RATE_GTE,
            threshold=20.0,
        )
    )

    result = runner.invoke(
        app,
        [
            "bootstrap",
            "--program",
            "acme",
            "--db-root",
            str(tmp_path / "db"),
            "--programs-root",
            str(programs_root),
        ],
    )

    hypotheses = store.list_hypotheses(statuses=(HypothesisStatus.PROPOSED,))

    assert result.exit_code == 0
    assert len(hypotheses) == 1
    assert hypotheses[0].statement == "acme.backlog_count should burn down by at least 20 per window."