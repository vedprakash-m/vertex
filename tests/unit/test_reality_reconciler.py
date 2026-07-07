from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core.delivery_date_evaluator import DeliveryDateSnapshot
from src.core.hypothesis_models import AssertionOperator, ChallengeKind, ChallengeSeverity, ChallengeState, CompositeAssertion, CompositeAssertionOperator, Hypothesis, HypothesisKind, HypothesisStatus, TelemetryAssertion
from src.core.metric_models import MetricAggregation, MetricDefinition, MetricObservation, MetricQualityState, MetricSourceBinding, ObservationWindow
from src.core.reality_reconciler import reconcile_reality
from src.core.reality_store import RealityStore
from src.core.source_models import MaintenanceWindow, MetricBindingHealth


def test_reconcile_reality_opens_alert_challenge_and_caches_digest(tmp_path: Path) -> None:
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
            evidence_url_template="https://kusto/{metric_id}/{binding_id}",
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
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id="assertion-001",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
        )
    )
    store.upsert_binding_health(
        MetricBindingHealth(
            program_id="acme",
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            last_success_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_attempt_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_successful_observation_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
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
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            value_num=100.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    evaluations = store.list_assertion_evaluations()
    challenges = store.list_open_challenges()
    digest_row = store.read_digest_cache_row()
    updated_hypothesis = store.get_hypothesis("hyp-001")

    assert result.evaluations_written == 1
    assert result.challenges_opened == 1
    assert result.hypotheses_challenged == 1
    assert len(evaluations) == 1
    assert evaluations[0].violated is True
    assert len(challenges) == 1
    assert challenges[0].severity.value == "alert"
    assert challenges[0].evidence_url == "https://kusto/acme.cluster_count/binding-001"
    assert updated_hypothesis is not None
    assert updated_hypothesis.status == HypothesisStatus.CHALLENGED
    assert digest_row is not None
    assert str(digest_row["program_id"]) == "acme"
    assert '"health":"red"' in str(digest_row["payload_json"])


def test_reconcile_reality_populates_delta_since_last_digest(tmp_path: Path) -> None:
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
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id="assertion-001",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
        )
    )
    store.upsert_binding_health(
        MetricBindingHealth(
            program_id="acme",
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            last_success_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_attempt_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_successful_observation_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
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
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            value_num=160.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )

    first = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-002",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc),
            value_num=100.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )

    second = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 11, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    assert first.digest.delta_since_last_digest is None
    assert second.digest.delta_since_last_digest is not None
    assert second.digest.delta_since_last_digest.challenges_opened == 1
    assert second.digest.delta_since_last_digest.challenges_resolved == 0
    assert second.digest.delta_since_last_digest.hypotheses_confirmed == 0
    assert second.digest.delta_since_last_digest.hypotheses_recovered == 0


def test_reconcile_reality_blanks_unknown_evidence_url_tokens(tmp_path: Path) -> None:
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
            evidence_url_template="https://kusto/{metric_id}/{unknown_token}/{binding_id}",
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
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id="assertion-001",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
        )
    )
    store.upsert_binding_health(
        MetricBindingHealth(
            program_id="acme",
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            last_success_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_attempt_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            is_degraded=False,
            last_successful_observation_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_failure_at=None,
        )
    )
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 1, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            value_num=120.0,
            value_text=None,
            sample_count=1,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )

    reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    challenges = store.list_open_challenges()

    assert len(challenges) == 1
    assert challenges[0].evidence_url == "https://kusto/acme.cluster_count//binding-001"


def test_reconcile_reality_opens_dependency_cascade_for_dependent_hypothesis(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach_fixture(store)
    _seed_dependent_hypothesis_fixture(store, depends_on=("hyp-001",))

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=2,
    )

    challenges = store.list_open_challenges()
    dependent = store.get_hypothesis("hyp-002")
    cascade = store.get_latest_challenge_for_assertion("hyp-002", "assertion-002")

    assert result.challenges_opened == 2
    assert len(challenges) == 2
    assert cascade is not None
    assert cascade.challenge_kind is ChallengeKind.DEPENDENCY_CASCADE
    assert cascade.severity is ChallengeSeverity.ALERT
    assert "H-001 (threshold_breach)" in (cascade.note or "")
    assert dependent is not None
    assert dependent.status is HypothesisStatus.CHALLENGED


def test_reconcile_reality_resolves_dependency_cascade_after_upstream_recovery(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach_fixture(store)
    _seed_dependent_hypothesis_fixture(store, depends_on=("hyp-001",))

    reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=2,
    )

    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-002",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc),
            value_num=180.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 11, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    cascade = store.get_latest_challenge_for_assertion("hyp-002", "assertion-002")

    assert result.challenges_opened == 0
    assert len(store.list_open_challenges()) == 0
    assert cascade is not None
    assert cascade.challenge_kind is ChallengeKind.DEPENDENCY_CASCADE
    assert cascade.current_state is ChallengeState.RESOLVED


def test_reconcile_reality_opens_threshold_challenge_for_composite_and_hypothesis(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()

    _seed_threshold_breach_fixture(store)
    store.upsert_composite_assertion(
        CompositeAssertion(
            id="composite-001",
            program_id="acme",
            operator=CompositeAssertionOperator.AND,
            child_assertion_ids=("assertion-001", "assertion-002"),
            linked_hypothesis_id="hyp-composite-001",
        )
    )
    _seed_dependent_hypothesis_fixture(store, depends_on=())
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-composite-001",
            short_id="H-003",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Both readiness guards stay green.",
            expected_value=1.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id=None,
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 20, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 25, tzinfo=timezone.utc),
            composite_assertion_id="composite-001",
        )
    )

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=2,
    )

    evaluations = store.list_assertion_evaluations(composite_assertion_ids=("composite-001",))
    challenge = store.get_latest_challenge_for_composite_assertion("hyp-composite-001", "composite-001")
    hypothesis = store.get_hypothesis("hyp-composite-001")

    assert result.evaluations_written == 3
    assert result.challenges_opened == 2
    assert len(evaluations) == 1
    assert evaluations[0].assertion_id is None
    assert evaluations[0].composite_assertion_id == "composite-001"
    assert evaluations[0].violated is True
    assert challenge is not None
    assert challenge.challenge_kind is ChallengeKind.THRESHOLD_BREACH
    assert challenge.composite_assertion_id == "composite-001"
    assert challenge.severity is ChallengeSeverity.ALERT
    assert hypothesis is not None
    assert hypothesis.status is HypothesisStatus.CHALLENGED


def test_reconcile_reality_opens_data_loss_challenge_for_unvalidated_binding(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()

    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            program_id="acme",
            source_kind="kusto",
            cluster="https://cluster.kusto.windows.net",
            database="MetricsDb",
            kql_template="Metrics | summarize Count=count()",
            result_column="Count",
            validated=False,
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

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=0,
    )

    challenges = store.list_open_challenges()
    evaluations = store.list_assertion_evaluations()
    hypothesis = store.get_hypothesis("hyp-001")

    assert result.challenges_opened == 1
    assert len(challenges) == 1
    assert challenges[0].challenge_kind is ChallengeKind.DATA_LOSS
    assert challenges[0].severity is ChallengeSeverity.INFO
    assert "binding not validated" in (challenges[0].note or "")
    assert len(evaluations) == 1
    assert evaluations[0].violated is True
    assert evaluations[0].note == "binding not validated"
    assert hypothesis is not None
    assert hypothesis.status is HypothesisStatus.CONFIRMED


def test_reconcile_reality_opens_data_loss_challenge_for_degraded_binding(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()

    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            program_id="acme",
            source_kind="kusto",
            cluster="https://cluster.kusto.windows.net",
            database="MetricsDb",
            kql_template="Metrics | summarize Count=count()",
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
    store.upsert_binding_health(
        MetricBindingHealth(
            program_id="acme",
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            last_success_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_attempt_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_successful_observation_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_failure_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            consecutive_failures=3,
            is_degraded=True,
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
            value_num=100.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    challenges = store.list_open_challenges()
    evaluations = store.list_assertion_evaluations()

    assert result.challenges_opened == 1
    assert len(challenges) == 1
    assert challenges[0].challenge_kind is ChallengeKind.DATA_LOSS
    assert challenges[0].severity is ChallengeSeverity.WARN
    assert challenges[0].note == "binding degraded"
    assert len(evaluations) == 1
    assert evaluations[0].note == "binding degraded"


def test_reconcile_reality_keeps_passing_hypothesis_confirmed(tmp_path: Path) -> None:
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
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            value_num=180.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    updated_hypothesis = store.get_hypothesis("hyp-001")

    assert result.challenges_opened == 0
    assert updated_hypothesis is not None
    assert updated_hypothesis.status == HypothesisStatus.CONFIRMED
    assert result.digest.health == "green"


def test_reconcile_reality_prefers_recent_kusto_observation_over_manual(tmp_path: Path) -> None:
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
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-manual",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            value_num=300.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.MANUAL,
        )
    )
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-kusto",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            value_num=100.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    evaluations = store.list_assertion_evaluations()

    assert result.challenges_opened == 1
    assert len(evaluations) == 1
    assert evaluations[0].value_num == 100.0


def test_reconcile_reality_prefers_pinned_manual_observation_over_recent_kusto(tmp_path: Path) -> None:
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
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-manual",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            value_num=300.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.MANUAL,
            is_pinned=True,
            pinned_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
            pin_reason="PM confirmed telemetry lag",
        )
    )
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-kusto",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            value_num=100.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    evaluations = store.list_assertion_evaluations()

    assert result.challenges_opened == 0
    assert len(evaluations) == 1
    assert evaluations[0].value_num == 300.0



def test_reconcile_reality_does_not_duplicate_open_challenge_on_repeat_violation(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach_fixture(store)

    first = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )
    second = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 11, 5, tzinfo=timezone.utc),
        l1_observations_written=0,
    )

    challenges = store.list_open_challenges()
    evaluations = store.list_assertion_evaluations()

    assert first.challenges_opened == 1
    assert second.challenges_opened == 0
    assert len(challenges) == 1
    assert len(evaluations) == 2


def test_reconcile_reality_resolves_open_challenge_after_recovery(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach_fixture(store)

    first = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-002",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc),
            value_num=180.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )

    second = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 11, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    assert first.challenges_opened == 1
    assert second.challenges_opened == 0
    assert len(store.list_open_challenges()) == 0

    resolved = store.get_latest_challenge_for_assertion("hyp-001", "assertion-001")

    assert resolved is not None
    assert resolved.current_state == ChallengeState.RESOLVED


def test_reconcile_reality_honors_cooldown_after_dismissal(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach_fixture(store)

    first = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )
    original = store.list_open_challenges()[0]
    store.update_challenge_state(
        original.id,
        ChallengeState.DISMISSED,
        datetime(2026, 5, 20, 10, 10, tzinfo=timezone.utc),
        reason="pm_reinstated",
    )

    second = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 11, 5, tzinfo=timezone.utc),
        l1_observations_written=0,
    )

    latest = store.get_latest_challenge_for_assertion("hyp-001", "assertion-001")
    evaluations = store.list_assertion_evaluations()

    assert first.challenges_opened == 1
    assert second.challenges_opened == 0
    assert latest is not None
    assert latest.id == original.id
    assert latest.current_state == ChallengeState.DISMISSED
    assert len(evaluations) == 2


def test_reconcile_reality_reopens_after_cooldown_window_expires(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach_fixture(store)

    reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )
    original = store.list_open_challenges()[0]
    store.update_challenge_state(
        original.id,
        ChallengeState.DISMISSED,
        datetime(2026, 5, 20, 10, 10, tzinfo=timezone.utc),
        reason="pm_reinstated",
    )

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 21, 10, 11, tzinfo=timezone.utc),
        l1_observations_written=0,
    )

    latest = store.get_latest_challenge_for_assertion("hyp-001", "assertion-001")
    prior = store.get_challenge(original.id)

    assert result.challenges_opened == 1
    assert latest is not None
    assert latest.id != original.id
    assert latest.current_state == ChallengeState.OPEN
    assert prior is not None
    assert prior.current_state == ChallengeState.DISMISSED


def test_reconcile_reality_reopens_after_snooze_expiry(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach_fixture(store)

    first = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )
    original = store.list_open_challenges()[0]
    store.update_challenge_state(
        original.id,
        ChallengeState.SNOOZED,
        datetime(2026, 5, 20, 10, 10, tzinfo=timezone.utc),
        reason="waiting on infra",
        snoozed_until=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        snooze_reason="waiting on infra",
    )

    second = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 12, 5, tzinfo=timezone.utc),
        l1_observations_written=0,
    )

    latest = store.get_latest_challenge_for_assertion("hyp-001", "assertion-001")
    prior = store.get_challenge(original.id)

    assert first.challenges_opened == 1
    assert second.challenges_opened == 1
    assert latest is not None
    assert latest.id != original.id
    assert latest.current_state == ChallengeState.OPEN
    assert prior is not None
    assert prior.current_state == ChallengeState.RESOLVED


def test_reconcile_reality_suppresses_threshold_breach_during_maintenance_window(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_breach_fixture(store)
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

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=1,
    )

    evaluations = store.list_assertion_evaluations()

    assert result.challenges_opened == 0
    assert len(store.list_open_challenges()) == 0
    assert len(evaluations) == 1
    assert evaluations[0].note == "suppressed_by_maintenance:mw-001"
    assert len(result.digest.suppressed_during_maintenance) == 1
    assert result.digest.suppressed_during_maintenance[0].suppressed_count == 1


def test_reconcile_reality_opens_delivery_date_challenge_for_overdue_open_work_item(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-dd-001",
            short_id="H-101",
            program_id="acme",
            kind=HypothesisKind.DELIVERY_DATE,
            statement="Pilot completes by June 1.",
            expected_value="2026-06-01",
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id=None,
            linked_ado_item_id=12345,
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 1, 9, 5, tzinfo=timezone.utc),
        )
    )

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
        l1_observations_written=0,
        delivery_date_snapshot_provider=lambda work_item_id: DeliveryDateSnapshot(
            work_item_id=work_item_id,
            state="Active",
            target_date=date(2026, 6, 1),
        ),
    )

    challenges = store.list_open_challenges()
    updated_hypothesis = store.get_hypothesis("hyp-dd-001")

    assert result.challenges_opened == 1
    assert len(challenges) == 1
    assert challenges[0].challenge_kind == ChallengeKind.DELIVERY_DATE
    assert challenges[0].severity.value == "alert"
    assert challenges[0].ado_current_target == "2026-06-01"
    assert updated_hypothesis is not None
    assert updated_hypothesis.status == HypothesisStatus.CHALLENGED


def test_reconcile_reality_marks_hypothesis_stale_when_observation_is_too_old(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_monitoring_fixture(store)
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-stale",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 18, 8, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc),
            value_num=180.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=0,
        metric_definitions_by_id={
            "acme.cluster_count": MetricDefinition(
                id="acme.cluster_count",
                title="Cluster count",
                unit="count",
                aggregation=MetricAggregation.LAST,
                freshness_tier="hot",
            )
        },
        expected_gather_cadence_hours=24.0,
    )

    challenges = store.list_open_challenges()
    updated_hypothesis = store.get_hypothesis("hyp-001")

    assert result.challenges_opened == 1
    assert len(challenges) == 1
    assert challenges[0].challenge_kind == ChallengeKind.STALENESS
    assert challenges[0].severity.value == "warn"
    assert updated_hypothesis is not None
    assert updated_hypothesis.status == HypothesisStatus.STALE
    assert result.digest.stale_count == 1


def test_reconcile_reality_resolves_staleness_when_fresh_observation_arrives(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    _seed_threshold_monitoring_fixture(store)
    stale_definition = {
        "acme.cluster_count": MetricDefinition(
            id="acme.cluster_count",
            title="Cluster count",
            unit="count",
            aggregation=MetricAggregation.LAST,
            freshness_tier="hot",
        )
    }
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-stale",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 18, 8, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 18, 9, 0, tzinfo=timezone.utc),
            value_num=180.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )
    reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 5, tzinfo=timezone.utc),
        l1_observations_written=0,
        metric_definitions_by_id=stale_definition,
        expected_gather_cadence_hours=24.0,
    )
    original = store.list_open_challenges()[0]
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-fresh",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            value_num=180.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 5, 20, 10, 35, tzinfo=timezone.utc),
        l1_observations_written=1,
        metric_definitions_by_id=stale_definition,
        expected_gather_cadence_hours=24.0,
    )

    resolved = store.get_challenge(original.id)
    updated_hypothesis = store.get_hypothesis("hyp-001")

    assert result.challenges_opened == 0
    assert len(store.list_open_challenges()) == 0
    assert resolved is not None
    assert resolved.current_state == ChallengeState.RESOLVED
    assert updated_hypothesis is not None
    assert updated_hypothesis.status == HypothesisStatus.CONFIRMED


def test_reconcile_reality_resolves_delivery_date_challenge_when_item_closes(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-dd-001",
            short_id="H-101",
            program_id="acme",
            kind=HypothesisKind.DELIVERY_DATE,
            statement="Pilot completes by June 1.",
            expected_value="2026-06-01",
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id=None,
            linked_ado_item_id=12345,
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
        )
    )

    reconcile_reality(
        store=store,
        as_of=datetime(2026, 6, 10, 10, 0, tzinfo=timezone.utc),
        l1_observations_written=0,
        delivery_date_snapshot_provider=lambda work_item_id: DeliveryDateSnapshot(
            work_item_id=work_item_id,
            state="Active",
            target_date=date(2026, 6, 1),
        ),
    )
    original = store.list_open_challenges()[0]

    result = reconcile_reality(
        store=store,
        as_of=datetime(2026, 6, 11, 10, 0, tzinfo=timezone.utc),
        l1_observations_written=0,
        delivery_date_snapshot_provider=lambda work_item_id: DeliveryDateSnapshot(
            work_item_id=work_item_id,
            state="Closed",
            target_date=date(2026, 6, 1),
            closed_at=datetime(2026, 6, 10, 18, 0, tzinfo=timezone.utc),
        ),
    )

    resolved = store.get_challenge(original.id)

    assert result.challenges_opened == 0
    assert len(store.list_open_challenges()) == 0
    assert resolved is not None
    assert resolved.challenge_kind == ChallengeKind.DELIVERY_DATE
    assert resolved.current_state == ChallengeState.RESOLVED


def _seed_threshold_breach_fixture(store: RealityStore) -> None:
    _seed_threshold_monitoring_fixture(store)
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            value_num=100.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-001",
            binding_version=1,
        )
    )


def _seed_threshold_monitoring_fixture(store: RealityStore) -> None:
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            program_id="acme",
            source_kind="kusto",
            kql_template="Metrics | summarize count()",
            result_column="Count",
            evidence_url_template="https://kusto/{metric_id}/{binding_id}",
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
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id="assertion-001",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
        )
    )
    store.upsert_binding_health(
        MetricBindingHealth(
            program_id="acme",
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            last_success_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_attempt_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_successful_observation_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_failure_at=None,
            consecutive_failures=0,
            is_degraded=False,
        )
    )


def _seed_dependent_hypothesis_fixture(store: RealityStore, *, depends_on: tuple[str, ...]) -> None:
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-002",
            metric_id="acme.capacity_margin",
            program_id="acme",
            source_kind="kusto",
            kql_template="Metrics | summarize avg(capacity_margin)",
            result_column="CapacityMargin",
            validated=True,
        )
    )
    store.upsert_telemetry_assertion(
        TelemetryAssertion(
            id="assertion-002",
            program_id="acme",
            metric_id="acme.capacity_margin",
            window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
            operator=AssertionOperator.GTE,
            threshold=10.0,
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-002",
            short_id="H-002",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Capacity margin stays above 10.",
            expected_value=10.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id="assertion-002",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 10, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 15, tzinfo=timezone.utc),
            depends_on=depends_on,
        )
    )
    store.upsert_binding_health(
        MetricBindingHealth(
            program_id="acme",
            binding_id="binding-002",
            metric_id="acme.capacity_margin",
            last_success_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_attempt_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_successful_observation_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            last_failure_at=None,
            consecutive_failures=0,
            is_degraded=False,
        )
    )
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-dependent-001",
            program_id="acme",
            metric_id="acme.capacity_margin",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            value_num=12.0,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-002",
            binding_version=1,
        )
    )