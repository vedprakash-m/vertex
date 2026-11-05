from dataclasses import FrozenInstanceError, asdict, replace
from datetime import date, datetime, timedelta, timezone
from itertools import product
import math

import pytest

from src.core.exceptions import ConfigError
from src.core.forecast_engine import ETAForecast
from src.core.manifest_writer import hash_jsonable
from src.core.models import Confidence, ConfirmedDimension, EditionType, RiskLevel, RunEvidenceSnapshot, Snapshot, SnapshotItem
from src.core.models_v2 import RiskImpact, RiskProbability, RiskStatus
from src.core.program_reality import FactAssessment
from src.core.truth_levels import TruthLevel


NOW = datetime(2026, 11, 5, 20, tzinfo=timezone.utc)
RISKS = (RiskLevel.DONE, RiskLevel.LOW, RiskLevel.UNKNOWN, RiskLevel.MEDIUM, RiskLevel.HIGH, RiskLevel.BLOCKED)


def _snapshot(*, risk=RiskLevel.LOW, state="Active", target=date(2026, 11, 10), dimensions=(RiskLevel.LOW,)):
    return Snapshot(
        issue_number=1, generated_at=NOW, ado_data_as_of=NOW,
        edition_type=EditionType.DETAILED,
        items=(SnapshotItem(1, "Task", "Synthetic", state, None, "Synthetic", target, risk, []),),
        scorecards=tuple(
            ConfirmedDimension("Synthetic", f"Lane-{index}", value, None, 1, "")
            for index, value in enumerate(dimensions)
        ),
    )


def _capture(snapshot):
    capture = RunEvidenceSnapshot(
        program_id="synthetic", fact_snapshot_id="pin", fact_content_sha256="a" * 64,
        as_of=NOW, valid_at=NOW, gather_run_id=None, gather_run_hash=None,
        source_scopes=(), binding_declarations=(),
        fetched_item_ids=tuple(sorted(item.id for item in snapshot.items)),
        report_snapshot_hash=hash_jsonable(snapshot), config_hash="sha256:" + "b" * 64,
        sha256="", effective_family_modes=(("judgment", "primary"),),
    )
    payload = asdict(capture)
    payload.pop("sha256")
    return replace(capture, sha256=hash_jsonable(payload))


def _forecast(probability=0.1, **changes):
    return replace(ETAForecast(
        work_item_id=1, ado_target_date=date(2026, 11, 10),
        predicted_target_date=date(2026, 11, 11), confidence=Confidence.HIGH,
        slip_probability=probability, reasoning="Synthetic", prior_slips=0,
        p50_date=date(2026, 11, 10),
    ), **changes)


def _observe(snapshot=None, forecasts=()):
    from src.core.program_verdict import observe_program_verdict

    snapshot = snapshot or _snapshot()
    return observe_program_verdict(_capture(snapshot), snapshot, forecasts)


@pytest.mark.parametrize(("item_risk", "dimension_risk"), list(product(RISKS, RISKS)))
def test_observer_preserves_known_adverse_risk_without_green_qualification(item_risk, dimension_risk):
    observed = _observe(_snapshot(risk=item_risk, dimensions=(dimension_risk,)))
    ranks = {risk: index for index, risk in enumerate(RISKS)}
    assert observed.verdict.health == max((item_risk, dimension_risk, RiskLevel.UNKNOWN), key=ranks.__getitem__)
    assert observed.verdict.confidence == Confidence.NONE
    assert observed.verdict.completion_phase == "unknown"
    assert observed.verdict.valid_until is None
    assert observed.mode == "observe"
    assert {gap.code for gap in observed.verdict.evidence_gaps} >= {
        "nonfact_inputs_unsealed", "effective_policy_uncaptured", "trust_unevaluated",
        "dependencies_unevaluated", "completion_validation_unevaluated",
        "overrides_unevaluated", "contradictions_unevaluated", "horizon_outcomes_unevaluated",
        "risk_register_asks_unevaluated",
    }


@pytest.mark.parametrize(("probability", "expected"), [
    (0.0, RiskLevel.UNKNOWN), (0.499999, RiskLevel.UNKNOWN),
    (0.5, RiskLevel.MEDIUM), (0.799999, RiskLevel.MEDIUM),
    (0.8, RiskLevel.HIGH), (1.0, RiskLevel.HIGH),
])
def test_observer_schedule_probability_floors_are_inclusive(probability, expected):
    verdict = _observe(forecasts=(_forecast(probability),)).verdict
    assert verdict.schedule_risk == expected
    assert verdict.health == expected
    assert (any(reason.code == "miss_probability_high" for reason in verdict.reasons)) is (probability >= 0.8)
    assert (any(reason.code == "miss_probability_medium" for reason in verdict.reasons)) is (0.5 <= probability < 0.8)


@pytest.mark.parametrize(("target_offset", "p50_offset", "expected"), [
    (-1, 2, RiskLevel.MEDIUM), (0, 0, RiskLevel.UNKNOWN),
    (1, -1, RiskLevel.MEDIUM), (1, 0, RiskLevel.UNKNOWN),
])
def test_observer_overdue_and_p50_use_strict_utc_date(target_offset, p50_offset, expected):
    target = NOW.date() + timedelta(days=target_offset)
    forecast = _forecast(ado_target_date=target, p50_date=NOW.date() + timedelta(days=p50_offset))
    verdict = _observe(_snapshot(target=target), (forecast,)).verdict
    assert verdict.schedule_risk == expected


@pytest.mark.parametrize("state", ["Closed", "Done", "Resolved", "Completed", "Removed", "Cut"])
def test_observer_does_not_make_terminal_items_overdue(state):
    target = NOW.date() - timedelta(days=2)
    verdict = _observe(
        _snapshot(state=state, target=target, risk=RiskLevel.DONE, dimensions=(RiskLevel.DONE,)),
        (_forecast(1.0, ado_target_date=target, p50_date=target),),
    ).verdict
    assert verdict.schedule_risk == RiskLevel.UNKNOWN
    assert verdict.health == RiskLevel.UNKNOWN
    assert not verdict.reasons
    assert verdict.completion_phase != "done"


def test_observer_targetless_forecast_cannot_claim_miss_probability():
    verdict = _observe(_snapshot(target=None), (_forecast(1.0, ado_target_date=None),)).verdict
    assert verdict.schedule_risk == RiskLevel.UNKNOWN
    assert "forecast_target_missing" in {gap.code for gap in verdict.evidence_gaps}


def test_observer_empty_evidence_is_unknown_with_explicit_gaps():
    snapshot = replace(_snapshot(), items=(), scorecards=())
    verdict = _observe(snapshot).verdict
    assert verdict.health == verdict.schedule_risk == RiskLevel.UNKNOWN
    assert {"dimensions_missing", "items_missing", "schedule_evidence_missing"} <= {gap.code for gap in verdict.evidence_gaps}


def test_observer_partial_forecasts_disclose_each_unassessed_item():
    snapshot = _snapshot()
    snapshot = replace(snapshot, items=snapshot.items + (replace(snapshot.items[0], id=2),))
    verdict = _observe(snapshot, (_forecast(0.8),)).verdict
    missing, = (gap for gap in verdict.evidence_gaps if gap.code == "forecast_missing")
    assert missing.entity_refs == ("WI:2",)
    assert verdict.schedule_risk == RiskLevel.HIGH


@pytest.mark.parametrize("probability", [-0.01, 1.01, math.nan, math.inf, -math.inf, True])
def test_observer_refuses_invalid_probabilities(probability):
    with pytest.raises(ConfigError, match="probability"):
        _observe(forecasts=(_forecast(probability),))


@pytest.mark.parametrize("damage", ["snapshot", "membership", "capture_hash", "clock", "duplicate_item", "duplicate_dimension"])
def test_observer_refuses_unbound_or_invalid_inputs(damage):
    from src.core.program_verdict import observe_program_verdict

    snapshot = _snapshot()
    capture = _capture(snapshot)
    if damage == "snapshot":
        snapshot = replace(snapshot, items=())
    elif damage == "membership":
        capture = replace(capture, fetched_item_ids=(2,))
    elif damage == "capture_hash":
        capture = replace(capture, program_id="other")
    elif damage == "clock":
        snapshot = replace(snapshot, generated_at=NOW.replace(tzinfo=None))
        capture = _capture(snapshot)
    elif damage == "duplicate_item":
        snapshot = replace(snapshot, items=snapshot.items * 2)
        capture = _capture(snapshot)
    else:
        snapshot = replace(snapshot, scorecards=snapshot.scorecards * 2)
        capture = _capture(snapshot)
    with pytest.raises(ConfigError):
        observe_program_verdict(capture, snapshot, ())


def test_observer_refuses_duplicate_forecasts():
    with pytest.raises(ConfigError, match="forecast"):
        _observe(forecasts=(_forecast(), _forecast()))


@pytest.mark.parametrize("work_item_id", [0, -1, True, 1.0, "1"])
def test_observer_refuses_invalid_forecast_identity(work_item_id):
    with pytest.raises(ConfigError, match="forecast identity"):
        _observe(forecasts=(_forecast(work_item_id=work_item_id),))


def test_observer_rejects_invalid_forecast_identity_before_canonical_sort():
    with pytest.raises(ConfigError, match="forecast identity"):
        _observe(forecasts=(_forecast(), _forecast(work_item_id="2")))


def test_evaluator_rejects_invalid_forecast_identity_in_retained_inputs():
    from src.core.program_verdict import ProgramVerdictInputs, evaluate_program_verdict

    snapshot = _snapshot()
    inputs = ProgramVerdictInputs(_capture(snapshot), snapshot, (_forecast(work_item_id=0),))
    with pytest.raises(ConfigError, match="forecast identity"):
        evaluate_program_verdict(inputs)


@pytest.mark.parametrize(("forecast", "gap"), [
    (_forecast(1.0, work_item_id=2), "forecast_item_uncaptured"),
    (_forecast(1.0, ado_target_date=date(2026, 11, 11), p50_date=date(2026, 11, 1)), "forecast_target_changed"),
])
def test_observer_discloses_unbound_forecasts_without_promoting_or_blocking_them(forecast, gap):
    observation = _observe(forecasts=(forecast,))
    assert observation.inputs.forecasts == (forecast,)
    assert observation.verdict.schedule_risk == RiskLevel.UNKNOWN
    assert gap in {finding.code for finding in observation.verdict.evidence_gaps}
    assert not observation.verdict.reasons


def test_observation_retains_exact_replayable_inputs_and_frozen_result():
    from src.core.program_verdict import evaluate_program_verdict

    observation = _observe(forecasts=(_forecast(0.8),))
    assert evaluate_program_verdict(observation.inputs) == observation.verdict
    assert hash_jsonable(observation.inputs) == observation.inputs_sha256
    assert observation.verdict.snapshot_id == observation.inputs.capture.sha256
    assert observation.verdict.as_of == NOW
    assert observation.verdict.source_refs == ("WI:1",)
    with pytest.raises(FrozenInstanceError):
        observation.verdict.health = RiskLevel.LOW


def test_observer_forecast_order_is_canonical():
    snapshot = _snapshot()
    snapshot = replace(snapshot, items=snapshot.items + (replace(snapshot.items[0], id=2),))
    forecasts = (_forecast(0.8), _forecast(0.5, work_item_id=2))
    assert _observe(snapshot, forecasts) == _observe(snapshot, tuple(reversed(forecasts)))


def test_observer_diagnostic_order_is_canonical():
    from src.core.program_verdict import observe_program_verdict

    snapshot = _snapshot()
    capture = _capture(snapshot)
    first = observe_program_verdict(
        capture, snapshot, (), risk_warnings=("second", "first"),
        milestone_warnings=("later", "earlier"),
    )
    second = observe_program_verdict(
        capture, snapshot, (), risk_warnings=("first", "second"),
        milestone_warnings=("earlier", "later"),
    )
    assert first == second
    assert first.inputs.risk_warnings == ("first", "second")
    assert first.inputs.milestone_warnings == ("earlier", "later")


def test_observer_uses_utc_date_when_local_assessment_is_previous_day():
    snapshot = replace(
        _snapshot(target=date(2026, 11, 5)),
        ado_data_as_of=datetime(2026, 11, 5, 23, 30, tzinfo=timezone(timedelta(hours=-8))),
    )
    verdict = _observe(snapshot).verdict
    assert verdict.as_of == datetime(2026, 11, 6, 7, 30, tzinfo=timezone.utc)
    assert verdict.schedule_risk == RiskLevel.MEDIUM


def test_observer_uses_selected_assessment_clock_not_artifact_generation_time():
    snapshot = replace(_snapshot(), generated_at=NOW + timedelta(days=30))
    verdict = _observe(snapshot).verdict
    assert verdict.as_of == NOW
    assert verdict.schedule_risk == RiskLevel.UNKNOWN


def test_retained_input_mutation_cannot_recompute_as_the_original_capture():
    from src.core.program_verdict import evaluate_program_verdict

    observation = _observe()
    observation.inputs.snapshot.items[0].tags.append("changed after capture")
    with pytest.raises(ConfigError, match="identity"):
        evaluate_program_verdict(observation.inputs)


def _risk(**changes):
    from tests.unit.test_risk_register_engine import _risk_entry

    return replace(
        _risk_entry(), **{
            "program_id": "synthetic", "owner_alias": "", "mitigation_due_date": None,
            **changes,
        },
    )


def _risk_assessment(record, **changes):
    return FactAssessment(**{
        "record": record, "fact_id": "fact-risk-1", "truth_level": TruthLevel.HUMAN_CONFIRMED,
        "disputed": False, "stale": False, "provisional_inputs": False,
        "evidence": ("signal:risk-1",), **changes,
    })


def _observe_risks(risks, assessments=None, warnings=()):
    from src.core.program_verdict import observe_program_verdict

    snapshot = _snapshot()
    return observe_program_verdict(
        _capture(snapshot), snapshot, (), risks=risks,
        risk_assessments=assessments, risk_warnings=warnings,
    )


@pytest.mark.parametrize(("probability", "impact", "score"), [
    (RiskProbability.POSSIBLE, RiskImpact.CRITICAL, 8),
    (RiskProbability.LIKELY, RiskImpact.HIGH, 9),
    (RiskProbability.LIKELY, RiskImpact.CRITICAL, 12),
    (RiskProbability.VERY_LIKELY, RiskImpact.CRITICAL, 16),
    (RiskProbability.UNASSESSED, RiskImpact.HIGH, 0),
    (RiskProbability.LIKELY, RiskImpact.UNASSESSED, 0),
])
def test_verdict_risk_asks_reuse_existing_score_threshold(probability, impact, score):
    risk = _risk(probability=probability, impact=impact)
    observed = _observe_risks((risk,), (_risk_assessment(risk),))
    assert observed.verdict.health == (RiskLevel.HIGH if score >= 9 else RiskLevel.UNKNOWN)
    assert len(observed.verdict.leadership_asks) == (1 if score >= 9 else 0)
    assert observed.verdict.confidence == Confidence.NONE
    assert "risk_register_coverage_unqualified" in {gap.code for gap in observed.verdict.evidence_gaps}
    assert "risk_register_asks_unevaluated" not in {gap.code for gap in observed.verdict.evidence_gaps}


@pytest.mark.parametrize(("owner", "eta", "missing"), [
    ("", None, ("owner", "mitigation_eta")),
    ("  ", date(2026, 11, 10), ("owner",)),
    ("maintainer", None, ("mitigation_eta",)),
    ("maintainer", date(2026, 11, 10), ()),
])
def test_verdict_risk_ask_identifies_only_actual_missing_fields(owner, eta, missing):
    risk = _risk(owner_alias=owner, mitigation_due_date=eta)
    observed = _observe_risks((risk,), (_risk_assessment(risk),))
    assert observed.inputs.risks == (risk,)
    assert observed.verdict.health == RiskLevel.HIGH
    if missing:
        ask, = observed.verdict.leadership_asks
        assert ask.missing_fields == missing
        assert ask.entity_refs == ("RISK:risk-1",)
        assert ask.owner_alias == (owner.strip() or None)
        assert ask.evidence_refs == ("FACT:fact-risk-1", "signal:risk-1")
        assert "risk-1" in ask.text
        assert (("owner" in ask.text) == ("owner" in missing))
        assert (("mitigation ETA" in ask.text) == ("mitigation_eta" in missing))
    else:
        assert observed.verdict.leadership_asks == ()


@pytest.mark.parametrize(("status", "kind"), list(product(
    (RiskStatus.OPEN, RiskStatus.ESCALATED, RiskStatus.MITIGATED, RiskStatus.ACCEPTED, RiskStatus.CLOSED),
    ("strategic", "candidate", "hygiene"),
)))
def test_verdict_only_active_strategic_risks_create_floor_and_ask(status, kind):
    risk = _risk(status=status, kind=kind)
    observed = _observe_risks((risk,), (_risk_assessment(risk),))
    active = status in {RiskStatus.OPEN, RiskStatus.ESCALATED} and kind == "strategic"
    assert observed.verdict.health == (RiskLevel.HIGH if active else RiskLevel.UNKNOWN)
    assert bool(observed.verdict.leadership_asks) is active


@pytest.mark.parametrize(("change", "gap"), [
    ({"disputed": True}, "risk_assessment_disputed"),
    ({"stale": True}, "risk_assessment_stale"),
    ({"provisional_inputs": True}, "risk_assessment_provisional"),
])
def test_verdict_risk_assessment_limits_cannot_erase_adverse_findings(change, gap):
    risk = _risk()
    observed = _observe_risks((risk,), (_risk_assessment(risk, **change),))
    assert observed.verdict.health == RiskLevel.HIGH
    assert len(observed.verdict.leadership_asks) == 1
    assert gap in {finding.code for finding in observed.verdict.evidence_gaps}


@pytest.mark.parametrize("context", ["absent", "different_record", "foreign", "duplicate", "mixed_revision"])
def test_verdict_never_attributes_unmatched_risk_assessment_evidence(context):
    risk = _risk()
    assessment = _risk_assessment(risk)
    assessments = {
        "absent": None,
        "different_record": (replace(assessment, record=replace(risk, title="Another revision")),),
        "foreign": (replace(assessment, record=replace(risk, id="other")),),
        "duplicate": (assessment, assessment),
        "mixed_revision": (assessment, replace(assessment, record=replace(risk, title="Another revision"))),
    }[context]
    observed = _observe_risks((risk,), assessments)
    ask, = observed.verdict.leadership_asks
    assert ask.evidence_refs == ()
    assert "risk_assessment_unavailable" in {gap.code for gap in observed.verdict.evidence_gaps}
    assert observed.verdict.health == RiskLevel.HIGH


@pytest.mark.parametrize("risks", [None, ()])
def test_verdict_distinguishes_unloaded_from_loaded_empty_risk_input(risks):
    observed = _observe_risks(risks, warnings=("Synthetic unavailable source",))
    gaps = {gap.code for gap in observed.verdict.evidence_gaps}
    assert ("risk_register_asks_unevaluated" in gaps) is (risks is None)
    assert "risk_input_degraded" in gaps
    assert "risk_register_coverage_unqualified" in gaps
    assert observed.inputs.risk_warnings == ("Synthetic unavailable source",)
    assert observed.verdict.health == RiskLevel.UNKNOWN
    assert not observed.verdict.leadership_asks


@pytest.mark.parametrize("risks", [
    (_risk(), _risk()),
    (_risk(program_id="foreign"),),
    (_risk(id=""),),
])
def test_verdict_refuses_ambiguous_or_foreign_risk_inputs(risks):
    with pytest.raises(ConfigError, match="risk"):
        _observe_risks(risks)


def test_verdict_risk_inputs_and_asks_are_canonical_and_replayable():
    from src.core.program_verdict import evaluate_program_verdict

    risks = (_risk(), _risk(id="risk-2"))
    assessments = tuple(_risk_assessment(risk, fact_id=risk.id) for risk in risks)
    observed = _observe_risks(risks, assessments)
    assert _observe_risks(tuple(reversed(risks)), tuple(reversed(assessments))) == observed
    assert evaluate_program_verdict(observed.inputs) == observed.verdict
    assert hash_jsonable(observed.inputs) == observed.inputs_sha256
    assert {"RISK:risk-1", "RISK:risk-2"} <= set(observed.verdict.source_refs)
    assert len(observed.verdict.leadership_asks) == 2


def test_verdict_retains_stale_review_context_without_erasing_high_floor():
    from src.core.program_verdict import VerdictReason, observe_program_verdict

    snapshot = _snapshot()
    risk = _risk()
    observed = observe_program_verdict(
        _capture(snapshot), snapshot, (), risks=(risk,), stale_risk_ids=(risk.id,),
    )
    assert observed.inputs.stale_risk_ids == (risk.id,)
    assert observed.verdict.health == RiskLevel.HIGH
    assert VerdictReason("risk_review_stale", ("RISK:risk-1",)) in observed.verdict.evidence_gaps


def _milestone(**changes):
    from tests.unit.test_milestone_no_evidence import _milestone as build

    return replace(build(target_date=date(2026, 11, 10)), program_id="synthetic", **changes)


def _milestone_assessment(milestone, **changes):
    from src.core.models_v2 import MilestoneAssessment, MilestoneStatus

    return MilestoneAssessment(**{
        "milestone_id": milestone.id, "computed_health": MilestoneStatus.UNKNOWN,
        "blocked_criteria": (), "slip_probability": 0.5, "critical_path": False,
        "confidence": Confidence.NONE, "reasoning": "Synthetic", "coverage_gap": True,
        **changes,
    })


def _observe_milestones(milestones, assessments=None, warnings=()):
    from src.core.program_verdict import observe_program_verdict

    snapshot = _snapshot()
    return observe_program_verdict(
        _capture(snapshot), snapshot, (), milestones=milestones,
        milestone_assessments=assessments, milestone_warnings=warnings,
    )


@pytest.mark.parametrize(("status", "expected"), [
    ("missed", RiskLevel.HIGH), ("at_risk", RiskLevel.MEDIUM),
    ("on_track", RiskLevel.UNKNOWN), ("unknown", RiskLevel.UNKNOWN),
    ("completed", RiskLevel.UNKNOWN), ("deferred", RiskLevel.UNKNOWN),
])
@pytest.mark.parametrize("probability", [0.0, 0.5, 0.8, 1.0])
def test_milestone_observer_uses_computed_status_not_heuristic_probability(status, expected, probability):
    from src.core.models_v2 import MilestoneStatus

    milestone = _milestone()
    assessment = _milestone_assessment(
        milestone, computed_health=MilestoneStatus(status), slip_probability=probability,
    )
    observed = _observe_milestones((milestone,), (assessment,))
    assert observed.verdict.health == expected
    assert observed.verdict.schedule_risk == expected
    assert observed.verdict.confidence == Confidence.NONE
    assert observed.verdict.completion_phase == "unknown"
    assert "milestone_inputs_unqualified" in {gap.code for gap in observed.verdict.evidence_gaps}
    assert "milestones_unevaluated" not in {gap.code for gap in observed.verdict.evidence_gaps}


@pytest.mark.parametrize("offset", [-1, 0, 1])
@pytest.mark.parametrize("status", ["unknown", "on_track", "completed", "deferred"])
def test_milestone_overdue_floor_preserves_assessed_terminal_and_target_day(status, offset):
    from src.core.models_v2 import MilestoneStatus

    milestone = _milestone(target_date=NOW.date() + timedelta(days=offset))
    assessment = _milestone_assessment(milestone, computed_health=MilestoneStatus(status))
    observed = _observe_milestones((milestone,), (assessment,))
    expected = RiskLevel.MEDIUM if offset < 0 and status not in {"completed", "deferred"} else RiskLevel.UNKNOWN
    assert observed.verdict.schedule_risk == expected
    assert ("milestone_target_overdue" in {reason.code for reason in observed.verdict.reasons}) is (expected == RiskLevel.MEDIUM)


@pytest.mark.parametrize("context", ["absent", "missing", "duplicate", "orphan"])
def test_missing_or_ambiguous_milestone_assessment_cannot_supply_a_status_floor(context):
    from src.core.models_v2 import MilestoneStatus

    milestone = _milestone()
    assessment = _milestone_assessment(milestone, computed_health=MilestoneStatus.MISSED)
    assessments = {
        "absent": None, "missing": (), "duplicate": (assessment, assessment),
        "orphan": (replace(assessment, milestone_id="other"),),
    }[context]
    observed = _observe_milestones((milestone,), assessments)
    assert observed.verdict.schedule_risk == RiskLevel.UNKNOWN
    assert "milestone_assessment_unavailable" in {gap.code for gap in observed.verdict.evidence_gaps}
    if context == "orphan":
        assert "milestone_assessment_unbound" in {gap.code for gap in observed.verdict.evidence_gaps}


def test_missing_milestone_assessment_does_not_hide_overdue_unverified_completion():
    from src.core.models_v2 import MilestoneStatus

    milestone = _milestone(target_date=NOW.date() - timedelta(days=1), status=MilestoneStatus.COMPLETED)
    observed = _observe_milestones((milestone,))
    assert observed.verdict.schedule_risk == RiskLevel.MEDIUM
    assert observed.verdict.completion_phase == "unknown"
    deferred = _observe_milestones((replace(milestone, status=MilestoneStatus.DEFERRED),))
    assert deferred.verdict.schedule_risk == RiskLevel.UNKNOWN


@pytest.mark.parametrize("milestones", [None, ()])
def test_milestone_observer_retains_empty_absent_and_degraded_input_distinction(milestones):
    observed = _observe_milestones(milestones, warnings=("Synthetic unavailable milestone source",))
    assert observed.inputs.milestones == milestones
    assert observed.inputs.milestone_warnings == ("Synthetic unavailable milestone source",)
    gaps = {gap.code for gap in observed.verdict.evidence_gaps}
    assert ("milestones_unevaluated" in gaps) is (milestones is None)
    assert {"milestone_inputs_unqualified", "milestone_input_degraded"} <= gaps


@pytest.mark.parametrize("damage", ["blank", "duplicate", "foreign"])
def test_milestone_observer_refuses_invalid_record_identity(damage):
    milestone = _milestone()
    records = {
        "blank": (replace(milestone, id=" "),),
        "duplicate": (milestone, milestone),
        "foreign": (replace(milestone, program_id="foreign"),),
    }[damage]
    with pytest.raises(ConfigError, match="milestone"):
        _observe_milestones(records)


def test_milestone_observation_retains_coverage_and_replays_canonically():
    from src.core.models_v2 import MilestoneStatus
    from src.core.program_verdict import VerdictReason, evaluate_program_verdict

    milestones = (_milestone(id="m1"), _milestone(id="m2"))
    assessments = tuple(_milestone_assessment(
        milestone, computed_health=MilestoneStatus.AT_RISK, coverage_gap=index == 0,
    ) for index, milestone in enumerate(milestones))
    observed = _observe_milestones(milestones, assessments)
    assert _observe_milestones(tuple(reversed(milestones)), tuple(reversed(assessments))) == observed
    assert observed.verdict == evaluate_program_verdict(observed.inputs)
    assert hash_jsonable(observed.inputs) == observed.inputs_sha256
    assert VerdictReason("milestone_evidence_incomplete", ("MILESTONE:m1",)) in observed.verdict.evidence_gaps
    assert VerdictReason("milestone_evidence_incomplete", ("MILESTONE:m2",)) not in observed.verdict.evidence_gaps
    assert {"MILESTONE:m1", "MILESTONE:m2"} <= set(observed.verdict.source_refs)
