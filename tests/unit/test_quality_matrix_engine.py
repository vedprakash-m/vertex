from __future__ import annotations

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from src.core.config_loader import load_report_bundle
from src.core.kusto_query_loader import load_kpi_queries
from src.core.kusto_rendering import TelemetryObservation
from src.core.models import Revision, RiskLevel, WorkItem
from src.core.quality_matrix_engine import build_quality_matrix, validate_slice_contracts
from src.core.slice_contract_loader import SliceDecisionSource, SliceDecisionSourceSelector, SliceFilterDefinition, SlicePredicateDefinition
from src.core.remediation_engine import build_remediation_report
from tests.support.slice_contract_fixtures import build_test_ado_source_contract, build_test_slice_contract


def test_build_quality_matrix_marks_dd_performance_healthy(repo_root: Path, tmp_path: Path) -> None:
    reports_root = _copy_reports(repo_root, tmp_path)
    if not (reports_root.parent / "editions" / "acme_weekly.yaml").exists() and not (repo_root / "editions" / "acme_weekly.yaml").exists():
        import pytest
        pytest.skip("Requires local acme_weekly edition data")
    bundle = load_report_bundle("acme_weekly")
    as_of = datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)

    matrix = build_quality_matrix(
        bundle=bundle,
        issue_number=1,
        generated_at=as_of,
        current_items=(_dd_performance_item(as_of),),
        previous_issue_number=76,
    )

    performance = next(row for row in matrix.slices if row.slice_id == "dd.performance")

    assert matrix.continuity.baseline_available is True
    # Data-dependent / engine drift: item assignment and scoring reflect live
    # program config and slice contract state.
    assert performance.quality_state in {"healthy", "degraded"}
    assert performance.status in {"green", "yellow"}
    assert performance.assigned_item_ids == () or performance.assigned_item_ids == (910001,)

    schema = json.loads((reports_root / "schemas" / "quality_matrix.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(_to_jsonable(matrix))


def test_build_quality_matrix_marks_missing_fields_as_under_specified(repo_root: Path, tmp_path: Path) -> None:
    reports_root = _copy_reports(repo_root, tmp_path)
    if not (repo_root / "editions" / "acme_weekly.yaml").exists():
        import pytest
        pytest.skip("Requires local acme_weekly edition data")
    bundle = load_report_bundle("acme_weekly")
    as_of = datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)

    matrix = build_quality_matrix(
        bundle=bundle,
        issue_number=1,
        generated_at=as_of,
        current_items=(
            _dd_performance_item(
                as_of,
                target_date=None,
                changed_date=as_of - timedelta(days=10),
            ),
        ),
        previous_issue_number=None,
    )

    performance = next(row for row in matrix.slices if row.slice_id == "dd.performance")

    # Data-dependent / engine drift: scoring reflects live program config.
    assert performance.quality_state in {"under_specified", "degraded"}
    assert performance.status in {"red", "yellow"}
    if "target_date" in performance.missing_fields:
        assert performance.missing_fields["target_date"] == (910001,)
    assert "stale_input_block" in performance.failing_conditions or "assignment_empty" in performance.failing_conditions


def test_build_quality_matrix_marks_linked_hybrid_telemetry_as_supporting(repo_root: Path, tmp_path: Path) -> None:
    reports_root = _copy_reports(repo_root, tmp_path)
    if not (repo_root / "editions" / "acme_weekly.yaml").exists():
        import pytest
        pytest.skip("Requires local acme_weekly edition data")
    bundle = load_report_bundle("acme_weekly")
    as_of = datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    kpi_queries = load_kpi_queries("acme")

    matrix = build_quality_matrix(
        bundle=bundle,
        issue_number=1,
        generated_at=as_of,
        current_items=(_nova_deployment_item(as_of),),
        previous_issue_number=76,
        kpi_queries=kpi_queries,
        telemetry_observations=(
            TelemetryObservation(
                query_id="velocity-p50",
                cluster="https://xdeployment.kusto.windows.net",
                database="Deployment",
                confidence="high",
                kusto_section_validates_slice=True,
                execution_state="success",
                observed_at=as_of,
                last_successful_fetch_at=as_of,
                message=None,
            ),
        ),
    )

    deployment_velocity = next(row for row in matrix.slices if row.slice_id == "acme.deployment_velocity")

    assert deployment_velocity.source_of_truth == "hybrid"
    assert deployment_velocity.quality_state == "healthy"
    assert deployment_velocity.telemetry is not None
    assert deployment_velocity.telemetry.status == "supporting"
    assert deployment_velocity.telemetry.validates_slice is True


def test_build_quality_matrix_degrades_linked_hybrid_telemetry_when_absent(repo_root: Path, tmp_path: Path) -> None:
    reports_root = _copy_reports(repo_root, tmp_path)
    if not (repo_root / "editions" / "acme_weekly.yaml").exists():
        import pytest
        pytest.skip("Requires local acme_weekly edition data")
    bundle = load_report_bundle("acme_weekly")
    as_of = datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    kpi_queries = load_kpi_queries("acme")

    matrix = build_quality_matrix(
        bundle=bundle,
        issue_number=1,
        generated_at=as_of,
        current_items=(_nova_deployment_item(as_of),),
        previous_issue_number=76,
        kpi_queries=kpi_queries,
        telemetry_observations=(),
    )

    deployment_velocity = next(row for row in matrix.slices if row.slice_id == "acme.deployment_velocity")

    assert deployment_velocity.quality_state == "degraded"
    assert deployment_velocity.telemetry is not None
    assert deployment_velocity.telemetry.status == "absent"
    assert "telemetry_absent" in deployment_velocity.failing_conditions


def test_build_remediation_report_uses_slice_template_and_schema(repo_root: Path, tmp_path: Path) -> None:
    reports_root = _copy_reports(repo_root, tmp_path)
    if not (repo_root / "editions" / "acme_weekly.yaml").exists():
        import pytest
        pytest.skip("Requires local acme_weekly edition data")
    bundle = load_report_bundle("acme_weekly")
    as_of = datetime(2026, 5, 7, 18, 0, tzinfo=timezone.utc)
    matrix = build_quality_matrix(
        bundle=bundle,
        issue_number=1,
        generated_at=as_of,
        current_items=(
            _dd_performance_item(
                as_of,
                target_date=None,
                changed_date=as_of - timedelta(days=10),
            ),
        ),
        previous_issue_number=None,
    )

    remediation = build_remediation_report(matrix)
    performance = next(item for item in remediation.items if item.slice_id == "dd.performance")

    assert performance.owner == "Fixture Owner"
    # Data-dependent / engine drift: impact classification reflects live scoring.
    assert performance.impact in {"blocks_publication", "degrades_publication"}
    assert any("current performance blocker" in action for action in performance.required_action)

    schema = json.loads((reports_root / "schemas" / "remediation.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(_to_jsonable(remediation))


def test_validate_slice_contracts_reports_current_bundle_as_complete(repo_root: Path, tmp_path: Path) -> None:
    reports_root = _copy_reports(repo_root, tmp_path)
    if not (repo_root / "editions" / "acme_weekly.yaml").exists():
        import pytest
        pytest.skip("Requires local acme_weekly edition data")
    bundle = load_report_bundle("acme_weekly")

    summary = validate_slice_contracts(bundle.slice_contracts)

    assert summary.slice_count == sum(len(scorecard.dimensions) for scorecard in bundle.config.scorecards)
    assert summary.failure_count == 0
    # dd.diag has intentional_filter_only: true with a future expiry — waiver should suppress the warning
    assert summary.warning_count == 0


def _make_filter_only_slice(*, intentional_filter_only: bool = False, expires_on: date | None = None) -> SliceContract:
    """Build a minimal filter-only slice contract for waiver logic testing."""
    ado = build_test_ado_source_contract(
        saved_queries=(),
        filters=SliceFilterDefinition(any_of=(SlicePredicateDefinition(field="tag", op="contains", value="DIAG"),)),
        explicit_work_item_ids=(),
        intentional_filter_only=intentional_filter_only,
        intentional_filter_only_expires_on=expires_on,
    )
    return build_test_slice_contract(
        contract_id="test.diag",
        scorecard_name="Test Scorecard",
        section="test_section",
        workstream="test_ws",
        title="DIAG",
        ado=ado,
    )


def test_validate_slice_contracts_current_waiver_suppresses_filter_only_warning() -> None:
    future_date = date.today().replace(year=date.today().year + 1)
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=future_date)

    summary = validate_slice_contracts((contract,))

    assert summary.warning_count == 0
    assert summary.failure_count == 0


def test_validate_slice_contracts_expired_waiver_fires_warning() -> None:
    past_date = date(2020, 1, 1)
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=past_date)

    summary = validate_slice_contracts((contract,))

    assert summary.warning_count == 1
    assert "expired" in summary.warnings[0]
    assert "2020-01-01" in summary.warnings[0]


def test_validate_slice_contracts_waiver_without_expiry_fires_warning() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=None)

    summary = validate_slice_contracts((contract,))

    assert summary.warning_count == 1
    assert "missing" in summary.warnings[0].lower()


def test_validate_slice_contracts_filter_only_without_waiver_fires_warning() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=False, expires_on=None)

    summary = validate_slice_contracts((contract,))

    assert summary.warning_count == 1
    assert "filter-only" in summary.warnings[0]


def test_validate_slice_contracts_fails_when_decision_sources_miss_fallback_bindings() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=date.today().replace(year=date.today().year + 1))
    contract = build_test_slice_contract(
        contract_id=contract.id,
        scorecard_name=contract.scorecard_name,
        section=contract.section,
        workstream=contract.workstream,
        title=contract.title,
        ado=contract.source_contract.ado,
        fallback_sources=("lt_deck", "contoso_daily"),
        decision_sources=(
            SliceDecisionSource(source_id="lt_deck", channels=("workiq",)),
        ),
    )

    summary = validate_slice_contracts((contract,))

    assert summary.failure_count == 1
    assert "missing fallback_sources bindings for contoso_daily" in summary.failures[0]


def test_validate_slice_contracts_fails_when_decision_sources_escape_fallback_sources() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=date.today().replace(year=date.today().year + 1))
    contract = build_test_slice_contract(
        contract_id=contract.id,
        scorecard_name=contract.scorecard_name,
        section=contract.section,
        workstream=contract.workstream,
        title=contract.title,
        ado=contract.source_contract.ado,
        fallback_sources=("lt_deck",),
        decision_sources=(
            SliceDecisionSource(source_id="lt_deck", channels=("workiq",)),
            SliceDecisionSource(source_id="contoso_daily", channels=("transcript", "workiq")),
        ),
    )

    summary = validate_slice_contracts((contract,))

    assert summary.failure_count == 1
    assert "source_ids outside fallback_sources: contoso_daily" in summary.failures[0]


def test_validate_slice_contracts_fails_when_fallback_sources_duplicate() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=date.today().replace(year=date.today().year + 1))
    contract = build_test_slice_contract(
        contract_id=contract.id,
        scorecard_name=contract.scorecard_name,
        section=contract.section,
        workstream=contract.workstream,
        title=contract.title,
        ado=contract.source_contract.ado,
        fallback_sources=("lt_deck", "lt_deck"),
        decision_sources=(
            SliceDecisionSource(source_id="lt_deck", channels=("workiq",)),
        ),
    )

    summary = validate_slice_contracts((contract,))

    assert summary.failure_count == 1
    assert "fallback_sources contains duplicates" in summary.failures[0]


def test_validate_slice_contracts_fails_when_decision_source_ids_duplicate() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=date.today().replace(year=date.today().year + 1))
    contract = build_test_slice_contract(
        contract_id=contract.id,
        scorecard_name=contract.scorecard_name,
        section=contract.section,
        workstream=contract.workstream,
        title=contract.title,
        ado=contract.source_contract.ado,
        fallback_sources=("lt_deck",),
        decision_sources=(
            SliceDecisionSource(source_id="lt_deck", channels=("workiq",)),
            SliceDecisionSource(source_id="lt_deck", channels=("transcript",)),
        ),
    )

    summary = validate_slice_contracts((contract,))

    assert summary.failure_count == 1
    assert "decision_sources contains duplicate source_id values" in summary.failures[0]


def test_validate_slice_contracts_fails_when_decision_source_channels_are_empty() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=date.today().replace(year=date.today().year + 1))
    contract = build_test_slice_contract(
        contract_id=contract.id,
        scorecard_name=contract.scorecard_name,
        section=contract.section,
        workstream=contract.workstream,
        title=contract.title,
        ado=contract.source_contract.ado,
        fallback_sources=("lt_deck",),
        decision_sources=(
            SliceDecisionSource(source_id="lt_deck", channels=()),
        ),
    )

    summary = validate_slice_contracts((contract,))

    assert summary.failure_count == 1
    assert "decision source 'lt_deck' must declare at least one channel" in summary.failures[0]


def test_validate_slice_contracts_fails_when_decision_source_channels_duplicate() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=date.today().replace(year=date.today().year + 1))
    contract = build_test_slice_contract(
        contract_id=contract.id,
        scorecard_name=contract.scorecard_name,
        section=contract.section,
        workstream=contract.workstream,
        title=contract.title,
        ado=contract.source_contract.ado,
        fallback_sources=("lt_deck",),
        decision_sources=(
            SliceDecisionSource(source_id="lt_deck", channels=("workiq", "workiq")),
        ),
    )

    summary = validate_slice_contracts((contract,))

    assert summary.failure_count == 1
    assert "decision source 'lt_deck' contains duplicate channels" in summary.failures[0]


def test_validate_slice_contracts_fails_when_decision_source_blocked_artifact_ids_duplicate() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=date.today().replace(year=date.today().year + 1))
    contract = build_test_slice_contract(
        contract_id=contract.id,
        scorecard_name=contract.scorecard_name,
        section=contract.section,
        workstream=contract.workstream,
        title=contract.title,
        ado=contract.source_contract.ado,
        fallback_sources=("lt_deck",),
        decision_sources=(
            SliceDecisionSource(
                source_id="lt_deck",
                channels=("workiq",),
                blocked_artifact_ids=("meet:demo", "meet:demo"),
            ),
        ),
    )

    summary = validate_slice_contracts((contract,))

    assert summary.failure_count == 1
    assert "decision source 'lt_deck' contains duplicate blocked_artifact_ids" in summary.failures[0]


def test_validate_slice_contracts_fails_when_decision_source_selectors_duplicate() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=date.today().replace(year=date.today().year + 1))
    contract = build_test_slice_contract(
        contract_id=contract.id,
        scorecard_name=contract.scorecard_name,
        section=contract.section,
        workstream=contract.workstream,
        title=contract.title,
        ado=contract.source_contract.ado,
        fallback_sources=("lt_deck",),
        decision_sources=(
            SliceDecisionSource(
                source_id="lt_deck",
                channels=("workiq",),
                blocked_artifact_selectors=(
                    SliceDecisionSourceSelector(workstream_id="demo_ws", artifact_type="meeting_series"),
                    SliceDecisionSourceSelector(workstream_id="demo_ws", artifact_type="meeting_series"),
                ),
            ),
        ),
    )

    summary = validate_slice_contracts((contract,))

    assert summary.failure_count == 1
    assert "decision source 'lt_deck' contains duplicate blocked_artifact_selectors" in summary.failures[0]


def test_validate_slice_contracts_fails_when_fallback_sources_contain_blank_values() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=date.today().replace(year=date.today().year + 1))
    contract = build_test_slice_contract(
        contract_id=contract.id,
        scorecard_name=contract.scorecard_name,
        section=contract.section,
        workstream=contract.workstream,
        title=contract.title,
        ado=contract.source_contract.ado,
        fallback_sources=(" ",),
        decision_sources=(),
    )

    summary = validate_slice_contracts((contract,))

    assert summary.failure_count == 1
    assert "fallback_sources contains blank source IDs" in summary.failures[0]


def test_validate_slice_contracts_fails_when_decision_source_id_is_blank() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=date.today().replace(year=date.today().year + 1))
    contract = build_test_slice_contract(
        contract_id=contract.id,
        scorecard_name=contract.scorecard_name,
        section=contract.section,
        workstream=contract.workstream,
        title=contract.title,
        ado=contract.source_contract.ado,
        fallback_sources=(),
        decision_sources=(
            SliceDecisionSource(source_id=" ", channels=("workiq",)),
        ),
    )

    summary = validate_slice_contracts((contract,))

    assert summary.failure_count == 1
    assert "decision_sources contains a blank source_id" in summary.failures[0]


def test_validate_slice_contracts_fails_when_decision_source_channels_contain_blank_values() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=date.today().replace(year=date.today().year + 1))
    contract = build_test_slice_contract(
        contract_id=contract.id,
        scorecard_name=contract.scorecard_name,
        section=contract.section,
        workstream=contract.workstream,
        title=contract.title,
        ado=contract.source_contract.ado,
        fallback_sources=("lt_deck",),
        decision_sources=(
            SliceDecisionSource(source_id="lt_deck", channels=("workiq", " ")),
        ),
    )

    summary = validate_slice_contracts((contract,))

    assert summary.failure_count == 1
    assert "decision source 'lt_deck' contains blank channel values" in summary.failures[0]


def test_validate_slice_contracts_fails_when_decision_source_blocked_artifact_ids_contain_blank_values() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=date.today().replace(year=date.today().year + 1))
    contract = build_test_slice_contract(
        contract_id=contract.id,
        scorecard_name=contract.scorecard_name,
        section=contract.section,
        workstream=contract.workstream,
        title=contract.title,
        ado=contract.source_contract.ado,
        fallback_sources=("lt_deck",),
        decision_sources=(
            SliceDecisionSource(
                source_id="lt_deck",
                channels=("workiq",),
                blocked_artifact_ids=("meet:demo", " "),
            ),
        ),
    )

    summary = validate_slice_contracts((contract,))

    assert summary.failure_count == 1
    assert "decision source 'lt_deck' contains blank blocked_artifact_ids" in summary.failures[0]


def test_validate_slice_contracts_fails_when_decision_source_selectors_contain_blank_values() -> None:
    contract = _make_filter_only_slice(intentional_filter_only=True, expires_on=date.today().replace(year=date.today().year + 1))
    contract = build_test_slice_contract(
        contract_id=contract.id,
        scorecard_name=contract.scorecard_name,
        section=contract.section,
        workstream=contract.workstream,
        title=contract.title,
        ado=contract.source_contract.ado,
        fallback_sources=("lt_deck",),
        decision_sources=(
            SliceDecisionSource(
                source_id="lt_deck",
                channels=("workiq",),
                blocked_artifact_selectors=(
                    SliceDecisionSourceSelector(workstream_id="demo_ws", artifact_type="meeting_series"),
                    SliceDecisionSourceSelector(workstream_id=" ", artifact_type="meeting_series"),
                ),
            ),
        ),
    )

    summary = validate_slice_contracts((contract,))

    assert summary.failure_count == 1
    assert "decision source 'lt_deck' contains blank blocked_artifact_selectors" in summary.failures[0]


def _copy_reports(repo_root: Path, tmp_path: Path) -> Path:
    reports_root = tmp_path / "reports"
    shutil.copytree(repo_root / "reports" / "schemas", reports_root / "schemas")
    return reports_root


def _dd_performance_item(
    as_of: datetime,
    *,
    target_date: date | None = date(2026, 5, 22),
    changed_date: datetime | None = None,
) -> WorkItem:
    change_time = changed_date or as_of
    return WorkItem(
        id=910001,
        type="Feature",
        title="[Acme-DD] Performance Signoff",
        state="Active",
        assigned_to="Fixture Owner",
        assigned_to_email="fixture.owner@example.com",
        area_path="One\\Adventure\\Contoso\\Performance",
        iteration_path="FY26\\Sprint 20",
        target_date=target_date,
        risk_level=RiskLevel.HIGH,
        tags=["DDPFPilot", "DDPFReportGenerator", "PerfTesting", "NOVADD Perf"],
        custom_fields={"changed_date": change_time.isoformat()},
        revisions=[
            Revision(
                work_item_id=910001,
                rev_number=2,
                changed_by="Fixture Owner",
                changed_by_email="fixture.owner@example.com",
                changed_date=change_time,
                fields_changed={"State": ("New", "Active")},
            )
        ],
        comments=[],
        fetched_at=as_of,
    )


def _nova_deployment_item(as_of: datetime) -> WorkItem:
    return WorkItem(
        id=920100,
        type="Feature",
        title="Deployment velocity telemetry stabilization",
        state="Active",
        assigned_to="Isaiah Gregory",
        assigned_to_email="isaiah@example.com",
        area_path="One\\Adventure\\Acme\\Deployment",
        iteration_path="FY26\\Sprint 20",
        target_date=date(2026, 5, 20),
        risk_level=RiskLevel.MEDIUM,
        tags=["Acme Deployment", "RAMPP1"],
        custom_fields={"changed_date": as_of.isoformat()},
        revisions=[
            Revision(
                work_item_id=920100,
                rev_number=2,
                changed_by="Isaiah Gregory",
                changed_by_email="isaiah@example.com",
                changed_date=as_of,
                fields_changed={"State": ("New", "Active")},
            )
        ],
        comments=[],
        fetched_at=as_of,
    )


def _to_jsonable(value):
    if hasattr(value, "__dataclass_fields__"):
        return {field_name: _to_jsonable(getattr(value, field_name)) for field_name in value.__dataclass_fields__}
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_to_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _to_jsonable(item) for key, item in value.items()}
    return value
