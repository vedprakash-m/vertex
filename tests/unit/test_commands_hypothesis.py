from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.assumption_tracker import save_assumptions
from src.core.hypothesis_models import ChallengeKind, ChallengeSeverity, ChallengeState, Hypothesis, HypothesisKind, HypothesisStatus, RealityChallenge, TelemetryAssertion, AssertionOperator
from src.core.hypothesis_models import CompositeAssertion, CompositeAssertionOperator
from src.core.metric_models import MetricAggregation, MetricObservation, ObservationWindow
from src.core.models_v2 import Assumption, AssumptionStatus
from src.core.reality_store import RealityStore
from src.core.source_models import SourceKind


runner = CliRunner()


def _read_confirmation_events(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _read_jsonl_records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_hypothesis_from_assumption_command_proposes_linked_hypothesis(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    monkeypatch.setattr("src.commands.hypothesis.PROGRAMS_ROOT", programs_root)
    save_assumptions(
        "acme",
        (
            Assumption(
                id="assumption-001",
                program_id="acme",
                text="Repair capacity remains above the launch floor.",
                validation_method="Daily metric check",
                validation_due=date(2026, 5, 30),
                status=AssumptionStatus.UNVALIDATED,
                linked_risk_id=None,
                linked_milestone_id=None,
                owner_alias="pm",
                identified_date=date(2026, 5, 20),
                entity_refs=("WI:1234",),
            ),
        ),
        programs_root=programs_root,
    )

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

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "from-assumption",
            "--program",
            "acme",
            "--id",
            "assumption-001",
            "--kind",
            "scalar_fact",
            "--assertion-id",
            "assertion-001",
            "--expected-value",
            "150",
            "--proposed-by",
            "lead.pm",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    proposed = store.get_hypothesis_by_short_id("H-001")

    assert result.exit_code == 0
    assert "Proposed hypothesis H-001 from assumption assumption-001" in result.stdout
    assert proposed is not None
    assert proposed.status is HypothesisStatus.PROPOSED
    assert proposed.statement == "Repair capacity remains above the launch floor."
    assert proposed.linked_assumption_id == "assumption-001"
    assert proposed.review_due == date(2026, 5, 30)
    assert proposed.source_refs[0].kind is SourceKind.ASSUMPTION
    assert proposed.source_refs[0].ref == "assumption-001"


def test_hypothesis_propose_and_confirm_commands_persist_state(tmp_path: Path) -> None:
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

    propose_result = runner.invoke(
        app,
        [
            "hypothesis",
            "propose",
            "--program",
            "acme",
            "--kind",
            "scalar_fact",
            "--statement",
            "We have at least 150 healthy clusters.",
            "--assertion-id",
            "assertion-001",
            "--expected-value",
            "150",
            "--proposed-by",
            "pm",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    proposed = store.get_hypothesis_by_short_id("H-001")

    assert propose_result.exit_code == 0
    assert proposed is not None
    assert proposed.status is HypothesisStatus.PROPOSED
    assert proposed.telemetry_assertion_id == "assertion-001"

    confirm_result = runner.invoke(
        app,
        [
            "hypothesis",
            "confirm",
            "--program",
            "acme",
            "--id",
            "H-001",
            "--confirmed-by",
            "lead.pm",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    confirmed = store.get_hypothesis(proposed.id)
    confirmation_log = tmp_path / "db" / "acme" / "_confirmations.jsonl"

    assert confirm_result.exit_code == 0
    assert confirmed is not None
    assert confirmed.status is HypothesisStatus.CONFIRMED
    assert confirmed.confirmed_by == "lead.pm"
    assert confirmed.confirmed_at is not None
    assert confirmation_log.exists()
    events = _read_confirmation_events(confirmation_log)
    assert len(events) == 1
    assert events[0]["event_type"] == "confirmed"

def test_hypothesis_propose_supports_composite_assertion_id(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_composite_assertion(
        CompositeAssertion(
            id="composite-001",
            program_id="acme",
            operator=CompositeAssertionOperator.AND,
            child_assertion_ids=("assertion-001", "assertion-002"),
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "propose",
            "--program",
            "acme",
            "--kind",
            "scalar_fact",
            "--statement",
            "Both readiness guards stay green.",
            "--composite-assertion-id",
            "composite-001",
            "--expected-value",
            "1",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    proposed = store.get_hypothesis_by_short_id("H-001")
    composite = store.get_composite_assertion("composite-001")

    assert result.exit_code == 0
    assert proposed is not None
    assert proposed.telemetry_assertion_id is None
    assert proposed.composite_assertion_id == "composite-001"
    assert composite is not None
    assert composite.linked_hypothesis_id == proposed.id


def test_hypothesis_show_renders_composite_assertion_in_json(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_composite_assertion(
        CompositeAssertion(
            id="composite-001",
            program_id="acme",
            operator=CompositeAssertionOperator.OR,
            child_assertion_ids=("assertion-001", "assertion-002"),
            linked_hypothesis_id="hyp-001",
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Either readiness guard can satisfy the check.",
            expected_value=1.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id=None,
            composite_assertion_id="composite-001",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "show",
            "--program",
            "acme",
            "--id",
            "H-001",
            "--format",
            "json",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    payload = json.loads(result.output)

    assert result.exit_code == 0
    assert payload["assertion"] is None
    assert payload["composite_assertion"]["id"] == "composite-001"
    assert payload["composite_assertion"]["operator"] == "or"


def test_hypothesis_propose_persists_dependency_links(tmp_path: Path) -> None:
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
            id="hyp-upstream",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Upstream dependency stays healthy.",
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

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "propose",
            "--program",
            "acme",
            "--kind",
            "scalar_fact",
            "--statement",
            "Capacity margin stays above 10.",
            "--assertion-id",
            "assertion-002",
            "--expected-value",
            "10",
            "--depends-on",
            "H-001",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    proposed = store.get_hypothesis_by_short_id("H-002")

    assert result.exit_code == 0
    assert proposed is not None
    assert proposed.depends_on == ("hyp-upstream",)


def test_hypothesis_update_can_replace_dependency_links(tmp_path: Path) -> None:
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
            linked_hypothesis_id="hyp-upstream-1",
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
            linked_hypothesis_id="hyp-current",
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-upstream-1",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="First upstream dependency.",
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
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-upstream-2",
            short_id="H-002",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Second upstream dependency.",
            expected_value=150.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id=None,
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 10, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 15, tzinfo=timezone.utc),
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-current",
            short_id="H-003",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Current dependent hypothesis.",
            expected_value=10.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id="assertion-002",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 20, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 25, tzinfo=timezone.utc),
            depends_on=("hyp-upstream-1",),
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "update",
            "--program",
            "acme",
            "--id",
            "H-003",
            "--reason",
            "Refine cascade dependency.",
            "--depends-on",
            "H-002",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    replacement = store.get_hypothesis_by_short_id("H-003")

    assert result.exit_code == 0
    assert replacement is not None
    assert replacement.id != "hyp-current"
    assert replacement.depends_on == ("hyp-upstream-2",)


def test_hypothesis_export_confirmations_writes_seed_jsonl_for_active_l1_state(tmp_path: Path) -> None:
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
            linked_hypothesis_id="hyp-confirmed",
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-confirmed",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="Repair capacity remains above the floor.",
            expected_value=150.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id="assertion-001",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="lead.pm",
            confirmed_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-challenged",
            short_id="H-002",
            program_id="acme",
            kind=HypothesisKind.DELIVERY_DATE,
            statement="The release lands by the promised date.",
            expected_value="2026-06-01",
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id=None,
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 11, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CHALLENGED,
            confirmed_by="lead.pm",
            confirmed_at=datetime(2026, 5, 20, 11, 30, tzinfo=timezone.utc),
            linked_ado_item_id=12345,
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-proposed",
            short_id="H-003",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="A proposed hypothesis should not be exported.",
            expected_value=175.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id=None,
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.PROPOSED,
        )
    )
    store.upsert_challenge(
        RealityChallenge(
            id="challenge-001",
            program_id="acme",
            hypothesis_id="hyp-challenged",
            assertion_id=None,
            observation_id=None,
            challenge_kind=ChallengeKind.MANUAL,
            observed_value=None,
            expected_value=None,
            delta_magnitude=None,
            severity=ChallengeSeverity.ALERT,
            source="pm_review",
            detected_at=datetime(2026, 5, 21, 9, 0, tzinfo=timezone.utc),
            note="Waiting on updated target date.",
            current_state=ChallengeState.OPEN,
        )
    )

    export_path = tmp_path / "exports" / "confirmations.seed.jsonl"
    result = runner.invoke(
        app,
        [
            "hypothesis",
            "export-confirmations",
            "--program",
            "acme",
            "--output",
            str(export_path),
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    records = _read_jsonl_records(export_path)
    record_types = [record["record_type"] for record in records]
    hypothesis_records = [record["record"] for record in records if record["record_type"] == "hypothesis"]
    challenge_records = [record["record"] for record in records if record["record_type"] == "challenge"]

    assert result.exit_code == 0
    assert export_path.exists()
    assert "Exported 5 confirmation seed records" in result.stdout
    assert record_types == ["seed_metadata", "telemetry_assertion", "hypothesis", "hypothesis", "challenge"]
    assert records[0]["program_id"] == "acme"
    assert records[0]["assertion_count"] == 1
    assert records[0]["hypothesis_count"] == 2
    assert records[0]["challenge_count"] == 1
    assert {record["id"] for record in hypothesis_records} == {"hyp-confirmed", "hyp-challenged"}
    assert {record["status"] for record in hypothesis_records} == {"confirmed", "challenged"}
    assert all(record["id"] != "hyp-proposed" for record in hypothesis_records)
    assert challenge_records == [
        {
            "ado_current_target": None,
            "assertion_id": None,
            "challenge_kind": "manual",
            "current_state": "open",
            "delta_magnitude": None,
            "detected_at": "2026-05-21T09:00:00+00:00",
            "evidence_url": None,
            "expected_value": None,
            "hypothesis_id": "hyp-challenged",
            "id": "challenge-001",
            "last_event_at": challenge_records[0]["last_event_at"],
            "note": "Waiting on updated target date.",
            "observation_id": None,
            "observed_value": None,
            "policy_version": 1,
            "program_id": "acme",
            "severity": "alert",
            "snooze_reason": None,
            "snoozed_until": None,
            "source": "pm_review",
            "state_actor": None,
            "state_changed_at": None,
            "state_reason": None,
        }
    ]


def test_hypothesis_propose_rejects_archived_assertion_id(tmp_path: Path) -> None:
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
            valid_until=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "propose",
            "--program",
            "acme",
            "--kind",
            "scalar_fact",
            "--statement",
            "We have at least 150 healthy clusters.",
            "--assertion-id",
            "assertion-001",
            "--expected-value",
            "150",
            "--proposed-by",
            "pm",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    assert result.exit_code != 0
    assert "is archived" in result.output


def test_hypothesis_reject_command_rejects_proposed_hypothesis(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.DELIVERY_DATE,
            statement="The release lands by the promised date.",
            expected_value="2026-06-01",
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
        [
            "hypothesis",
            "reject",
            "--program",
            "acme",
            "--id",
            "H-001",
            "--reason",
            "The proposal duplicates an existing plan artifact.",
            "--rejected-by",
            "lead.pm",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    rejected = store.get_hypothesis("hyp-001")

    assert result.exit_code == 0
    assert "Rejected hypothesis H-001" in result.stdout
    assert rejected is not None
    assert rejected.status is HypothesisStatus.REJECTED
    assert rejected.rejection_reason == "The proposal duplicates an existing plan artifact."


def test_hypothesis_challenge_command_creates_manual_alert_challenge(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.DELIVERY_DATE,
            statement="The release lands by the promised date.",
            expected_value="2026-06-01",
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id=None,
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
            linked_ado_item_id=12345,
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "challenge",
            "--program",
            "acme",
            "--id",
            "H-001",
            "--reason",
            "Hallway conversation with infra lead indicates the commitment is at risk.",
            "--challenged-by",
            "lead.pm",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    challenged = store.get_hypothesis("hyp-001")
    manual_challenge = store.get_latest_challenge_for_hypothesis("hyp-001", challenge_kind=ChallengeKind.MANUAL)

    assert result.exit_code == 0
    assert "Challenged hypothesis H-001 with manual challenge" in result.stdout
    assert challenged is not None
    assert challenged.status is HypothesisStatus.CHALLENGED
    assert manual_challenge is not None
    assert manual_challenge.challenge_kind is ChallengeKind.MANUAL
    assert manual_challenge.severity is ChallengeSeverity.ALERT
    assert manual_challenge.source == "pm:lead.pm"
    assert manual_challenge.note == "Hallway conversation with infra lead indicates the commitment is at risk."
    assert manual_challenge.current_state is ChallengeState.OPEN


def test_hypothesis_show_command_returns_lifecycle_assertion_and_recent_observations(tmp_path: Path) -> None:
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
            linked_hypothesis_id="hyp-001",
        )
    )
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-000",
            short_id="superseded:hyp-000",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="We have at least 140 healthy clusters.",
            expected_value=140.0,
            as_of_date=date(2026, 5, 19),
            telemetry_assertion_id="assertion-001",
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 19, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.SUPERSEDED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 19, 9, 5, tzinfo=timezone.utc),
            superseded_by="hyp-001",
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
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
            supersedes_id="hyp-000",
        )
    )
    store.write_metric_observation(
        MetricObservation(
            observation_id="observation-001",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 19, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 19, 23, 59, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 20, 8, 0, tzinfo=timezone.utc),
            value_num=148.0,
            value_text=None,
            sample_count=1,
        )
    )
    store.write_metric_observation(
        MetricObservation(
            observation_id="observation-002",
            program_id="acme",
            metric_id="acme.cluster_count",
            dimensions_json="{}",
            measurement_period_start=datetime(2026, 5, 20, 0, 0, tzinfo=timezone.utc),
            measurement_period_end=datetime(2026, 5, 20, 23, 59, tzinfo=timezone.utc),
            observed_at=datetime(2026, 5, 21, 8, 0, tzinfo=timezone.utc),
            value_num=152.0,
            value_text=None,
            sample_count=1,
        )
    )
    store.upsert_challenge(
        RealityChallenge(
            id="challenge-001",
            program_id="acme",
            hypothesis_id="hyp-001",
            assertion_id="assertion-001",
            observation_id=None,
            challenge_kind=ChallengeKind.THRESHOLD_BREACH,
            observed_value=148.0,
            expected_value=150.0,
            delta_magnitude=1.2,
            severity=ChallengeSeverity.WARN,
            source="kusto:binding-001",
            detected_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "show",
            "--program",
            "acme",
            "--id",
            "H-001",
            "--format",
            "json",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    payload = json.loads(result.stdout)

    assert result.exit_code == 0
    assert payload["hypothesis"]["id"] == "hyp-001"
    assert payload["assertion"]["id"] == "assertion-001"
    assert payload["latest_challenge"]["id"] == "challenge-001"
    assert [item["id"] for item in payload["lifecycle"]] == ["hyp-000", "hyp-001"]
    assert [item["observation_id"] for item in payload["recent_observations"]] == ["observation-002", "observation-001"]
    assert payload["annotations"] == []


def test_hypothesis_annotation_commands_add_list_archive_without_changing_hypothesis_state(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="We have at least 150 healthy clusters.",
            expected_value=150.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id=None,
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
        )
    )

    add_result = runner.invoke(
        app,
        [
            "hypothesis",
            "annotate",
            "add",
            "--program",
            "acme",
            "--id",
            "H-001",
            "--kind",
            "markdown",
            "--title",
            "Launch notes",
            "--locator",
            "notes/launch.md",
            "--locator-kind",
            "repo_path",
            "--note",
            "Context from the review prep doc.",
            "--tag",
            "review",
            "--tag",
            "context",
            "--added-by",
            "lead.pm",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    annotations = store.list_hypothesis_annotations("hyp-001")
    list_result = runner.invoke(
        app,
        [
            "hypothesis",
            "annotate",
            "list",
            "--program",
            "acme",
            "--id",
            "H-001",
            "--format",
            "json",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )
    listed_payload = json.loads(list_result.stdout)

    assert add_result.exit_code == 0
    assert len(annotations) == 1
    assert annotations[0].title == "Launch notes"
    assert annotations[0].locator == "notes/launch.md"
    assert annotations[0].locator_kind == "repo_path"
    assert annotations[0].tags == ("review", "context")
    assert annotations[0].source_ref is not None
    assert annotations[0].source_ref.kind is SourceKind.DOCUMENT
    assert store.get_hypothesis("hyp-001").status is HypothesisStatus.CONFIRMED
    assert list_result.exit_code == 0
    assert listed_payload[0]["title"] == "Launch notes"
    assert listed_payload[0]["archive_reason"] is None

    archive_result = runner.invoke(
        app,
        [
            "hypothesis",
            "annotate",
            "archive",
            "--program",
            "acme",
            "--annotation-id",
            annotations[0].id,
            "--reason",
            "Superseded by the final shiproom notes.",
            "--archived-by",
            "lead.pm",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    active_annotations = store.list_hypothesis_annotations("hyp-001")
    archived_annotations = store.list_hypothesis_annotations("hyp-001", include_archived=True)

    assert archive_result.exit_code == 0
    assert active_annotations == ()
    assert len(archived_annotations) == 1
    assert archived_annotations[0].archived_by == "lead.pm"
    assert archived_annotations[0].archive_reason == "Superseded by the final shiproom notes."


def test_hypothesis_show_command_includes_annotations(tmp_path: Path) -> None:
    store = RealityStore("acme", db_root=tmp_path / "db")
    store.initialize()
    store.upsert_hypothesis(
        Hypothesis(
            id="hyp-001",
            short_id="H-001",
            program_id="acme",
            kind=HypothesisKind.SCALAR_FACT,
            statement="We have at least 150 healthy clusters.",
            expected_value=150.0,
            as_of_date=date(2026, 5, 20),
            telemetry_assertion_id=None,
            proposed_by="pm",
            proposed_at=datetime(2026, 5, 20, 9, 0, tzinfo=timezone.utc),
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
        )
    )

    add_result = runner.invoke(
        app,
        [
            "hypothesis",
            "annotate",
            "add",
            "--program",
            "acme",
            "--id",
            "H-001",
            "--kind",
            "url",
            "--title",
            "Incident review deck",
            "--locator",
            "https://contoso.example/review",
            "--locator-kind",
            "url",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )
    show_result = runner.invoke(
        app,
        [
            "hypothesis",
            "show",
            "--program",
            "acme",
            "--id",
            "H-001",
            "--format",
            "json",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    payload = json.loads(show_result.stdout)

    assert add_result.exit_code == 0
    assert show_result.exit_code == 0
    assert payload["annotations"][0]["title"] == "Incident review deck"
    assert payload["annotations"][0]["locator"] == "https://contoso.example/review"
    assert payload["annotations"][0]["locator_kind"] == "url"


def test_hypothesis_update_supersedes_prior_row_and_moves_snoozed_challenges(tmp_path: Path) -> None:
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
            linked_hypothesis_id="hyp-001",
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
            status=HypothesisStatus.CONFIRMED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
        )
    )
    store.upsert_challenge(
        RealityChallenge(
            id="challenge-001",
            program_id="acme",
            hypothesis_id="hyp-001",
            assertion_id="assertion-001",
            observation_id=None,
            challenge_kind=ChallengeKind.THRESHOLD_BREACH,
            observed_value=120.0,
            expected_value=150.0,
            delta_magnitude=2.5,
            severity=ChallengeSeverity.WARN,
            source="kusto:binding-001",
            detected_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
            current_state=ChallengeState.SNOOZED,
            snoozed_until=datetime(2026, 5, 21, 10, 0, tzinfo=timezone.utc),
            snooze_reason="Awaiting mitigation",
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "update",
            "--program",
            "acme",
            "--id",
            "H-001",
            "--statement",
            "We have at least 160 healthy clusters.",
            "--expected-value",
            "160",
            "--reason",
            "Raised the target after capacity expansion.",
            "--updated-by",
            "lead.pm",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    superseded = store.get_hypothesis("hyp-001")
    replacement = store.get_hypothesis_by_short_id("H-001")
    moved_challenge = store.get_challenge("challenge-001")
    assertion = store.get_telemetry_assertion("assertion-001")
    confirmation_log = tmp_path / "db" / "acme" / "_confirmations.jsonl"

    assert result.exit_code == 0
    assert superseded is not None
    assert superseded.status is HypothesisStatus.SUPERSEDED
    assert superseded.superseded_by is not None
    assert superseded.short_id.startswith("superseded:")
    assert replacement is not None
    assert replacement.id != superseded.id
    assert replacement.status is HypothesisStatus.CONFIRMED
    assert replacement.satisfies if False else True
    assert replacement.expected_value == 160.0
    assert replacement.statement == "We have at least 160 healthy clusters."
    assert replacement.supersedes_id == "hyp-001"
    assert moved_challenge is not None
    assert moved_challenge.hypothesis_id == replacement.id
    assert assertion is not None
    assert assertion.linked_hypothesis_id == replacement.id
    assert confirmation_log.exists()
    events = _read_confirmation_events(confirmation_log)
    assert [event["event_type"] for event in events] == ["superseded", "confirmed"]
    assert events[0]["hypothesis_id"] == "hyp-001"
    assert events[0]["status"] == "superseded"
    assert events[0]["superseded_by"] == replacement.id
    assert events[1]["hypothesis_id"] == replacement.id
    assert events[1]["status"] == "confirmed"
    assert events[1]["supersedes_id"] == "hyp-001"


def test_hypothesis_reinstate_command_confirms_hypothesis_and_dismisses_active_challenge(tmp_path: Path) -> None:
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
            linked_hypothesis_id="hyp-001",
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
            status=HypothesisStatus.CHALLENGED,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
        )
    )
    store.upsert_challenge(
        RealityChallenge(
            id="challenge-001",
            program_id="acme",
            hypothesis_id="hyp-001",
            assertion_id="assertion-001",
            observation_id=None,
            challenge_kind=ChallengeKind.THRESHOLD_BREACH,
            observed_value=120.0,
            expected_value=150.0,
            delta_magnitude=2.5,
            severity=ChallengeSeverity.ALERT,
            source="kusto:binding-001",
            detected_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "reinstate",
            "--program",
            "acme",
            "--id",
            "H-001",
            "--reason",
            "Mitigation shipped and metric recovered.",
            "--reinstated-by",
            "lead.pm",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    reinstated = store.get_hypothesis("hyp-001")
    challenge = store.get_challenge("challenge-001")
    evaluations = store.list_assertion_evaluations()

    assert result.exit_code == 0
    assert "Reinstated hypothesis H-001; dismissed 1 active challenge(s)." in result.stdout
    assert reinstated is not None
    assert reinstated.status is HypothesisStatus.CONFIRMED
    assert challenge is not None
    assert challenge.current_state is ChallengeState.DISMISSED
    assert len(evaluations) == 1
    assert evaluations[0].hypothesis_id == "hyp-001"
    assert evaluations[0].assertion_id == "assertion-001"
    assert evaluations[0].violated is False
    assert evaluations[0].observation_id is None
    assert evaluations[0].note == "reinstated:Mitigation shipped and metric recovered."


def test_hypothesis_invalidate_command_invalidates_hypothesis_and_resolves_active_challenge(tmp_path: Path) -> None:
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
            linked_hypothesis_id="hyp-001",
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
            status=HypothesisStatus.STALE,
            confirmed_by="pm",
            confirmed_at=datetime(2026, 5, 20, 9, 5, tzinfo=timezone.utc),
        )
    )
    store.upsert_challenge(
        RealityChallenge(
            id="challenge-001",
            program_id="acme",
            hypothesis_id="hyp-001",
            assertion_id="assertion-001",
            observation_id=None,
            challenge_kind=ChallengeKind.STALENESS,
            observed_value=None,
            expected_value=None,
            delta_magnitude=None,
            severity=ChallengeSeverity.WARN,
            source="reconciler:staleness",
            detected_at=datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc),
        )
    )

    result = runner.invoke(
        app,
        [
            "hypothesis",
            "invalidate",
            "--program",
            "acme",
            "--id",
            "H-001",
            "--reason",
            "Program commitment is no longer valid.",
            "--invalidated-by",
            "lead.pm",
            "--db-root",
            str(tmp_path / "db"),
        ],
    )

    invalidated = store.get_hypothesis("hyp-001")
    challenge = store.get_challenge("challenge-001")
    confirmation_log = tmp_path / "db" / "acme" / "_confirmations.jsonl"

    assert result.exit_code == 0
    assert "Invalidated hypothesis H-001; resolved 1 active challenge(s)." in result.stdout
    assert invalidated is not None
    assert invalidated.status is HypothesisStatus.INVALIDATED
    assert challenge is not None
    assert challenge.current_state is ChallengeState.RESOLVED
    assert confirmation_log.exists()
    events = _read_confirmation_events(confirmation_log)
    assert len(events) == 1
    assert events[0]["event_type"] == "invalidated"
    assert events[0]["hypothesis_id"] == "hyp-001"
    assert events[0]["status"] == "invalidated"