from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core.hypothesis_models import (
    AssertionOperator,
    ChallengeKind,
    ChallengeSeverity,
    ChallengeState,
    CompositeAssertion,
    CompositeAssertionOperator,
    Hypothesis,
    HypothesisKind,
    HypothesisStatus,
    RealityChallenge,
    TelemetryAssertion,
)
from src.core.metric_models import MetricAggregation, MetricObservation, MetricQualityState, MetricSourceBinding, ObservationWindow
from src.core.reality_store import RealityStore, get_program_reality_db_path
from src.core.source_models import MetricBindingHealth, SourceKind, SourceRef


def test_get_program_reality_db_path_defaults_to_home_vertex_dir(tmp_path: Path) -> None:
    home_root = tmp_path / "home"

    path = get_program_reality_db_path("acme", home_root=home_root)

    assert path == home_root / ".vertex" / "acme" / "vertex.sqlite3"


def test_reality_store_round_trips_foundation_entities(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "reality-root")
    store.initialize()

    binding = MetricSourceBinding(
        binding_id="binding-001",
        metric_id="acme.cluster_count",
        program_id="acme",
        source_kind="kusto",
        cluster="https://adventure.kusto.windows.net",
        database="xdataanalytics",
        kql_template="Metrics | summarize count()",
        result_column="cluster_count",
        dimension_defaults=(("region", "eastus2"),),
        validated=True,
        last_validated_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        binding_version=2,
        kql_template_hash="hash-001",
        evidence_url_template="https://kusto/{metric_id}",
    )
    store.upsert_metric_source_binding(binding)

    assertion = TelemetryAssertion(
        id="assertion-001",
        program_id="acme",
        metric_id="acme.cluster_count",
        window=ObservationWindow(days=7, aggregation=MetricAggregation.LAST),
        operator=AssertionOperator.GTE,
        threshold=150.0,
        linked_hypothesis_id="hyp-001",
    )
    store.upsert_telemetry_assertion(assertion)

    hypothesis = Hypothesis(
        id="hyp-001",
        short_id="H-001",
        program_id="acme",
        kind=HypothesisKind.SCALAR_FACT,
        statement="We have at least 150 healthy clusters.",
        expected_value=150.0,
        as_of_date=date(2026, 5, 20),
        telemetry_assertion_id="assertion-001",
        source_refs=(SourceRef(kind=SourceKind.CLAIM, ref="claim:001"),),
        proposed_by="pm",
        proposed_at=datetime(2026, 5, 20, 12, 5, tzinfo=timezone.utc),
        status=HypothesisStatus.CONFIRMED,
        confirmed_by="pm",
        confirmed_at=datetime(2026, 5, 20, 12, 6, tzinfo=timezone.utc),
        linked_claim_id="claim-001",
    )
    store.upsert_hypothesis(hypothesis)

    challenge = RealityChallenge(
        id="challenge-001",
        program_id="acme",
        hypothesis_id="hyp-001",
        assertion_id="assertion-001",
        observation_id=None,
        challenge_kind=ChallengeKind.THRESHOLD_BREACH,
        observed_value=120.0,
        expected_value=150.0,
        delta_magnitude=2.0,
        severity=ChallengeSeverity.WARN,
        source="kusto:binding-001",
        detected_at=datetime(2026, 5, 20, 12, 10, tzinfo=timezone.utc),
        current_state=ChallengeState.OPEN,
    )
    store.upsert_challenge(challenge)

    health = MetricBindingHealth(
        program_id="acme",
        binding_id="binding-001",
        metric_id="acme.cluster_count",
        last_success_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        last_attempt_at=datetime(2026, 5, 20, 12, 10, tzinfo=timezone.utc),
        last_successful_observation_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        last_failure_at=None,
        consecutive_failures=0,
        is_degraded=False,
    )
    store.upsert_binding_health(health)

    assert store.get_metric_source_binding(binding.binding_id) == binding
    assert store.get_telemetry_assertion(assertion.id) == assertion
    assert store.get_hypothesis(hypothesis.id) == hypothesis
    assert store.get_challenge(challenge.id) == challenge
    assert store.get_binding_health(binding.binding_id) == health


def test_reality_store_round_trips_composite_assertion(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "reality-root")
    store.initialize()

    composite = CompositeAssertion(
        id="composite-001",
        program_id="acme",
        operator=CompositeAssertionOperator.AND,
        child_assertion_ids=("assertion-001", "assertion-002"),
        description="Both readiness guards must hold.",
        linked_hypothesis_id="hyp-001",
    )

    store.upsert_composite_assertion(composite)

    assert store.get_composite_assertion(composite.id) == composite
    assert store.list_active_composite_assertions() == (composite,)


def test_write_metric_observation_late_correction_preserves_identity(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "reality-root")
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-001",
            metric_id="acme.cluster_count",
            program_id="acme",
            source_kind="kusto",
            kql_template="Metrics | summarize count()",
            result_column="Count",
        )
    )

    original = MetricObservation(
        observation_id="obs-001",
        program_id="acme",
        metric_id="acme.cluster_count",
        dimensions_json='{"region":"eastus2"}',
        measurement_period_start=datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc),
        measurement_period_end=datetime(2026, 5, 19, 23, 59, tzinfo=timezone.utc),
        observed_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
        value_num=148.0,
        value_text=None,
        sample_count=1,
        quality_state=MetricQualityState.OK,
        source_binding_id="binding-001",
        binding_version=1,
    )
    inserted_id = store.write_metric_observation(original)

    corrected = MetricObservation(
        observation_id="obs-999",
        program_id="acme",
        metric_id="acme.cluster_count",
        dimensions_json='{"region":"eastus2"}',
        measurement_period_start=datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc),
        measurement_period_end=datetime(2026, 5, 19, 23, 59, tzinfo=timezone.utc),
        observed_at=datetime(2026, 5, 20, 13, 0, tzinfo=timezone.utc),
        value_num=152.0,
        value_text=None,
        sample_count=1,
        quality_state=MetricQualityState.OK,
        source_binding_id="binding-001",
        binding_version=1,
    )
    corrected_id = store.write_metric_observation(corrected, corrected_reason="late source correction")

    observations = store.list_metric_observations("acme.cluster_count")

    assert inserted_id == "obs-001"
    assert corrected_id == "obs-001"
    assert len(observations) == 1
    assert observations[0].observation_id == "obs-001"
    assert observations[0].value_num == 152.0
    assert observations[0].quality_state == MetricQualityState.LATE_CORRECTED
    assert observations[0].corrected_reason == "late source correction"

def _seed_single_observation(store: RealityStore, *, program_id: str, metric_id: str, value: float) -> None:
    store.initialize()
    store.upsert_metric_source_binding(
        MetricSourceBinding(
            binding_id="binding-obs",
            metric_id=metric_id,
            program_id=program_id,
            source_kind="kusto",
            kql_template="Metrics | summarize count()",
            result_column="Count",
        )
    )
    store.write_metric_observation(
        MetricObservation(
            observation_id="obs-latest",
            program_id=program_id,
            metric_id=metric_id,
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 19, 23, 59, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
            value_num=value,
            value_text=None,
            sample_count=1,
            quality_state=MetricQualityState.OK,
            source_binding_id="binding-obs",
            binding_version=1,
        )
    )


def test_read_latest_metric_observation_accepts_matching_program_id(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "reality-root")
    _seed_single_observation(store, program_id="acme", metric_id="acme.cluster_count", value=200.0)

    # Passing the matching program_id is an explicit, allowed assertion.
    observation = store.read_latest_metric_observation("acme.cluster_count", program_id="acme")
    assert observation is not None
    assert observation.value_num == 200.0
    # Omitting it preserves backward-compatible behavior.
    assert store.read_latest_metric_observation("acme.cluster_count") is not None


def test_read_latest_metric_observation_rejects_mismatched_program_id(tmp_path: Path) -> None:
    import pytest

    store = RealityStore("acme", db_root=tmp_path / "reality-root")
    _seed_single_observation(store, program_id="acme", metric_id="acme.cluster_count", value=200.0)

    # A mismatched program_id must fail loudly rather than silently returning acme data.
    with pytest.raises(ValueError):
        store.read_latest_metric_observation("acme.cluster_count", program_id="contoso")
