from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Callable, Literal
import uuid

from src.core.digest_cache import build_digest_model
from src.core.delivery_date_evaluator import DeliveryDateSnapshot, evaluate_delivery_date_hypothesis
from src.core.hypothesis_models import (
    AssertionEvaluation,
    ChallengeKind,
    ChallengeSeverity,
    ChallengeState,
    CompositeAssertion,
    DigestDelta,
    Hypothesis,
    HypothesisKind,
    HypothesisStatus,
    MetricFreshnessEntry,
    RealityChallenge,
    RealityDigestModel,
    StaleHypothesisEntry,
    TelemetryAssertion,
)
from src.core.metric_models import MetricAggregation, MetricDefinition, MetricObservation, MetricQualityState, MetricSourceBinding
from src.core.reality_store import RealityStore
from src.core.source_models import MaintenanceWindow
from src.core.telemetry_assertion_evaluator import EvaluationResult, evaluate_assertion


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    program_id: str
    as_of: datetime
    evaluations_written: int
    challenges_opened: int
    hypotheses_challenged: int
    digest: RealityDigestModel
    failed_hypothesis_id: str | None = None


DeliveryDateSnapshotProvider = Callable[[int], DeliveryDateSnapshot | None]


@dataclass(frozen=True, slots=True)
class StalenessEvaluation:
    effective_staleness_hours: float
    hours_since_last_observation: float | None
    severity: ChallengeSeverity
    staleness_reason: Literal["no_observations", "source_degraded", "review_due_passed"]
    note: str


def reconcile_reality(
    *,
    store: RealityStore,
    as_of: datetime,
    l1_observations_written: int = 0,
    delivery_date_snapshot_provider: DeliveryDateSnapshotProvider | None = None,
    metric_definitions_by_id: Mapping[str, MetricDefinition] | None = None,
    expected_gather_cadence_hours: float | None = None,
    included_metric_tiers: frozenset[str] | None = None,
    write_digest_cache: bool = True,
) -> ReconcileResult:
    previous_digest_row = store.read_digest_cache_row() if write_digest_cache else None
    active_hypotheses = store.list_active_hypotheses()
    assertions = {assertion.id: assertion for assertion in store.list_active_telemetry_assertions()}
    composite_assertions = {assertion.id: assertion for assertion in store.list_active_composite_assertions()}
    active_maintenance_windows = store.list_active_maintenance_windows(as_of)
    existing_challenges = {
        _challenge_key(challenge.hypothesis_id, challenge.assertion_id, challenge.composite_assertion_id): challenge
        for challenge in store.list_active_challenges(include_snoozed=True)
        if challenge.assertion_id is not None or challenge.composite_assertion_id is not None
    }
    existing_delivery_challenges = {
        challenge.hypothesis_id: challenge
        for challenge in store.list_active_challenges(include_snoozed=True)
        if challenge.assertion_id is None and challenge.challenge_kind == ChallengeKind.DELIVERY_DATE
    }
    if not active_hypotheses:
        digest_delta = _build_digest_delta(store, previous_digest_row=previous_digest_row, as_of=as_of)
        digest = build_digest_model(
            program_id=store.program_id,
            as_of=as_of,
            confirmed_count=0,
            challenged_count=0,
            stale_count=0,
            proposed_count=0,
            recovered_count=0,
            source_freshness=(),
            open_challenges=(),
            suppressed_during_maintenance=(),
            delta_since_last_digest=digest_delta,
        )
        if write_digest_cache:
            store.write_digest_cache(digest)
        return ReconcileResult(
            program_id=store.program_id,
            as_of=as_of,
            evaluations_written=0,
            challenges_opened=0,
            hypotheses_challenged=0,
            digest=digest,
        )

    evaluations: list[AssertionEvaluation] = []
    new_challenges: list[RealityChallenge] = []
    resolved_challenge_ids: list[str] = []
    changed_hypothesis_ids: list[str] = []
    freshness_entries: list[MetricFreshnessEntry] = []
    stale_entries: list[StaleHypothesisEntry] = []

    for hypothesis in active_hypotheses:
        if hypothesis.telemetry_assertion_id is None:
            if hypothesis.composite_assertion_id is not None:
                _reconcile_composite_assertion_hypothesis(
                    store=store,
                    hypothesis=hypothesis,
                    composite_assertions=composite_assertions,
                    assertions=assertions,
                    as_of=as_of,
                    existing_challenges=existing_challenges,
                    evaluations=evaluations,
                    new_challenges=new_challenges,
                    resolved_challenge_ids=resolved_challenge_ids,
                    changed_hypothesis_ids=changed_hypothesis_ids,
                )
                continue
            if included_metric_tiers is not None:
                continue
            if (
                hypothesis.kind == HypothesisKind.DELIVERY_DATE
                and hypothesis.linked_ado_item_id is not None
                and delivery_date_snapshot_provider is not None
            ):
                snapshot = delivery_date_snapshot_provider(hypothesis.linked_ado_item_id)
                if snapshot is not None:
                    delivery_result = evaluate_delivery_date_hypothesis(hypothesis, snapshot, as_of)
                    existing_delivery_challenge = existing_delivery_challenges.get(hypothesis.id)
                    if not delivery_result.violated:
                        if existing_delivery_challenge is not None:
                            store.update_challenge_state(
                                existing_delivery_challenge.id,
                                ChallengeState.RESOLVED,
                                as_of,
                                reason="delivery_date_recovered_on_reconcile",
                            )
                            resolved_challenge_ids.append(existing_delivery_challenge.id)
                            existing_delivery_challenges.pop(hypothesis.id, None)
                        elif delivery_result.closed_late and store.get_latest_challenge_for_hypothesis(
                            hypothesis.id,
                            challenge_kind=ChallengeKind.DELIVERY_DATE,
                        ) is None:
                            new_challenges.append(
                                _build_delivery_date_challenge(
                                    challenge_id=str(uuid.uuid4()),
                                    store=store,
                                    hypothesis=hypothesis,
                                    result=delivery_result,
                                    as_of=as_of,
                                    current_state=ChallengeState.RESOLVED,
                                )
                            )
                        continue

                    severity = delivery_result.severity or ChallengeSeverity.INFO
                    if existing_delivery_challenge is not None:
                        if existing_delivery_challenge.current_state == ChallengeState.SNOOZED:
                            if (
                                existing_delivery_challenge.snoozed_until is not None
                                and existing_delivery_challenge.snoozed_until > as_of
                            ):
                                continue
                        else:
                            continue

                    challenge_id = str(uuid.uuid4())
                    if existing_delivery_challenge is not None and existing_delivery_challenge.current_state == ChallengeState.SNOOZED:
                        store.update_challenge_state(
                            existing_delivery_challenge.id,
                            ChallengeState.RESOLVED,
                            as_of,
                            reason=f"snooze_expired_replaced_by:{challenge_id}",
                        )
                        resolved_challenge_ids.append(existing_delivery_challenge.id)
                        existing_delivery_challenges.pop(hypothesis.id, None)

                    challenge = _build_delivery_date_challenge(
                        challenge_id=challenge_id,
                        store=store,
                        hypothesis=hypothesis,
                        result=delivery_result,
                        as_of=as_of,
                        current_state=ChallengeState.OPEN,
                    )
                    new_challenges.append(challenge)
                    existing_delivery_challenges[hypothesis.id] = challenge
                    if severity == ChallengeSeverity.ALERT and hypothesis.status != HypothesisStatus.CHALLENGED:
                        store.update_hypothesis_status(hypothesis.id, HypothesisStatus.CHALLENGED, as_of)
                        changed_hypothesis_ids.append(hypothesis.id)
            continue
        assertion = assertions.get(hypothesis.telemetry_assertion_id)
        if assertion is None:
            continue
        bindings = store.list_active_metric_source_bindings(metric_id=assertion.metric_id)
        observations = _select_observations(store, assertion.metric_id, assertion.window, as_of)
        latest_observation = observations[-1] if observations else None
        selected_binding = _select_binding_for_reconcile(bindings, latest_observation)
        metric_definition = None
        if metric_definitions_by_id is not None:
            metric_definition = metric_definitions_by_id.get(assertion.metric_id)
        staleness = _evaluate_staleness(
            metric_definition=metric_definition,
            latest_observation=latest_observation,
            as_of=as_of,
            expected_gather_cadence_hours=expected_gather_cadence_hours,
        )
        freshness_entries.append(
            _build_freshness_entry(
                assertion.metric_id,
                latest_observation,
                as_of,
                stale=staleness is not None,
            )
        )
        existing_challenge = existing_challenges.get(_challenge_key(hypothesis.id, assertion.id))
        binding_health = None
        if selected_binding is not None:
            binding_health = store.get_binding_health(selected_binding.binding_id)

        if included_metric_tiers is not None and _metric_freshness_tier(metric_definition) not in included_metric_tiers:
            continue

        if selected_binding is not None and not selected_binding.validated:
            _record_data_loss_evaluation(
                evaluations,
                store,
                hypothesis.id,
                assertion.id,
                latest_observation,
                as_of,
                note="binding not validated",
            )
            _emit_data_loss_challenge(
                store=store,
                existing_challenges=existing_challenges,
                resolved_challenge_ids=resolved_challenge_ids,
                new_challenges=new_challenges,
                hypothesis_id=hypothesis.id,
                assertion_id=assertion.id,
                observation=latest_observation,
                binding=selected_binding,
                as_of=as_of,
                severity=ChallengeSeverity.INFO,
                note="binding not validated - run vertex admin metric validate",
            )
            continue

        if binding_health is not None and binding_health.is_degraded:
            _record_data_loss_evaluation(
                evaluations,
                store,
                hypothesis.id,
                assertion.id,
                latest_observation,
                as_of,
                note="binding degraded",
            )
            _emit_data_loss_challenge(
                store=store,
                existing_challenges=existing_challenges,
                resolved_challenge_ids=resolved_challenge_ids,
                new_challenges=new_challenges,
                hypothesis_id=hypothesis.id,
                assertion_id=assertion.id,
                observation=latest_observation,
                binding=selected_binding,
                as_of=as_of,
                severity=ChallengeSeverity.WARN,
                note="binding degraded",
            )
            continue

        if staleness is not None:
            stale_entries.append(
                _build_stale_hypothesis_entry(
                    hypothesis=hypothesis,
                    latest_observation=latest_observation,
                    staleness=staleness,
                    as_of=as_of,
                )
            )
            maintenance_window = _find_matching_maintenance_window(
                active_maintenance_windows,
                metric_id=assertion.metric_id,
                workstream_id=hypothesis.workstream_id,
                source_binding_id=latest_observation.source_binding_id if latest_observation else None,
                challenge_kind=ChallengeKind.STALENESS,
            )
            if maintenance_window is not None:
                evaluations.append(
                    AssertionEvaluation(
                        id=str(uuid.uuid4()),
                        program_id=store.program_id,
                        hypothesis_id=hypothesis.id,
                        assertion_id=assertion.id,
                        observation_id=latest_observation.observation_id if latest_observation else None,
                        evaluated_at=as_of,
                        violated=True,
                        value_num=staleness.hours_since_last_observation,
                        expected_value=staleness.effective_staleness_hours,
                        quality_state=latest_observation.quality_state if latest_observation else None,
                        note=f"suppressed_by_maintenance:{maintenance_window.id}",
                    )
                )
                store.record_suppression_event(
                    hypothesis_id=hypothesis.id,
                    assertion_id=assertion.id,
                    observation_id=latest_observation.observation_id if latest_observation else None,
                    would_be_kind=ChallengeKind.STALENESS,
                    would_be_severity=staleness.severity,
                    maintenance_window_id=maintenance_window.id,
                    suppressed_at=as_of,
                )
                continue

            evaluations.append(
                AssertionEvaluation(
                    id=str(uuid.uuid4()),
                    program_id=store.program_id,
                    hypothesis_id=hypothesis.id,
                    assertion_id=assertion.id,
                    observation_id=latest_observation.observation_id if latest_observation else None,
                    evaluated_at=as_of,
                    violated=True,
                    value_num=staleness.hours_since_last_observation,
                    expected_value=staleness.effective_staleness_hours,
                    quality_state=latest_observation.quality_state if latest_observation else None,
                    note=staleness.note,
                )
            )
            if _is_assertion_cooldown_active(
                store,
                hypothesis_id=hypothesis.id,
                assertion_id=assertion.id,
                cooldown_hours=assertion.cooldown_hours,
                as_of=as_of,
            ):
                continue
            if existing_challenge is not None:
                if existing_challenge.current_state == ChallengeState.SNOOZED:
                    if existing_challenge.snoozed_until is not None and existing_challenge.snoozed_until > as_of:
                        if hypothesis.status != HypothesisStatus.STALE:
                            store.update_hypothesis_status(hypothesis.id, HypothesisStatus.STALE, as_of)
                        continue
                    replacement_id = str(uuid.uuid4())
                    store.update_challenge_state(
                        existing_challenge.id,
                        ChallengeState.RESOLVED,
                        as_of,
                        reason=f"snooze_expired_replaced_by:{replacement_id}",
                    )
                    resolved_challenge_ids.append(existing_challenge.id)
                    existing_challenges.pop(_challenge_key(hypothesis.id, assertion.id), None)
                    challenge_id = replacement_id
                elif existing_challenge.challenge_kind == ChallengeKind.STALENESS:
                    if hypothesis.status != HypothesisStatus.STALE:
                        store.update_hypothesis_status(hypothesis.id, HypothesisStatus.STALE, as_of)
                    continue
                else:
                    store.update_challenge_state(
                        existing_challenge.id,
                        ChallengeState.RESOLVED,
                        as_of,
                        reason="replaced_by_staleness",
                    )
                    resolved_challenge_ids.append(existing_challenge.id)
                    existing_challenges.pop(_challenge_key(hypothesis.id, assertion.id), None)
                    challenge_id = str(uuid.uuid4())
            else:
                challenge_id = str(uuid.uuid4())
            challenge = _build_staleness_challenge(
                challenge_id=challenge_id,
                store=store,
                assertion_id=assertion.id,
                hypothesis=hypothesis,
                latest_observation=latest_observation,
                staleness=staleness,
                as_of=as_of,
            )
            new_challenges.append(challenge)
            existing_challenges[_challenge_key(hypothesis.id, assertion.id)] = challenge
            if hypothesis.status != HypothesisStatus.STALE:
                store.update_hypothesis_status(hypothesis.id, HypothesisStatus.STALE, as_of)
            continue

        if existing_challenge is not None and existing_challenge.challenge_kind == ChallengeKind.STALENESS:
            store.update_challenge_state(
                existing_challenge.id,
                ChallengeState.RESOLVED,
                as_of,
                reason="staleness_recovered_on_reconcile",
            )
            resolved_challenge_ids.append(existing_challenge.id)
            existing_challenges.pop(_challenge_key(hypothesis.id, assertion.id), None)
            existing_challenge = None
        if hypothesis.status == HypothesisStatus.STALE:
            store.update_hypothesis_status(hypothesis.id, HypothesisStatus.CONFIRMED, as_of)
        if existing_challenge is not None and existing_challenge.challenge_kind == ChallengeKind.DATA_LOSS:
            store.update_challenge_state(
                existing_challenge.id,
                ChallengeState.RESOLVED,
                as_of,
                reason="data_loss_recovered_on_reconcile",
            )
            resolved_challenge_ids.append(existing_challenge.id)
            existing_challenges.pop(_challenge_key(hypothesis.id, assertion.id), None)
            existing_challenge = None

        result = evaluate_assertion(assertion, observations, as_of, binding_health)
        if not result.violated:
            evaluations.append(
                AssertionEvaluation(
                    id=str(uuid.uuid4()),
                    program_id=store.program_id,
                    hypothesis_id=hypothesis.id,
                    assertion_id=assertion.id,
                    observation_id=latest_observation.observation_id if latest_observation else None,
                    evaluated_at=as_of,
                    violated=False,
                    value_num=result.observed_value,
                    expected_value=result.expected_value,
                    quality_state=latest_observation.quality_state if latest_observation else None,
                    note=result.rationale,
                )
            )
            if existing_challenge is not None:
                store.update_challenge_state(
                    existing_challenge.id,
                    ChallengeState.RESOLVED,
                    as_of,
                    reason="recovered_on_reconcile",
                )
                resolved_challenge_ids.append(existing_challenge.id)
                existing_challenges.pop(_challenge_key(hypothesis.id, assertion.id), None)
            continue
        severity = _derive_severity(assertion.severity_override, result)
        maintenance_window = _find_matching_maintenance_window(
            active_maintenance_windows,
            metric_id=assertion.metric_id,
            workstream_id=hypothesis.workstream_id,
            source_binding_id=latest_observation.source_binding_id if latest_observation else None,
            challenge_kind=ChallengeKind.THRESHOLD_BREACH,
        )
        if maintenance_window is not None:
            evaluations.append(
                AssertionEvaluation(
                    id=str(uuid.uuid4()),
                    program_id=store.program_id,
                    hypothesis_id=hypothesis.id,
                    assertion_id=assertion.id,
                    observation_id=latest_observation.observation_id if latest_observation else None,
                    evaluated_at=as_of,
                    violated=True,
                    value_num=result.observed_value,
                    expected_value=result.expected_value,
                    quality_state=latest_observation.quality_state if latest_observation else None,
                    note=f"suppressed_by_maintenance:{maintenance_window.id}",
                )
            )
            store.record_suppression_event(
                hypothesis_id=hypothesis.id,
                assertion_id=assertion.id,
                observation_id=latest_observation.observation_id if latest_observation else None,
                would_be_kind=ChallengeKind.THRESHOLD_BREACH,
                would_be_severity=severity,
                maintenance_window_id=maintenance_window.id,
                suppressed_at=as_of,
            )
            continue
        evaluations.append(
            AssertionEvaluation(
                id=str(uuid.uuid4()),
                program_id=store.program_id,
                hypothesis_id=hypothesis.id,
                assertion_id=assertion.id,
                observation_id=latest_observation.observation_id if latest_observation else None,
                evaluated_at=as_of,
                violated=True,
                value_num=result.observed_value,
                expected_value=result.expected_value,
                quality_state=latest_observation.quality_state if latest_observation else None,
                note=result.rationale,
            )
        )
        if _is_assertion_cooldown_active(
            store,
            hypothesis_id=hypothesis.id,
            assertion_id=assertion.id,
            cooldown_hours=assertion.cooldown_hours,
            as_of=as_of,
        ):
            continue
        if existing_challenge is not None:
            if existing_challenge.current_state == ChallengeState.SNOOZED:
                if existing_challenge.snoozed_until is not None and existing_challenge.snoozed_until > as_of:
                    continue
            else:
                continue
        challenge_id = str(uuid.uuid4())
        if existing_challenge is not None and existing_challenge.current_state == ChallengeState.SNOOZED:
            store.update_challenge_state(
                existing_challenge.id,
                ChallengeState.RESOLVED,
                as_of,
                reason=f"snooze_expired_replaced_by:{challenge_id}",
            )
            resolved_challenge_ids.append(existing_challenge.id)
            existing_challenges.pop(_challenge_key(hypothesis.id, assertion.id), None)
        challenge = RealityChallenge(
            id=challenge_id,
            program_id=store.program_id,
            hypothesis_id=hypothesis.id,
            assertion_id=assertion.id,
            observation_id=latest_observation.observation_id if latest_observation else None,
            challenge_kind=ChallengeKind.THRESHOLD_BREACH,
            observed_value=result.observed_value,
            expected_value=result.expected_value,
            delta_magnitude=result.delta_magnitude,
            severity=severity,
            source=f"metric:{assertion.metric_id}",
            detected_at=as_of,
            note=result.rationale,
            evidence_url=_format_evidence_url(latest_observation, assertion.metric_id, store),
            current_state=ChallengeState.OPEN,
        )
        new_challenges.append(challenge)
        existing_challenges[_challenge_key(hypothesis.id, assertion.id)] = challenge
        if severity == ChallengeSeverity.ALERT and hypothesis.status != HypothesisStatus.CHALLENGED:
            store.update_hypothesis_status(hypothesis.id, HypothesisStatus.CHALLENGED, as_of)
            changed_hypothesis_ids.append(hypothesis.id)

    _apply_dependency_cascades(
        store=store,
        active_hypotheses=active_hypotheses,
        as_of=as_of,
        existing_challenges=existing_challenges,
        evaluations=evaluations,
        new_challenges=new_challenges,
        resolved_challenge_ids=resolved_challenge_ids,
        changed_hypothesis_ids=changed_hypothesis_ids,
    )

    for evaluation in evaluations:
        store.append_assertion_evaluation(evaluation)
    for challenge in new_challenges:
        store.upsert_challenge(challenge)

    active_hypotheses_after = store.list_active_hypotheses(include_proposed=True)
    open_challenges = store.list_open_challenges()
    suppression_summaries = store.list_suppression_summaries(as_of)
    digest_delta = _build_digest_delta(store, previous_digest_row=previous_digest_row, as_of=as_of)
    digest = build_digest_model(
        program_id=store.program_id,
        as_of=as_of,
        confirmed_count=sum(1 for item in active_hypotheses_after if item.status == HypothesisStatus.CONFIRMED),
        challenged_count=sum(1 for item in active_hypotheses_after if item.status == HypothesisStatus.CHALLENGED),
        stale_count=sum(1 for item in active_hypotheses_after if item.status == HypothesisStatus.STALE),
        proposed_count=sum(1 for item in active_hypotheses_after if item.status == HypothesisStatus.PROPOSED),
        recovered_count=0,
        source_freshness=tuple(_dedupe_freshness_entries(freshness_entries)),
        open_challenges=open_challenges,
        stale_entries=tuple(stale_entries),
        suppressed_during_maintenance=suppression_summaries,
        delta_since_last_digest=digest_delta,
    )
    if write_digest_cache:
        store.write_digest_cache(digest)
    return ReconcileResult(
        program_id=store.program_id,
        as_of=as_of,
        evaluations_written=len(evaluations),
        challenges_opened=len(new_challenges),
        hypotheses_challenged=len(changed_hypothesis_ids),
        digest=digest,
    )


def _build_digest_delta(
    store: RealityStore,
    *,
    previous_digest_row,
    as_of: datetime,
) -> DigestDelta | None:
    if previous_digest_row is None:
        return None
    previous_as_of_raw = previous_digest_row["as_of"]
    if previous_as_of_raw is None:
        return None
    previous_as_of = datetime.fromisoformat(str(previous_as_of_raw))
    if previous_as_of >= as_of:
        return None
    return store.build_digest_delta(since=previous_as_of, to=as_of)


def _metric_freshness_tier(metric_definition: MetricDefinition | None) -> str:
    if metric_definition is None:
        return "warm"
    return str(metric_definition.freshness_tier)


def _select_observations(
    store: RealityStore,
    metric_id: str,
    window,
    as_of: datetime,
) -> tuple[MetricObservation, ...]:
    observations = store.list_metric_observations(metric_id)
    cutoff = as_of - timedelta(days=window.days)
    filtered = [
        observation
        for observation in observations
        if observation.observed_at <= as_of and observation.observed_at >= cutoff
    ]
    if window.dimensions:
        expected_dimensions = {name: value for name, value in window.dimensions}
        filtered = [
            observation
            for observation in filtered
            if _dimensions_match(observation.dimensions_json, expected_dimensions)
        ]
    filtered = _apply_manual_precedence(filtered)
    if window.aggregation != MetricAggregation.LAST:
        raise ValueError(f"Unsupported M0 window aggregation: {window.aggregation.value}")
    return tuple(sorted(filtered, key=lambda observation: observation.observed_at))


def _apply_manual_precedence(observations: list[MetricObservation]) -> list[MetricObservation]:
    chosen_by_identity: dict[tuple[str, datetime], MetricObservation] = {}
    for observation in observations:
        identity = (observation.dimensions_json, observation.measurement_period_end)
        current = chosen_by_identity.get(identity)
        if current is None:
            chosen_by_identity[identity] = observation
            continue
        chosen_by_identity[identity] = _prefer_observation(current, observation)
    return list(chosen_by_identity.values())


def _prefer_observation(left: MetricObservation, right: MetricObservation) -> MetricObservation:
    left_pinned_manual = left.quality_state == MetricQualityState.MANUAL and left.is_pinned
    right_pinned_manual = right.quality_state == MetricQualityState.MANUAL and right.is_pinned
    if left_pinned_manual != right_pinned_manual:
        return left if left_pinned_manual else right
    left_manual = left.quality_state == MetricQualityState.MANUAL
    right_manual = right.quality_state == MetricQualityState.MANUAL
    if left_manual == right_manual:
        return left if left.observed_at >= right.observed_at else right
    manual = left if left_manual else right
    kusto = right if left_manual else left
    if kusto.observed_at >= manual.observed_at - timedelta(hours=24):
        return kusto
    return manual


def _is_assertion_cooldown_active(
    store: RealityStore,
    *,
    hypothesis_id: str,
    assertion_id: str,
    cooldown_hours: int,
    as_of: datetime,
) -> bool:
    if cooldown_hours <= 0:
        return False
    latest_challenge = store.get_latest_challenge_for_assertion(hypothesis_id, assertion_id)
    if latest_challenge is None:
        return False
    if latest_challenge.current_state not in {ChallengeState.DISMISSED, ChallengeState.RESOLVED}:
        return False
    terminal_at = latest_challenge.state_changed_at or latest_challenge.last_event_at
    if terminal_at is None:
        return False
    return as_of - terminal_at < timedelta(hours=cooldown_hours)


def _challenge_key(
    hypothesis_id: str,
    assertion_id: str | None,
    composite_assertion_id: str | None = None,
) -> tuple[str, str | None, str | None]:
    return (hypothesis_id, assertion_id, composite_assertion_id)


def _is_composite_assertion_cooldown_active(
    store: RealityStore,
    *,
    hypothesis_id: str,
    composite_assertion_id: str,
    cooldown_hours: int,
    as_of: datetime,
) -> bool:
    if cooldown_hours <= 0:
        return False
    latest_challenge = store.get_latest_challenge_for_composite_assertion(hypothesis_id, composite_assertion_id)
    if latest_challenge is None:
        return False
    if latest_challenge.current_state not in {ChallengeState.DISMISSED, ChallengeState.RESOLVED}:
        return False
    terminal_at = latest_challenge.state_changed_at or latest_challenge.last_event_at
    if terminal_at is None:
        return False
    return as_of - terminal_at < timedelta(hours=cooldown_hours)


def _reconcile_composite_assertion_hypothesis(
    *,
    store: RealityStore,
    hypothesis,
    composite_assertions: dict[str, CompositeAssertion],
    assertions: dict[str, TelemetryAssertion],
    as_of: datetime,
    existing_challenges: dict[tuple[str, str | None, str | None], RealityChallenge],
    evaluations: list[AssertionEvaluation],
    new_challenges: list[RealityChallenge],
    resolved_challenge_ids: list[str],
    changed_hypothesis_ids: list[str],
) -> None:
    composite_id = hypothesis.composite_assertion_id
    if composite_id is None:
        return
    composite = composite_assertions.get(composite_id)
    if composite is None:
        return

    child_results: list[EvaluationResult] = []
    for child_assertion_id in composite.child_assertion_ids:
        assertion = assertions.get(child_assertion_id)
        if assertion is None:
            child_results.append(
                EvaluationResult(
                    status="insufficient_data",
                    violated=False,
                    delta_magnitude=None,
                    observed_value=None,
                    expected_value=None,
                    rationale=f"child assertion {child_assertion_id} is missing or archived",
                )
            )
            continue
        child_results.append(_evaluate_child_assertion_for_composite(store=store, assertion=assertion, as_of=as_of))

    combined_result = _combine_composite_results(composite, child_results, as_of=as_of)
    evaluations.append(
        AssertionEvaluation(
            id=str(uuid.uuid4()),
            program_id=store.program_id,
            hypothesis_id=hypothesis.id,
            assertion_id=None,
            observation_id=None,
            evaluated_at=as_of,
            violated=combined_result.violated,
            value_num=combined_result.observed_value,
            expected_value=combined_result.expected_value,
            quality_state=None,
            note=combined_result.rationale,
            composite_assertion_id=composite.id,
        )
    )

    existing_challenge = existing_challenges.get(_challenge_key(hypothesis.id, None, composite.id))
    if not combined_result.violated:
        if existing_challenge is not None:
            store.update_challenge_state(
                existing_challenge.id,
                ChallengeState.RESOLVED,
                as_of,
                reason="composite_recovered_on_reconcile",
            )
            resolved_challenge_ids.append(existing_challenge.id)
            existing_challenges.pop(_challenge_key(hypothesis.id, None, composite.id), None)
        return

    if _is_composite_assertion_cooldown_active(
        store,
        hypothesis_id=hypothesis.id,
        composite_assertion_id=composite.id,
        cooldown_hours=max((assertions[child_id].cooldown_hours for child_id in composite.child_assertion_ids if child_id in assertions), default=24),
        as_of=as_of,
    ):
        return
    if existing_challenge is not None:
        if existing_challenge.current_state == ChallengeState.SNOOZED:
            if existing_challenge.snoozed_until is not None and existing_challenge.snoozed_until > as_of:
                return
        else:
            return
    challenge_id = str(uuid.uuid4())
    if existing_challenge is not None and existing_challenge.current_state == ChallengeState.SNOOZED:
        store.update_challenge_state(
            existing_challenge.id,
            ChallengeState.RESOLVED,
            as_of,
            reason=f"snooze_expired_replaced_by:{challenge_id}",
        )
        resolved_challenge_ids.append(existing_challenge.id)
        existing_challenges.pop(_challenge_key(hypothesis.id, None, composite.id), None)
    severity = _derive_composite_severity(child_results)
    challenge = RealityChallenge(
        id=challenge_id,
        program_id=store.program_id,
        hypothesis_id=hypothesis.id,
        assertion_id=None,
        observation_id=None,
        challenge_kind=ChallengeKind.THRESHOLD_BREACH,
        observed_value=combined_result.observed_value,
        expected_value=combined_result.expected_value,
        delta_magnitude=combined_result.delta_magnitude,
        severity=severity,
        source=f"composite:{composite.id}",
        detected_at=as_of,
        note=combined_result.rationale,
        current_state=ChallengeState.OPEN,
        composite_assertion_id=composite.id,
    )
    new_challenges.append(challenge)
    existing_challenges[_challenge_key(hypothesis.id, None, composite.id)] = challenge
    if severity == ChallengeSeverity.ALERT and hypothesis.status != HypothesisStatus.CHALLENGED:
        store.update_hypothesis_status(hypothesis.id, HypothesisStatus.CHALLENGED, as_of)
        changed_hypothesis_ids.append(hypothesis.id)


def _evaluate_child_assertion_for_composite(
    *,
    store: RealityStore,
    assertion: TelemetryAssertion,
    as_of: datetime,
) -> EvaluationResult:
    bindings = store.list_active_metric_source_bindings(metric_id=assertion.metric_id)
    observations = _select_observations(store, assertion.metric_id, assertion.window, as_of)
    latest_observation = observations[-1] if observations else None
    selected_binding = _select_binding_for_reconcile(bindings, latest_observation)
    binding_health = store.get_binding_health(selected_binding.binding_id) if selected_binding is not None else None
    if selected_binding is not None and not selected_binding.validated:
        return EvaluationResult(
            status="insufficient_data",
            violated=False,
            delta_magnitude=None,
            observed_value=latest_observation.value_num if latest_observation is not None else None,
            expected_value=None,
            rationale="binding not validated",
        )
    return evaluate_assertion(assertion, observations, as_of, binding_health)


def _combine_composite_results(
    composite: CompositeAssertion,
    child_results: list[EvaluationResult],
    *,
    as_of: datetime,
) -> EvaluationResult:
    if composite.operator.value == "and":
        if any(result.violated for result in child_results):
            violated_children = [result for result in child_results if result.violated]
            worst = max(violated_children, key=lambda result: result.delta_magnitude or 0.0)
            return EvaluationResult(
                status="violated",
                violated=True,
                delta_magnitude=worst.delta_magnitude,
                observed_value=worst.observed_value,
                expected_value=worst.expected_value,
                rationale=f"Composite AND violated at {as_of.isoformat()}: " + "; ".join(result.rationale for result in violated_children),
            )
        if any(result.status == "degraded_source" for result in child_results):
            return EvaluationResult("degraded_source", False, None, None, None, "Composite AND degraded by child source state")
        if any(result.status == "insufficient_data" for result in child_results):
            return EvaluationResult("insufficient_data", False, None, None, None, "Composite AND has insufficient child data")
        return EvaluationResult("passed", False, 0.0, None, None, f"Composite AND passed at {as_of.isoformat()}")

    if any(result.status == "passed" for result in child_results):
        return EvaluationResult("passed", False, 0.0, None, None, f"Composite OR passed at {as_of.isoformat()}")
    if all(result.violated for result in child_results):
        worst = max(child_results, key=lambda result: result.delta_magnitude or 0.0)
        return EvaluationResult(
            status="violated",
            violated=True,
            delta_magnitude=worst.delta_magnitude,
            observed_value=worst.observed_value,
            expected_value=worst.expected_value,
            rationale=f"Composite OR violated at {as_of.isoformat()}: all children violated",
        )
    if any(result.status == "degraded_source" for result in child_results):
        return EvaluationResult("degraded_source", False, None, None, None, "Composite OR degraded by child source state")
    return EvaluationResult("insufficient_data", False, None, None, None, "Composite OR has insufficient child data")


def _derive_composite_severity(child_results: list[EvaluationResult]) -> ChallengeSeverity:
    highest = ChallengeSeverity.INFO
    for result in child_results:
        if result.delta_magnitude is None:
            continue
        if result.delta_magnitude >= 4.0:
            return ChallengeSeverity.ALERT
        if result.delta_magnitude >= 2.0:
            highest = ChallengeSeverity.WARN
    return highest


def _build_freshness_entry(
    metric_id: str,
    observation: MetricObservation | None,
    as_of: datetime,
    *,
    stale: bool = False,
) -> MetricFreshnessEntry:
    if observation is None:
        return MetricFreshnessEntry(
            metric_id=metric_id,
            last_observed_at=None,
            quality_state=MetricQualityState.ZERO_ROWS,
            hours_since_last_observation=None,
        )
    delta_hours = (as_of - observation.observed_at).total_seconds() / 3600.0
    return MetricFreshnessEntry(
        metric_id=metric_id,
        last_observed_at=observation.observed_at,
        quality_state=MetricQualityState.STALE_SOURCE if stale else observation.quality_state,
        hours_since_last_observation=delta_hours,
    )


def _dedupe_freshness_entries(entries: list[MetricFreshnessEntry]) -> tuple[MetricFreshnessEntry, ...]:
    latest_by_metric: dict[str, MetricFreshnessEntry] = {}
    for entry in entries:
        current = latest_by_metric.get(entry.metric_id)
        if current is None:
            latest_by_metric[entry.metric_id] = entry
            continue
        current_dt = current.last_observed_at or datetime.fromtimestamp(0, tz=timezone.utc)
        entry_dt = entry.last_observed_at or datetime.fromtimestamp(0, tz=timezone.utc)
        if entry_dt >= current_dt:
            latest_by_metric[entry.metric_id] = entry
    return tuple(sorted(latest_by_metric.values(), key=lambda entry: entry.metric_id))


def _dimensions_match(dimensions_json: str, expected_dimensions: dict[str, str]) -> bool:
    import json

    payload = json.loads(dimensions_json)
    return all(str(payload.get(name)) == value for name, value in expected_dimensions.items())


def _derive_severity(severity_override: str | None, result: EvaluationResult) -> ChallengeSeverity:
    if severity_override is not None:
        return ChallengeSeverity.from_string(severity_override)
    if result.delta_magnitude is None:
        return ChallengeSeverity.INFO
    if result.delta_magnitude >= 4.0:
        return ChallengeSeverity.ALERT
    if result.delta_magnitude >= 2.0:
        return ChallengeSeverity.WARN
    return ChallengeSeverity.INFO


def _find_matching_maintenance_window(
    windows: tuple[MaintenanceWindow, ...],
    *,
    metric_id: str,
    workstream_id: str | None,
    source_binding_id: str | None,
    challenge_kind: ChallengeKind,
) -> MaintenanceWindow | None:
    for window in windows:
        if challenge_kind.value not in window.suppress_kinds:
            continue
        if window.scope_kind == "program" and window.scope_value in {"*", ""}:
            return window
        if window.scope_kind == "metric" and window.scope_value == metric_id:
            return window
        if window.scope_kind == "binding" and source_binding_id is not None and window.scope_value == source_binding_id:
            return window
        if window.scope_kind == "workstream" and workstream_id is not None and window.scope_value == workstream_id:
            return window
    return None


def _evaluate_staleness(
    *,
    metric_definition: MetricDefinition | None,
    latest_observation: MetricObservation | None,
    as_of: datetime,
    expected_gather_cadence_hours: float | None,
) -> StalenessEvaluation | None:
    if metric_definition is None or expected_gather_cadence_hours is None:
        return None
    effective_staleness_hours = _compute_effective_staleness_hours(metric_definition, expected_gather_cadence_hours)
    severity = ChallengeSeverity.WARN if metric_definition.freshness_tier == "hot" else ChallengeSeverity.INFO
    if latest_observation is None:
        return StalenessEvaluation(
            effective_staleness_hours=effective_staleness_hours,
            hours_since_last_observation=None,
            severity=severity,
            staleness_reason="no_observations",
            note=f"No observations available for metric {metric_definition.id}",
        )
    hours_since_last_observation = (as_of - latest_observation.observed_at).total_seconds() / 3600.0
    if hours_since_last_observation <= effective_staleness_hours:
        return None
    return StalenessEvaluation(
        effective_staleness_hours=effective_staleness_hours,
        hours_since_last_observation=hours_since_last_observation,
        severity=severity,
        staleness_reason="no_observations",
        note=(
            f"Latest observation for metric {metric_definition.id} is {hours_since_last_observation:.1f}h old; "
            f"threshold is {effective_staleness_hours:.1f}h"
        ),
    )


def _compute_effective_staleness_hours(
    metric_definition: MetricDefinition,
    expected_gather_cadence_hours: float,
) -> float:
    tier_window_hours = {"hot": 2.0, "warm": 48.0, "cold": 24.0 * 14.0}[metric_definition.freshness_tier]
    return max(2.0 * tier_window_hours, 1.5 * expected_gather_cadence_hours) + (
        metric_definition.expected_pipeline_lag_minutes / 60.0
    )


def _build_stale_hypothesis_entry(
    *,
    hypothesis,
    latest_observation: MetricObservation | None,
    staleness: StalenessEvaluation,
    as_of: datetime,
) -> StaleHypothesisEntry:
    confirmed_at = hypothesis.confirmed_at or hypothesis.proposed_at or as_of
    return StaleHypothesisEntry(
        hypothesis_id=hypothesis.id,
        confirmed_at=confirmed_at,
        days_since_confirmation=max(0, (as_of.date() - confirmed_at.date()).days),
        last_observation_at=latest_observation.observed_at if latest_observation else None,
        staleness_reason=staleness.staleness_reason,
    )


def _build_staleness_challenge(
    *,
    challenge_id: str,
    store: RealityStore,
    assertion_id: str,
    hypothesis,
    latest_observation: MetricObservation | None,
    staleness: StalenessEvaluation,
    as_of: datetime,
) -> RealityChallenge:
    return RealityChallenge(
        id=challenge_id,
        program_id=store.program_id,
        hypothesis_id=hypothesis.id,
        assertion_id=assertion_id,
        observation_id=latest_observation.observation_id if latest_observation else None,
        challenge_kind=ChallengeKind.STALENESS,
        observed_value=staleness.hours_since_last_observation,
        expected_value=staleness.effective_staleness_hours,
        delta_magnitude=(
            None
            if staleness.hours_since_last_observation is None or staleness.effective_staleness_hours <= 0
            else staleness.hours_since_last_observation / staleness.effective_staleness_hours
        ),
        severity=staleness.severity,
        source=f"metric:{assertion_id}",
        detected_at=as_of,
        note=staleness.note,
        evidence_url=_format_evidence_url(latest_observation, assertion_id, store),
        current_state=ChallengeState.OPEN,
    )


def _build_delivery_date_challenge(
    *,
    challenge_id: str,
    store: RealityStore,
    hypothesis,
    result,
    as_of: datetime,
    current_state: ChallengeState,
) -> RealityChallenge:
    return RealityChallenge(
        id=challenge_id,
        program_id=store.program_id,
        hypothesis_id=hypothesis.id,
        assertion_id=None,
        observation_id=None,
        challenge_kind=ChallengeKind.DELIVERY_DATE,
        observed_value=float(result.days_past_due),
        expected_value=0.0,
        delta_magnitude=result.delta_magnitude,
        severity=result.severity or ChallengeSeverity.INFO,
        source=f"ado:{hypothesis.linked_ado_item_id}",
        detected_at=as_of,
        note=result.note,
        ado_current_target=result.ado_current_target,
        current_state=current_state,
    )


def _select_binding_for_reconcile(
    bindings: tuple[MetricSourceBinding, ...],
    latest_observation: MetricObservation | None,
) -> MetricSourceBinding | None:
    if latest_observation is not None and latest_observation.source_binding_id is not None:
        for binding in bindings:
            if binding.binding_id == latest_observation.source_binding_id:
                return binding
    for binding in bindings:
        if binding.validated:
            return binding
    return bindings[0] if bindings else None


def _record_data_loss_evaluation(
    evaluations: list[AssertionEvaluation],
    store: RealityStore,
    hypothesis_id: str,
    assertion_id: str,
    observation: MetricObservation | None,
    as_of: datetime,
    *,
    note: str,
) -> None:
    evaluations.append(
        AssertionEvaluation(
            id=str(uuid.uuid4()),
            program_id=store.program_id,
            hypothesis_id=hypothesis_id,
            assertion_id=assertion_id,
            observation_id=observation.observation_id if observation else None,
            evaluated_at=as_of,
            violated=True,
            value_num=observation.value_num if observation else None,
            expected_value=None,
            quality_state=observation.quality_state if observation else None,
            note=note,
        )
    )


def _apply_dependency_cascades(
    *,
    store: RealityStore,
    active_hypotheses: tuple[Hypothesis, ...],
    as_of: datetime,
    existing_challenges: dict[tuple[str, str | None, str | None], RealityChallenge],
    evaluations: list[AssertionEvaluation],
    new_challenges: list[RealityChallenge],
    resolved_challenge_ids: list[str],
    changed_hypothesis_ids: list[str],
) -> None:
    current_hypotheses = {
        hypothesis.id: (store.get_hypothesis(hypothesis.id) or hypothesis)
        for hypothesis in active_hypotheses
    }
    active_dependency_challenges = {
        challenge.hypothesis_id: challenge
        for challenge in existing_challenges.values()
        if challenge.current_state is not ChallengeState.SNOOZED
    }

    for hypothesis in current_hypotheses.values():
        if hypothesis.telemetry_assertion_id is None or not hypothesis.depends_on:
            continue

        upstream_failures = []
        for dependency_id in hypothesis.depends_on:
            dependency = current_hypotheses.get(dependency_id) or store.get_hypothesis(dependency_id)
            if dependency is None:
                continue
            dependency_challenge = active_dependency_challenges.get(dependency.id)
            if dependency_challenge is None:
                continue
            upstream_failures.append((dependency, dependency_challenge))

        existing_challenge = existing_challenges.get(_challenge_key(hypothesis.id, hypothesis.telemetry_assertion_id))
        if not upstream_failures:
            if existing_challenge is not None and existing_challenge.challenge_kind == ChallengeKind.DEPENDENCY_CASCADE:
                store.update_challenge_state(
                    existing_challenge.id,
                    ChallengeState.RESOLVED,
                    as_of,
                    reason="dependency_cascade_recovered_on_reconcile",
                )
                resolved_challenge_ids.append(existing_challenge.id)
                existing_challenges.pop(_challenge_key(hypothesis.id, hypothesis.telemetry_assertion_id), None)
            continue

        if existing_challenge is not None:
            if existing_challenge.current_state == ChallengeState.SNOOZED:
                if existing_challenge.snoozed_until is not None and existing_challenge.snoozed_until > as_of:
                    continue
            elif existing_challenge.challenge_kind != ChallengeKind.DEPENDENCY_CASCADE:
                continue

        note = _build_dependency_cascade_note(upstream_failures)
        severity = _derive_dependency_cascade_severity(upstream_failures)
        evaluations.append(
            AssertionEvaluation(
                id=str(uuid.uuid4()),
                program_id=store.program_id,
                hypothesis_id=hypothesis.id,
                assertion_id=hypothesis.telemetry_assertion_id,
                observation_id=None,
                evaluated_at=as_of,
                violated=True,
                value_num=None,
                expected_value=None,
                quality_state=None,
                note=note,
            )
        )
        if existing_challenge is not None and existing_challenge.challenge_kind == ChallengeKind.DEPENDENCY_CASCADE:
            if severity == ChallengeSeverity.ALERT and hypothesis.status != HypothesisStatus.CHALLENGED:
                store.update_hypothesis_status(hypothesis.id, HypothesisStatus.CHALLENGED, as_of)
                changed_hypothesis_ids.append(hypothesis.id)
            continue

        challenge_id = str(uuid.uuid4())
        if existing_challenge is not None and existing_challenge.current_state == ChallengeState.SNOOZED:
            store.update_challenge_state(
                existing_challenge.id,
                ChallengeState.RESOLVED,
                as_of,
                reason=f"snooze_expired_replaced_by:{challenge_id}",
            )
            resolved_challenge_ids.append(existing_challenge.id)
            existing_challenges.pop(_challenge_key(hypothesis.id, hypothesis.telemetry_assertion_id), None)

        cascade_challenge = _build_dependency_cascade_challenge(
            store=store,
            hypothesis_id=hypothesis.id,
            assertion_id=hypothesis.telemetry_assertion_id,
            challenge_id=challenge_id,
            upstream_failures=tuple(upstream_failures),
            severity=severity,
            note=note,
            as_of=as_of,
        )
        new_challenges.append(cascade_challenge)
        existing_challenges[_challenge_key(hypothesis.id, hypothesis.telemetry_assertion_id)] = cascade_challenge
        if severity == ChallengeSeverity.ALERT and hypothesis.status != HypothesisStatus.CHALLENGED:
            store.update_hypothesis_status(hypothesis.id, HypothesisStatus.CHALLENGED, as_of)
            changed_hypothesis_ids.append(hypothesis.id)


def _derive_dependency_cascade_severity(
    upstream_failures: list[tuple[Hypothesis, RealityChallenge]],
) -> ChallengeSeverity:
    severity_rank = {
        ChallengeSeverity.INFO: 1,
        ChallengeSeverity.WARN: 2,
        ChallengeSeverity.ALERT: 3,
    }
    highest = ChallengeSeverity.INFO
    for _, challenge in upstream_failures:
        if severity_rank[challenge.severity] > severity_rank[highest]:
            highest = challenge.severity
    return highest


def _build_dependency_cascade_note(
    upstream_failures: list[tuple[Hypothesis, RealityChallenge]],
) -> str:
    return "Blocked by dependency cascade from " + ", ".join(
        f"{dependency.short_id} ({challenge.challenge_kind.value})"
        for dependency, challenge in upstream_failures
    )


def _build_dependency_cascade_challenge(
    *,
    store: RealityStore,
    hypothesis_id: str,
    assertion_id: str,
    challenge_id: str,
    upstream_failures: tuple[tuple[Hypothesis, RealityChallenge], ...],
    severity: ChallengeSeverity,
    note: str,
    as_of: datetime,
) -> RealityChallenge:
    source_refs = ",".join(dependency.short_id for dependency, _ in upstream_failures)
    evidence_url = next(
        (challenge.evidence_url for _, challenge in upstream_failures if challenge.evidence_url is not None),
        None,
    )
    return RealityChallenge(
        id=challenge_id,
        program_id=store.program_id,
        hypothesis_id=hypothesis_id,
        assertion_id=assertion_id,
        observation_id=None,
        challenge_kind=ChallengeKind.DEPENDENCY_CASCADE,
        observed_value=None,
        expected_value=None,
        delta_magnitude=None,
        severity=severity,
        source=f"cascade:{source_refs}",
        detected_at=as_of,
        note=note,
        evidence_url=evidence_url,
        current_state=ChallengeState.OPEN,
    )


def _emit_data_loss_challenge(
    *,
    store: RealityStore,
    existing_challenges: dict[tuple[str, str | None, str | None], RealityChallenge],
    resolved_challenge_ids: list[str],
    new_challenges: list[RealityChallenge],
    hypothesis_id: str,
    assertion_id: str,
    observation: MetricObservation | None,
    binding: MetricSourceBinding | None,
    as_of: datetime,
    severity: ChallengeSeverity,
    note: str,
) -> None:
    existing_challenge = existing_challenges.get(_challenge_key(hypothesis_id, assertion_id))
    if existing_challenge is not None:
        if existing_challenge.current_state == ChallengeState.SNOOZED:
            if existing_challenge.snoozed_until is not None and existing_challenge.snoozed_until > as_of:
                return
        elif existing_challenge.challenge_kind == ChallengeKind.DATA_LOSS:
            return
        else:
            store.update_challenge_state(
                existing_challenge.id,
                ChallengeState.RESOLVED,
                as_of,
                reason="replaced_by_data_loss",
            )
            resolved_challenge_ids.append(existing_challenge.id)
            existing_challenges.pop(_challenge_key(hypothesis_id, assertion_id), None)
            existing_challenge = None

    challenge_id = str(uuid.uuid4())
    if existing_challenge is not None and existing_challenge.current_state == ChallengeState.SNOOZED:
        store.update_challenge_state(
            existing_challenge.id,
            ChallengeState.RESOLVED,
            as_of,
            reason=f"snooze_expired_replaced_by:{challenge_id}",
        )
        resolved_challenge_ids.append(existing_challenge.id)
        existing_challenges.pop(_challenge_key(hypothesis_id, assertion_id), None)

    challenge = RealityChallenge(
        id=challenge_id,
        program_id=store.program_id,
        hypothesis_id=hypothesis_id,
        assertion_id=assertion_id,
        observation_id=observation.observation_id if observation else None,
        challenge_kind=ChallengeKind.DATA_LOSS,
        observed_value=observation.value_num if observation else None,
        expected_value=None,
        delta_magnitude=None,
        severity=severity,
        source=f"binding:{binding.binding_id}" if binding is not None else f"assertion:{assertion_id}",
        detected_at=as_of,
        note=note,
        current_state=ChallengeState.OPEN,
    )
    new_challenges.append(challenge)
    existing_challenges[_challenge_key(hypothesis_id, assertion_id)] = challenge


def _format_evidence_url(
    observation: MetricObservation | None,
    metric_id: str,
    store: RealityStore,
) -> str | None:
    if observation is None or observation.source_binding_id is None:
        return None
    binding = store.get_metric_source_binding(observation.source_binding_id)
    if binding is None or binding.evidence_url_template is None:
        return None
    values = {
        "metric_id": metric_id,
        "program_id": store.program_id,
        "cluster": binding.cluster or "",
        "database": binding.database or "",
        "binding_id": binding.binding_id,
        "observed_value": "" if observation.value_num is None else f"{observation.value_num:g}",
        "expected_value": "",
        "detected_at_iso": observation.observed_at.isoformat(),
    }
    rendered = binding.evidence_url_template
    for key, value in values.items():
        rendered = rendered.replace(f"{{{key}}}", value)
    return re.sub(r"\{[^{}]+\}", "", rendered)