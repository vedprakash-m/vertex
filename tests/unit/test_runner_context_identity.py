from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

import pytest

from src.ai.structured_feature_runner import (
    BatchProviderResult, ContextBuildResult, FeatureOutcomeStatus, RunnerContext, StructuredFeatureRunner,
    call_ceiling_ledger_path, deterministic_result_cache_path, released_result_cache_path,
)
from src.ai.tiered_router import RouteOutcome, TierDecision, TierResult
from src.core.exceptions import StateError
from tests.unit.test_structured_feature_runner import _compiled, _context, _spec


@pytest.mark.parametrize(
    "field", ["program_id", "run_id", "policy_version", "snapshot_id", "principal", "classification"],
)
@pytest.mark.parametrize("value", [None, "", "  ", 17])
def test_runner_context_requires_explicit_nonblank_identity(field, value) -> None:
    fields = dict(
        program_id="acme", run_id="run", policy_version="v1",
        snapshot_id="snap", principal="operator", classification="internal",
    )
    fields[field] = value
    with pytest.raises(StateError, match=field):
        RunnerContext(**fields)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("field", ["program_id", "feature", "prompt_version"])
def test_foreign_compilation_blocks_before_cache_reservation_or_provider(tmp_path, field, batch) -> None:
    compiled = _compiled()
    manifest = replace(compiled.manifest, feature="test_feature")
    compiled = replace(compiled, manifest=replace(manifest, **{field: "foreign"}))
    provider = Mock(return_value=BatchProviderResult(
        responses={"one": "must not release", "two": "must not release"},
        failed_item_ids=(),
    ) if batch else "must not release")
    cache = Mock()
    ledger = Mock()
    decisions: list[TierDecision] = []
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True,
        released_cache=cache, ceiling_ledger=ledger, deterministic_cache=Mock(),
        decision_sink=decisions.append,
    )
    build = lambda _: ContextBuildResult(compiled=compiled)
    spec = _spec(
        tmp_path, build_context=build, call_provider=provider,
        item_id=lambda request: request, build_batch_context=build, call_provider_batch=provider,
        request_hash=lambda request: request, deployment_fingerprint="test",
        validator_version="v1", serialize_response=str, deserialize_response=str,
    )

    outcomes = runner.run_batch(spec, ["one", "two"]) if batch else {"one": runner.run(spec, "one")}

    assert set(outcomes) == ({"one", "two"} if batch else {"one"})
    for outcome in outcomes.values():
        assert outcome.status == FeatureOutcomeStatus.BLOCKED
        assert outcome.value is None
        assert outcome.retryable is False
        assert outcome.next_action
        assert outcome.limitations
        assert all("foreign" not in reason for reason in outcome.limitations)
    provider.assert_not_called()
    cache.get.assert_not_called()
    cache.put.assert_not_called()
    ledger.reserve_call.assert_not_called()
    assert len(decisions) == 1
    assert decisions[0].outcome == RouteOutcome.SKIPPED
    assert decisions[0].frontier_called is False


def test_ineligible_batch_preserves_local_hits_without_context_or_provider(tmp_path) -> None:
    provider = Mock(return_value=BatchProviderResult(
        responses={"one": "must not release", "two": "must not release"}, failed_item_ids=(),
    ))
    build = Mock(return_value=ContextBuildResult(compiled=_compiled()))
    ledger = Mock()
    decisions: list[TierDecision] = []
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=False,
        released_cache=Mock(), ceiling_ledger=ledger, deterministic_cache=Mock(),
        decision_sink=decisions.append,
    )
    spec = _spec(
        tmp_path, item_id=lambda request: request,
        deterministic_handler=lambda request: TierResult("local", 1.0) if request == "local" else None,
        build_batch_context=build, call_provider_batch=provider,
    )

    outcomes = runner.run_batch(spec, ["local", "one", "two"])

    assert set(outcomes) == {"local", "one", "two"}
    assert outcomes["local"].status == FeatureOutcomeStatus.RELEASED
    assert outcomes["local"].value == "local"
    for key in ("one", "two"):
        assert outcomes[key].status == FeatureOutcomeStatus.DETERMINISTIC_FALLBACK
        assert outcomes[key].value == "fallback-value"
    provider.assert_not_called()
    build.assert_not_called()
    ledger.reserve_call.assert_not_called()
    assert any(decision.outcome == RouteOutcome.FRONTIER_BLOCKED for decision in decisions)
    assert all(decision.frontier_called is False for decision in decisions)


@pytest.mark.parametrize(
    "program", ["..", r"..\outside", r"inside\outside", r"Q:\outside", "/outside", "wild*", "line\nbreak", "trailing."],
)
@pytest.mark.parametrize(
    "path_helper", [None, call_ceiling_ledger_path, released_result_cache_path, deterministic_result_cache_path],
)
def test_runner_program_identity_cannot_select_a_path(tmp_path, program, path_helper) -> None:
    with pytest.raises(StateError, match="program_id"):
        if path_helper is None:
            _context(tmp_path, program_id=program)
        else:
            path_helper(program, programs_root=tmp_path)
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("program", ["acme", "acme-beta", "acme_weekly", "Acme Demo"])
def test_valid_runner_program_paths_preserve_explicit_root_without_effects(tmp_path, program) -> None:
    assert _context(tmp_path, program_id=program).program_id == program
    for helper in (call_ceiling_ledger_path, released_result_cache_path, deterministic_result_cache_path):
        path = helper(program, programs_root=tmp_path)
        assert path.parent == tmp_path / program / "runtime"
    assert not list(tmp_path.iterdir())
