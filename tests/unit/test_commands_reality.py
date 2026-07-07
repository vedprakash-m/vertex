from __future__ import annotations
import pytest
from pathlib import Path
pytestmark = pytest.mark.skipif(not (Path(__file__).resolve().parents[2] / "editions").exists(), reason="Requires private data")

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from cli import app
from src.core.delivery_date_evaluator import DeliveryDateSnapshot
from src.core.hypothesis_models import AssertionOperator, ChallengeState, Hypothesis, HypothesisKind, HypothesisStatus, TelemetryAssertion
from src.core.metric_models import MetricAggregation, MetricDefinition, MetricObservation, MetricQualityState, MetricSourceBinding, ObservationWindow
from src.core.reality_reconciler import reconcile_reality
from src.core.reality_store import RealityStore
from src.core.source_models import MaintenanceWindow, MetricBindingHealth


runner = CliRunner()


def test_reality_pending_review_lists_proposed_hypotheses(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.DELIVERY_DATE,
            statement="The rollout lands by the committed date.",
            expected_value="2026-05-30",
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id=None,
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.PROPOSED,
            linked_ado_item_id=12345,
        )
    )

    result = runner.invoke(
        app,
        ["reality", "pending-review", "--program", "acme", "--db-root", str(tmp_path / "db")],
    )

    assert result.exit_code == 0
    assert "Proposed hypotheses pending review: 1" in result.output
    assert "H-001 | delivery_date | The rollout lands by the committed date." in result.output


def test_reality_pending_review_interactive_accepts_and_rejects(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="We have at least 150 healthy clusters.",
            expected_value=150.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id="assertion-001",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.PROPOSED,
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-002",
            short_id="H-002",
            program_id="acme",
            kind=HypothesisKind.DELIVERY_DATE,
            statement="The rollout lands by the committed date.",
            expected_value="2026-05-30",
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id=None,
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 1, tzinfo=timezone.utc),
            status=HypothesisStatus.PROPOSED,
            linked_ado_item_id=12345,
        )
    )

    result = runner.invoke(
        app,
        [
            "reality",
            "pending-review",
            "--program",
            "acme",
            "--interactive",
            "--reviewer",
            "owner.pm",
            "--db-root",
            str(tmp_path / "db"),
        ],
        input="a\nr\nMissing source validation\n",
    )

    accepted = store.get_hypothesis("hyp-001")
    rejected = store.get_hypothesis("hyp-002")

    assert result.exit_code == 0
    assert accepted is not None
    assert accepted.status is HypothesisStatus.CONFIRMED
    assert accepted.confirmed_by == "owner.pm"
    assert rejected is not None
    assert rejected.status is HypothesisStatus.REJECTED
    assert rejected.rejection_reason == "Missing source validation"
    assert "accepted=1, rejected=1, skipped=0" in result.output


def test_reality_digest_command_renders_cached_digest(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach(store, as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc))
    reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    result = runner.invoke(
        app,
        ["reality", "digest", "--program", "acme", "--db-root", str(tmp_path / "db")],
    )

    assert result.exit_code == 0
    assert "Reality Digest - acme" in result.output
    assert "Health: red" in result.output
    assert "Open Challenges:" in result.output


def test_reality_digest_command_refreshes_when_requested(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach(store)

    result = runner.invoke(
        app,
        ["reality", "digest", "--program", "acme", "--refresh", "--db-root", str(tmp_path / "db"), "--format", "json"],
    )

    assert result.exit_code == 0
    assert '"program_id": "acme"' in result.output
    assert '"health": "red"' in result.output


def test_reality_digest_command_rolls_up_all_programs_in_json(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    for program_id in ("acme", "fabrikam"):
        program_dir = programs_root / program_id
        program_dir.mkdir(parents=True, exist_ok=True)
        (program_dir / "program.yaml").write_text(
            f"schema_version: '3.0'\nid: {program_id}\nname: {program_id.upper()}\n",
            encoding="utf-8",
        )

    nova_store = RealityStore("acme", db_root=tmp_path / "db")
    nova_store.initialize()
    _seed_threshold_breach(nova_store, as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc))
    reconcile_reality(
        store=nova_store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    armada_store = RealityStore("fabrikam", db_root=tmp_path / "db")
    armada_store.initialize()
    armada_store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="fabrikam",
            metric_id="fabrikam.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
        )
    )
    armada_store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="fabrikam",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Cluster count stays above 150.",
            expected_value=150.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id="assertion-001",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
        )
    )
    armada_store.write_metric_observation(
        MetricObservation(
            observation_id="obs-001",
            program_id="fabrikam",
            metric_id="fabrikam.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            value_num=200.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
        )
    )
    reconcile_reality(
        store=armada_store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    result = runner.invoke(
        app,
        [
            "reality",
            "digest",
            "--all-programs",
            "--format",
            "json",
            "--db-root",
            str(tmp_path / "db"),
            "--programs-root",
            str(programs_root),
        ],
    )

    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["scope"] == "all_programs"
    assert payload["program_count"] == 2
    assert payload["health"] == "red"
    assert payload["open_challenge_count"] == 1
    assert [entry["program_id"] for entry in payload["programs"]] == ["fabrikam", "acme"]


def test_reality_digest_command_rolls_up_all_programs_in_text(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True, exist_ok=True)
    (program_dir / "program.yaml").write_text(
        "schema_version: '3.0'\nid: acme\nname: Acme\n",
        encoding="utf-8",
    )

    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach(store, as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc))
    reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    result = runner.invoke(
        app,
        [
            "reality",
            "digest",
            "--all-programs",
            "--db-root",
            str(tmp_path / "db"),
            "--programs-root",
            str(programs_root),
        ],
    )

    assert result.exit_code == 0
    assert "Reality Digest Rollup - all programs" in result.output
    assert "- acme: red" in result.output


def test_reality_digest_command_marks_manual_freshness_entries(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Cluster count stays above 150.",
            expected_value=150.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id="assertion-001",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
        )
    )
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            value_num=160.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.MANUAL,
        )
    )
    reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    result = runner.invoke(
        app,
        ["reality", "digest", "--program", "acme", "--db-root", str(tmp_path / "db")],
    )

    assert result.exit_code == 0
    assert "📝 manual" in result.output


def test_reality_challenges_command_lists_open_challenges(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach(store, as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc))
    reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    result = runner.invoke(
        app,
        ["reality", "challenges", "--program", "acme", "--db-root", str(tmp_path / "db")],
    )

    assert result.exit_code == 0
    assert "Open challenges: 1" in result.output
    assert "threshold_breach" in result.output
    assert "H-001" in result.output


def test_reality_dismiss_and_reopen_commands_transition_challenge_state(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach(store, as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc))
    reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )
    challenge = store.list_open_challenges()[0]

    dismiss_result = runner.invoke(
        app,
        [
            "reality",
            "dismiss",
            "--program",
            "acme",
            "--challenge-id",
            challenge.id,
            "--reason",
            "false positive",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    dismissed = store.get_challenge(challenge.id)

    assert dismiss_result.exit_code == 0
    assert dismissed is not None
    assert dismissed.current_state == ChallengeState.DISMISSED

    reopen_result = runner.invoke(
        app,
        [
            "reality",
            "reopen",
            "--program",
            "acme",
            "--challenge-id",
            challenge.id,
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    reopened = store.get_challenge(challenge.id)

    assert reopen_result.exit_code == 0
    assert reopened is not None
    assert reopened.current_state == ChallengeState.REOPENED


def test_reality_snooze_command_updates_challenge_and_suppresses_repeat_emission(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach(store, as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc))
    reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )
    challenge = store.list_open_challenges()[0]

    result = runner.invoke(
        app,
        [
            "reality",
            "snooze",
            "--program",
            "acme",
            "--challenge-id",
            challenge.id,
            "--until",
            "2026-05-27",
            "--reason",
            "waiting on infra",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assert result.exit_code == 0
    snoozed = store.get_challenge(challenge.id)
    assert snoozed is not None
    assert snoozed.current_state == ChallengeState.SNOOZED
    assert snoozed.snooze_reason == "waiting on infra"

    second = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 21, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=0,
    )

    latest = store.get_latest_challenge_for_assertion("hyp-001", "assertion-001")
    assert second.challenges_opened == 0
    assert latest is not None
    assert latest.current_state == ChallengeState.SNOOZED


def test_reality_maintenance_schedule_command_persists_window(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()

    result = runner.invoke(
        app,
        [
            "reality",
            "maintenance",
            "schedule",
            "--program",
            "acme",
            "--title",
            "Planned metric outage",
            "--starts-at",
            "2026-05-20T09:30:00Z",
            "--ends-at",
            "2026-05-20T11:00:00Z",
            "--scope-kind",
            "metric",
            "--scope-value",
            "acme.cluster_count",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    active = store.list_active_maintenance_windows(datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc))

    assert result.exit_code == 0
    assert len(active) == 1
    assert active[0].title == "Planned metric outage"


def test_reality_digest_renders_maintenance_suppression_summary(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach(store, as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc))
    store.upsert_maintenance_window(
        MaintenanceWindow(
            id="mw-001",
            program_id="acme",
            title="Planned metric outage",
            starts_at=datetime(2026, 5, 20, 9, 30, tzinfo=timezone.utc),
            ends_at=datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc),
            scope_kind="metric",
            scope_value="acme.cluster_count",
            created_by="pm",
            created_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
        )
    )
    reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    result = runner.invoke(
        app,
        ["reality", "digest", "--program", "acme", "--db-root", str(tmp_path / "db")],
    )

    assert result.exit_code == 0
    assert "Suppressed During Maintenance:" in result.output
    assert "Planned metric outage: 1 suppressed challenge(s)" in result.output


def test_reality_digest_refresh_renders_delivery_date_challenge(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-dd-001",
            short_id="H-101",
            program_id="acme",
            kind=HypothesisKind.DELIVERY_DATE,
            statement="Pilot completes by June 1.",
            expected_value="2026-05-01",
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id=None,
            linked_ado_item_id=12345,
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
        )
    )

    with patch(
        "src.commands.reality._build_delivery_date_snapshot_provider",
        return_value=lambda work_item_id: DeliveryDateSnapshot(
            work_item_id=work_item_id,
            state="Active",
            target_date=date(2026, 5, 1),
        ),
    ):
        result = runner.invoke(
            app,
            ["reality", "digest", "--program", "acme", "--refresh", "--db-root", str(tmp_path / "db")],
        )

    assert result.exit_code == 0
    assert "delivery_date [alert] for hyp-dd-001" in result.output


def test_reality_digest_refresh_renders_staleness_challenge(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            program_id="acme",
            source_kind="kusto",
            kql_template="Metrics | summarize count()",
            result_column="Count",
            validated=True,
        )
    )
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Cluster count stays above 150.",
            expected_value=150.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id="assertion-001",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
        )
    )

    with patch(
        "src.commands.reality._build_metric_definition_map",
        return_value={
            "acme.cluster_count": MetricDefinition(
                id="acme.cluster_count",
                title="Cluster count",
                unit="count",
                aggregation=MetricAggregation.LAST,
                freshness_tier="hot",
            )
        },
    ), patch(
        "src.commands.reality._load_expected_gather_cadence_hours",
        return_value=24.0,
    ):
        result = runner.invoke(
            app,
            ["reality", "digest", "--program", "acme", "--refresh", "--db-root", str(tmp_path / "db")],
        )

    assert result.exit_code == 0
    assert "staleness [warn] for hyp-001" in result.output


def _seed_threshold_breach(store: RealityStore, *, as_of: datetime | None = None) -> None:
    _ref = (as_of or datetime.now(timezone.utc)) - timedelta(hours=1)
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            program_id="acme",
            source_kind="kusto",
            kql_template="Metrics | summarize count()",
            result_column="Count",
            validated=True,
        )
    )
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=150.0,
            tolerance_rel=0.05,
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Cluster count stays above 150.",
            expected_value=150.0,
            as_of_date=_ref.date(),
            telemetry_assertion_id="assertion-001",
            proposed_by="pm",
            proposed_at=_ref - timedelta(hours=1),
            status=HypothesisStatus.CONFIRMED,
        )
    )
    store.upsert_binding_health(
        MetricBindingHealth(
            program_id="acme",
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            last_success_at=_ref,
            last_attempt_at=_ref,
            last_successful_observation_at=_ref,
            last_failure_at=None,
            consecutive_failures=0,
            is_degraded=False,
        )
    )
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=_ref - timedelta(hours=1),
            measurement_period_end=_ref,
            observed_at=_ref,
            value_num=100.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )