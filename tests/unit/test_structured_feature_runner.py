from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
import sqlite3
from threading import Barrier, Lock
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.ai.ai_mode import AIMode
from src.ai.client import AIClient, BudgetExceeded
from src.ai.cost_guard import CostGuard
from src.ai.llm_trace import AITraceContext
from src.ai.structured_feature_runner import (
    BatchProviderResult,
    CacheIdentity,
    CallCeilingExceeded,
    CallCeilingLedger,
    ContextBuildResult,
    DeterministicCacheIdentity,
    DeterministicResultCache,
    FeatureOutcome,
    FeatureOutcomeStatus,
    ReleasedResultCache,
    RunnerContext,
    StructuredFeatureRunner,
    StructuredFeatureSpec,
    ValidationResult,
    call_ceiling_ledger_path,
)
from src.ai.tiered_router import Tier, TierResult
from src.core.ai_telemetry import append_ai_telemetry
from src.core.context_compiler import CompiledContext, ContextCompileRejected, ContextManifest
from src.core.exceptions import StateError
from datetime import datetime, timezone


def _context(tmp_path: Path, **overrides) -> RunnerContext:
    base = dict(
        program_id="acme",
        run_id="run-1",
        policy_version="v1",
        snapshot_id="snap-1",
        principal="operator",
    )
    base.update(overrides)
    return RunnerContext(**base)


def _manifest() -> ContextManifest:
    return ContextManifest(
        schema_version="1",
        program_id="acme",
        feature="test_feature",
        prompt_version="v1",
        policy_version="v1",
        tokenizer_id="test",
        model_deployment=None,
        included_evidence_ids=(),
        excluded=(),
        source_distribution={},
        token_estimate_total=10,
        reserved_tokens=10,
        truncated=False,
        classification="internal",
        compile_ms=0.1,
        cache_key="key",
        context_hash="hash",
        compiled_at=datetime.now(timezone.utc),
    )


def _compiled() -> CompiledContext:
    return CompiledContext(prompt_text="prompt", included=(), excluded=(), manifest=_manifest())


def _fallback_released(value: str = "fallback-value") -> FeatureOutcome[str]:
    return FeatureOutcome(status=FeatureOutcomeStatus.RELEASED, value=value, tier=Tier.DETERMINISTIC)


def _spec(tmp_path: Path, **overrides) -> StructuredFeatureSpec[str, str]:
    base = dict(
        feature="test_feature",
        task_class="synthesis",
        prompt_version="v1",
        schema_version="1",
        response_type=str,
        deterministic_handler=None,
        local_router_name=None,
        fallback_handler=lambda request: _fallback_released(),
        semantic_validator=lambda request, response: ValidationResult(valid=True),
        build_context=None,
        call_provider=None,
        max_remote_calls_per_run=None,
        max_frontier_calls_per_run=None,
    )
    base.update(overrides)
    return StructuredFeatureSpec(**base)


def _runner(tmp_path: Path, *, frontier_eligible: bool = True, **kwargs) -> StructuredFeatureRunner:
    ledger = CallCeilingLedger(tmp_path / "ceilings.sqlite3")
    kwargs.setdefault("released_cache", ReleasedResultCache(tmp_path / "released_cache.sqlite3"))
    kwargs.setdefault("deterministic_cache", DeterministicResultCache(tmp_path / "deterministic_cache.sqlite3"))
    return StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=frontier_eligible,
        ceiling_ledger=ledger,
        **kwargs,
    )


def test_deterministic_handler_short_circuits_and_releases(tmp_path: Path) -> None:
    calls = {"provider": 0}

    def det(request: str) -> TierResult[str] | None:
        return TierResult(value="det-value", confidence=1.0)

    def provider(request: str, compiled: CompiledContext) -> str:
        calls["provider"] += 1
        return "should-not-be-called"

    spec = _spec(tmp_path, deterministic_handler=det, call_provider=provider, build_context=lambda r: ContextBuildResult(compiled=_compiled()))
    outcome = _runner(tmp_path).run(spec, "request")

    assert outcome.status == FeatureOutcomeStatus.RELEASED
    assert outcome.value == "det-value"
    assert outcome.tier == Tier.DETERMINISTIC
    assert calls["provider"] == 0


def test_local_router_hit_short_circuits_frontier(tmp_path: Path) -> None:
    class _FakeRouter:
        def route(self, request: str) -> TierResult[str] | None:
            return TierResult(value="local-value", confidence=0.95)

    spec = _spec(tmp_path, local_router_name="fake_router")
    outcome = _runner(tmp_path, local_routers={"fake_router": _FakeRouter()}).run(spec, "request")

    assert outcome.status == FeatureOutcomeStatus.RELEASED
    assert outcome.value == "local-value"
    assert outcome.tier == Tier.LOCAL


def test_local_router_abstains_falls_through_to_frontier(tmp_path: Path) -> None:
    class _AbstainingRouter:
        def route(self, request: str) -> TierResult[str] | None:
            return None

    def provider(request: str, compiled: CompiledContext) -> str:
        return "frontier-value"

    spec = _spec(
        tmp_path,
        local_router_name="fake_router",
        call_provider=provider,
        build_context=lambda r: ContextBuildResult(compiled=_compiled()),
    )
    outcome = _runner(tmp_path, local_routers={"fake_router": _AbstainingRouter()}).run(spec, "request")

    assert outcome.status == FeatureOutcomeStatus.RELEASED
    assert outcome.value == "frontier-value"
    assert outcome.tier == Tier.FRONTIER


def test_frontier_ineligible_falls_back(tmp_path: Path) -> None:
    spec = _spec(tmp_path)
    outcome = _runner(tmp_path, frontier_eligible=False).run(spec, "request")

    assert outcome.status == FeatureOutcomeStatus.DETERMINISTIC_FALLBACK
    assert outcome.value == "fallback-value"
    assert "frontier_ineligible_or_unconfigured" in outcome.limitations


def test_missing_build_context_or_call_provider_falls_back(tmp_path: Path) -> None:
    spec = _spec(tmp_path, build_context=None, call_provider=lambda r, c: "x")
    outcome = _runner(tmp_path).run(spec, "request")
    assert outcome.status == FeatureOutcomeStatus.DETERMINISTIC_FALLBACK


def test_fallback_returning_abstained_is_never_relabeled_as_released(tmp_path: Path) -> None:
    # RELO-W1.13: blocked/unavailable/partial/abstained can never masquerade
    # as released -- _fallback() only ever relabels a RELEASED outcome (to
    # DETERMINISTIC_FALLBACK); this non-ceiling fallback passes through untouched.
    spec = _spec(
        tmp_path,
        fallback_handler=lambda r: FeatureOutcome(
            status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE, next_action="No confident answer."
        ),
    )
    outcome = _runner(tmp_path, frontier_eligible=False).run(spec, "request")
    assert outcome.status == FeatureOutcomeStatus.ABSTAINED
    assert outcome.value is None


def test_fallback_returning_partial_is_never_relabeled_as_released(tmp_path: Path) -> None:
    spec = _spec(
        tmp_path,
        fallback_handler=lambda r: FeatureOutcome(
            status=FeatureOutcomeStatus.PARTIAL, value="partial-value", tier=Tier.DETERMINISTIC
        ),
    )
    outcome = _runner(tmp_path, frontier_eligible=False).run(spec, "request")
    assert outcome.status == FeatureOutcomeStatus.PARTIAL
    assert outcome.value == "partial-value"


def test_context_compile_rejected_returns_evidence_gap(tmp_path: Path) -> None:
    def build_context(request: str) -> ContextBuildResult:
        raise ContextCompileRejected("reserved tokens exceed max_input_tokens")

    spec = _spec(tmp_path, build_context=build_context, call_provider=lambda r, c: "x")
    outcome = _runner(tmp_path).run(spec, "request")

    assert outcome.status == FeatureOutcomeStatus.EVIDENCE_GAP
    assert outcome.retryable is False
    assert "reserved tokens" in outcome.limitations[0]


def test_remote_call_ceiling_exhausted_falls_back_without_calling_provider(tmp_path: Path) -> None:
    calls = {"provider": 0}

    def provider(request: str, compiled: CompiledContext) -> str:
        calls["provider"] += 1
        return "value"

    spec = _spec(
        tmp_path,
        build_context=lambda r: ContextBuildResult(compiled=_compiled()),
        call_provider=provider,
        max_remote_calls_per_run=1,
    )
    runner = _runner(tmp_path)

    first = runner.run(spec, "request")
    assert first.status == FeatureOutcomeStatus.RELEASED
    assert calls["provider"] == 1

    second = runner.run(spec, "request")
    assert second.status == FeatureOutcomeStatus.DETERMINISTIC_FALLBACK
    assert "remote_cap_exhausted" in second.limitations
    assert calls["provider"] == 1  # never attempted a second time


def test_budget_exceeded_from_provider_falls_back_non_retryable(tmp_path: Path) -> None:
    def provider(request: str, compiled: CompiledContext) -> str:
        raise BudgetExceeded("run ceiling exceeded")

    spec = _spec(tmp_path, build_context=lambda r: ContextBuildResult(compiled=_compiled()), call_provider=provider)
    outcome = _runner(tmp_path).run(spec, "request")

    assert outcome.status == FeatureOutcomeStatus.DETERMINISTIC_FALLBACK
    assert "budget_exceeded" in outcome.limitations


def test_provider_exception_returns_unavailable_and_retryable(tmp_path: Path) -> None:
    def provider(request: str, compiled: CompiledContext) -> str:
        raise TimeoutError("provider timed out")

    spec = _spec(tmp_path, build_context=lambda r: ContextBuildResult(compiled=_compiled()), call_provider=provider)
    outcome = _runner(tmp_path).run(spec, "request")

    assert outcome.status == FeatureOutcomeStatus.UNAVAILABLE
    assert outcome.retryable is True
    assert outcome.value is None


def test_failed_validation_blocks_and_does_not_release(tmp_path: Path) -> None:
    def provider(request: str, compiled: CompiledContext) -> str:
        return "ungrounded-value"

    spec = _spec(
        tmp_path,
        build_context=lambda r: ContextBuildResult(compiled=_compiled()),
        call_provider=provider,
        semantic_validator=lambda request, response: ValidationResult(valid=False, reason="unsupported claim"),
    )
    outcome = _runner(tmp_path).run(spec, "request")

    assert outcome.status == FeatureOutcomeStatus.BLOCKED
    assert outcome.value is None
    assert outcome.retryable is False
    assert "unsupported claim" in outcome.limitations


def test_successful_frontier_call_releases_with_evidence_refs(tmp_path: Path) -> None:
    def provider(request: str, compiled: CompiledContext) -> str:
        return "released-value"

    spec = _spec(
        tmp_path,
        build_context=lambda r: ContextBuildResult(compiled=_compiled()),
        call_provider=provider,
        semantic_validator=lambda request, response: ValidationResult(valid=True, evidence_refs=("wi:1001",)),
    )
    outcome = _runner(tmp_path).run(spec, "request")

    assert outcome.status == FeatureOutcomeStatus.RELEASED
    assert outcome.value == "released-value"
    assert outcome.tier == Tier.FRONTIER
    assert outcome.evidence_refs == ("wi:1001",)
    assert outcome.released is True


def test_decision_sink_receives_every_outcome(tmp_path: Path) -> None:
    decisions = []
    spec = _spec(tmp_path, deterministic_handler=lambda r: TierResult(value="v", confidence=1.0))
    runner = _runner(tmp_path, decision_sink=decisions.append)
    runner.run(spec, "request")

    assert len(decisions) == 1
    assert decisions[0].feature == "test_feature"
    assert decisions[0].tier == Tier.DETERMINISTIC


# ---------------------------------------------------------------------------
# CallCeilingLedger: atomicity and ceiling semantics, tested directly.
# ---------------------------------------------------------------------------


def test_call_ceiling_ledger_allows_up_to_ceiling_then_raises(tmp_path: Path) -> None:
    ledger = CallCeilingLedger(tmp_path / "ceilings.sqlite3")
    ledger.reserve_call(scope_key="scope-1", kind="remote", ceiling=2)
    ledger.reserve_call(scope_key="scope-1", kind="remote", ceiling=2)

    with pytest.raises(CallCeilingExceeded):
        ledger.reserve_call(scope_key="scope-1", kind="remote", ceiling=2)


def test_call_ceiling_ledger_scopes_are_independent(tmp_path: Path) -> None:
    ledger = CallCeilingLedger(tmp_path / "ceilings.sqlite3")
    ledger.reserve_call(scope_key="scope-a", kind="remote", ceiling=1)
    ledger.reserve_call(scope_key="scope-b", kind="remote", ceiling=1)  # different scope, not exhausted

    with pytest.raises(CallCeilingExceeded):
        ledger.reserve_call(scope_key="scope-a", kind="remote", ceiling=1)


def test_call_ceiling_ledger_kinds_are_independent(tmp_path: Path) -> None:
    ledger = CallCeilingLedger(tmp_path / "ceilings.sqlite3")
    ledger.reserve_call(scope_key="scope-1", kind="remote", ceiling=1)
    ledger.reserve_call(scope_key="scope-1", kind="frontier", ceiling=1)  # different kind, not exhausted


def test_call_ceiling_ledger_none_ceiling_is_unbounded(tmp_path: Path) -> None:
    ledger = CallCeilingLedger(tmp_path / "ceilings.sqlite3")
    for _ in range(50):
        ledger.reserve_call(scope_key="scope-1", kind="remote", ceiling=None)


def test_call_ceiling_ledger_path_helper(tmp_path: Path) -> None:
    path = call_ceiling_ledger_path("acme", programs_root=tmp_path)
    assert path == tmp_path / "acme" / "runtime" / "ai_call_ceilings.sqlite3"


# ---------------------------------------------------------------------------
# RELO-W1.5: released-model-result cache (Sec 8.1.7).
# ---------------------------------------------------------------------------


def _cacheable_spec(tmp_path: Path, *, call_provider, **overrides) -> StructuredFeatureSpec[str, str]:
    base = dict(
        build_context=lambda r: ContextBuildResult(compiled=_compiled()),
        call_provider=call_provider,
        request_hash=lambda r: f"hash-{r}",
        deployment_fingerprint="gpt-5.4-mini@v1",
        validator_version="v1",
        serialize_response=lambda v: v,
        deserialize_response=lambda v: v,
    )
    base.update(overrides)
    return _spec(tmp_path, **base)


def test_second_call_with_same_identity_is_a_cache_hit_and_skips_provider(tmp_path: Path) -> None:
    calls = {"provider": 0}

    def provider(request: str, compiled) -> str:
        calls["provider"] += 1
        return f"released-{request}"

    spec = _cacheable_spec(tmp_path, call_provider=provider)
    runner = _runner(tmp_path)

    first = runner.run(spec, "request")
    assert first.status == FeatureOutcomeStatus.RELEASED
    assert first.tier == Tier.FRONTIER
    assert calls["provider"] == 1

    second = runner.run(spec, "request")
    assert second.status == FeatureOutcomeStatus.RELEASED
    assert second.value == "released-request"
    assert second.tier == Tier.CACHE
    assert calls["provider"] == 1  # never called again


def test_different_request_is_a_cache_miss(tmp_path: Path) -> None:
    calls = {"provider": 0}

    def provider(request: str, compiled) -> str:
        calls["provider"] += 1
        return f"released-{request}"

    spec = _cacheable_spec(tmp_path, call_provider=provider)
    runner = _runner(tmp_path)

    runner.run(spec, "request-a")
    runner.run(spec, "request-b")
    assert calls["provider"] == 2


def test_blocked_validation_never_writes_to_cache(tmp_path: Path) -> None:
    calls = {"provider": 0}

    def provider(request: str, compiled) -> str:
        calls["provider"] += 1
        return "ungrounded"

    spec = _cacheable_spec(
        tmp_path,
        call_provider=provider,
        semantic_validator=lambda request, response: ValidationResult(valid=False, reason="unsupported"),
    )
    runner = _runner(tmp_path)

    first = runner.run(spec, "request")
    assert first.status == FeatureOutcomeStatus.BLOCKED
    second = runner.run(spec, "request")
    assert second.status == FeatureOutcomeStatus.BLOCKED
    # Poisoned/blocked output must never be cached and replayed: the
    # provider is called again on the second attempt, not served from cache.
    assert calls["provider"] == 2


def test_cache_hit_does_not_consume_call_ceiling(tmp_path: Path) -> None:
    calls = {"provider": 0}

    def provider(request: str, compiled) -> str:
        calls["provider"] += 1
        return f"released-{request}"

    spec = _cacheable_spec(tmp_path, call_provider=provider, max_remote_calls_per_run=1)
    runner = _runner(tmp_path)

    runner.run(spec, "request")
    # A second real call would exceed max_remote_calls_per_run=1, but this
    # is a cache hit, so the ceiling is never consulted.
    second = runner.run(spec, "request")
    assert second.status == FeatureOutcomeStatus.RELEASED
    assert second.tier == Tier.CACHE


def test_cache_disabled_when_spec_omits_any_required_field(tmp_path: Path) -> None:
    calls = {"provider": 0}

    def provider(request: str, compiled) -> str:
        calls["provider"] += 1
        return "released"

    # request_hash omitted -> caching never engages, even though every
    # other cache field is present.
    spec = _cacheable_spec(tmp_path, call_provider=provider, request_hash=None)
    runner = _runner(tmp_path)

    runner.run(spec, "request")
    runner.run(spec, "request")
    assert calls["provider"] == 2


# ---------------------------------------------------------------------------
# CacheIdentity / ReleasedResultCache in isolation.
# ---------------------------------------------------------------------------


def _identity(**overrides) -> CacheIdentity:
    base = dict(
        program_id="acme",
        feature="f",
        request_hash="rh",
        context_hash="ch",
        prompt_version="v1",
        policy_version="pv1",
        compiler_policy_version="compiler.v1",
        deployment_fingerprint="dep1",
        tokenizer_id="tok1",
        response_schema_version="1",
        validator_version="v1",
        classification="internal",
        execution_classification="internal",
        principal="operator",
        snapshot_id="snap-1",
    )
    base.update(overrides)
    return CacheIdentity(**base)


def test_cache_identity_differs_by_every_field() -> None:
    base = _identity()
    fields = [
        "program_id", "feature", "request_hash", "context_hash", "prompt_version", "policy_version", "compiler_policy_version",
        "deployment_fingerprint", "tokenizer_id", "response_schema_version", "validator_version",
        "classification", "execution_classification", "principal", "snapshot_id",
    ]
    keys = {base.cache_key()}
    for field_name in fields:
        varied = _identity(**{field_name: "DIFFERENT"})
        keys.add(varied.cache_key())
    assert len(keys) == len(fields) + 1  # every variation produces a distinct key


def test_released_result_cache_round_trips(tmp_path: Path) -> None:
    cache = ReleasedResultCache(tmp_path / "cache.sqlite3")
    identity = _identity()

    assert cache.get(identity, deserialize=lambda v: v) is None

    cache.put(identity, "the-value", serialize=lambda v: v, released_content_hash="hash1", release_run_id="run-1")
    assert cache.get(identity, deserialize=lambda v: v) == "the-value"


def test_released_result_cache_authorization_recheck_on_read(tmp_path: Path) -> None:
    # Defense-in-depth (Sec 8.1.7): the stored principal/classification are
    # rechecked on every read, independent of cache_key construction. Prove
    # this by tampering with the stored row directly -- if get() only ever
    # trusted the key, this row would still be served.
    import sqlite3

    db_path = tmp_path / "cache.sqlite3"
    cache = ReleasedResultCache(db_path)
    identity = _identity(principal="operator")
    cache.put(identity, "secret-value", serialize=lambda v: v, released_content_hash="h", release_run_id="run-1")
    assert cache.get(identity, deserialize=lambda v: v) == "secret-value"

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE released_result_cache SET principal = 'someone-else' WHERE cache_key = ?",
            (identity.cache_key(),),
        )
        connection.commit()

    assert cache.get(identity, deserialize=lambda v: v) is None


def test_released_result_cache_evict_removes_entry(tmp_path: Path) -> None:
    cache = ReleasedResultCache(tmp_path / "cache.sqlite3")
    identity = _identity()
    cache.put(identity, "value", serialize=lambda v: v, released_content_hash="h", release_run_id="run-1")
    assert cache.get(identity, deserialize=lambda v: v) == "value"

    cache.evict(identity)
    assert cache.get(identity, deserialize=lambda v: v) is None


# ---------------------------------------------------------------------------
# RELO-W1.11: deterministic-result cache is a SEPARATE cache/identity from
# the released-model-result cache, consulted before the deterministic
# handler even runs -- "keyed without model context" (Sec 8.1.7).
# ---------------------------------------------------------------------------


def _det_cacheable_spec(tmp_path: Path, **overrides) -> StructuredFeatureSpec[str, str]:
    base = dict(
        deterministic_handler=None,
        request_hash=lambda r: f"hash-{r}",
        deterministic_engine_version="regex-v1",
        serialize_response=lambda v: v,
        deserialize_response=lambda v: v,
    )
    base.update(overrides)
    return _spec(tmp_path, **base)


def test_second_call_hits_deterministic_cache_and_skips_the_handler(tmp_path: Path) -> None:
    calls = {"handler": 0}

    def handler(request: str) -> TierResult[str] | None:
        calls["handler"] += 1
        return TierResult(value=f"det-{request}", confidence=1.0)

    spec = _det_cacheable_spec(tmp_path, deterministic_handler=handler)
    runner = _runner(tmp_path)

    first = runner.run(spec, "request")
    assert first.status == FeatureOutcomeStatus.RELEASED
    assert first.tier == Tier.DETERMINISTIC
    assert calls["handler"] == 1

    second = runner.run(spec, "request")
    assert second.status == FeatureOutcomeStatus.RELEASED
    assert second.value == "det-request"
    assert second.tier == Tier.CACHE
    assert calls["handler"] == 1  # handler never invoked again


def test_deterministic_cache_disabled_when_spec_omits_a_required_field(tmp_path: Path) -> None:
    calls = {"handler": 0}

    def handler(request: str) -> TierResult[str] | None:
        calls["handler"] += 1
        return TierResult(value="det-value", confidence=1.0)

    spec = _det_cacheable_spec(tmp_path, deterministic_handler=handler, deterministic_engine_version=None)
    runner = _runner(tmp_path)

    runner.run(spec, "request")
    runner.run(spec, "request")
    assert calls["handler"] == 2


def test_deterministic_and_released_caches_are_independent_of_each_other(tmp_path: Path) -> None:
    # A feature can be configured for both caches at once; they must not
    # collide or substitute for one another even for the "same" request,
    # because the deterministic path and the frontier path produce their
    # results through entirely different mechanisms.
    provider_calls = {"n": 0}

    def provider(request: str, compiled) -> str:
        provider_calls["n"] += 1
        return f"frontier-{request}"

    spec = _cacheable_spec(
        tmp_path,
        call_provider=provider,
        deterministic_handler=None,  # no deterministic path for this spec
        deterministic_engine_version="regex-v1",
    )
    runner = _runner(tmp_path)

    first = runner.run(spec, "request")
    assert first.tier == Tier.FRONTIER
    second = runner.run(spec, "request")
    assert second.tier == Tier.CACHE  # served from the RELEASED-model cache, not deterministic
    assert provider_calls["n"] == 1


def test_deterministic_cache_identity_differs_by_every_field() -> None:
    def det_identity(**overrides) -> DeterministicCacheIdentity:
        base = dict(
            program_id="acme", feature="f", request_hash="rh", snapshot_id="snap-1",
            deterministic_engine_version="v1", principal="operator", classification="internal",
        )
        base.update(overrides)
        return DeterministicCacheIdentity(**base)

    base = det_identity()
    fields = ["program_id", "feature", "request_hash", "snapshot_id", "deterministic_engine_version", "principal", "classification"]
    keys = {base.cache_key()}
    for field_name in fields:
        keys.add(det_identity(**{field_name: "DIFFERENT"}).cache_key())
    assert len(keys) == len(fields) + 1


def test_deterministic_result_cache_round_trips(tmp_path: Path) -> None:
    cache = DeterministicResultCache(tmp_path / "det_cache.sqlite3")
    identity = DeterministicCacheIdentity(
        program_id="acme", feature="f", request_hash="rh", snapshot_id="snap-1",
        deterministic_engine_version="v1", principal="operator", classification="internal",
    )
    assert cache.get(identity, deserialize=lambda v: v) is None
    cache.put(identity, "the-value", serialize=lambda v: v)
    assert cache.get(identity, deserialize=lambda v: v) == "the-value"


# ---------------------------------------------------------------------------
# RELO-W1.8: batching (Sec 8.1.9). Batch only cache misses; preserve
# per-item identity and partial failure; no batch exceeds a single call's
# compiler/schema/cost/output-item-count budgets.
# ---------------------------------------------------------------------------


def _batchable_spec(tmp_path: Path, *, call_provider_batch, **overrides) -> StructuredFeatureSpec[str, str]:
    base = dict(
        item_id=lambda r: r,
        build_batch_context=lambda rs: ContextBuildResult(compiled=_compiled()),
        call_provider_batch=call_provider_batch,
    )
    base.update(overrides)
    return _spec(tmp_path, **base)


def test_batch_falls_back_to_run_per_item_when_not_configured(tmp_path: Path) -> None:
    spec = _spec(tmp_path)  # no item_id/build_batch_context/call_provider_batch
    runner = _runner(tmp_path)

    results = runner.run_batch(spec, ["a", "b"])
    assert set(results.keys()) == {"0", "1"}
    assert all(outcome.status == FeatureOutcomeStatus.DETERMINISTIC_FALLBACK for outcome in results.values())


def test_batch_calls_provider_once_for_multiple_items(tmp_path: Path) -> None:
    batch_calls = {"n": 0}

    def call_provider_batch(requests: list[str], compiled) -> BatchProviderResult[str]:
        batch_calls["n"] += 1
        return BatchProviderResult(responses={r: f"released-{r}" for r in requests})

    spec = _batchable_spec(tmp_path, call_provider_batch=call_provider_batch)
    runner = _runner(tmp_path)

    results = runner.run_batch(spec, ["item-a", "item-b", "item-c"])
    assert batch_calls["n"] == 1
    assert results["item-a"].status == FeatureOutcomeStatus.RELEASED
    assert results["item-a"].value == "released-item-a"
    assert results["item-b"].value == "released-item-b"
    assert results["item-c"].value == "released-item-c"


def test_batch_preserves_per_item_partial_failure(tmp_path: Path) -> None:
    def call_provider_batch(requests: list[str], compiled) -> BatchProviderResult[str]:
        return BatchProviderResult(
            responses={r: f"released-{r}" for r in requests if r != "item-b"},
            failed_item_ids=("item-b",),
        )

    spec = _batchable_spec(tmp_path, call_provider_batch=call_provider_batch)
    runner = _runner(tmp_path)

    results = runner.run_batch(spec, ["item-a", "item-b", "item-c"])
    assert results["item-a"].status == FeatureOutcomeStatus.RELEASED
    assert results["item-b"].status == FeatureOutcomeStatus.UNAVAILABLE
    assert results["item-b"].retryable is True
    assert results["item-c"].status == FeatureOutcomeStatus.RELEASED


def test_batch_item_missing_from_response_without_being_listed_failed_is_unavailable(tmp_path: Path) -> None:
    # An item_id absent from BOTH responses and failed_item_ids must not be
    # silently dropped -- treated the same as an explicit failure.
    def call_provider_batch(requests: list[str], compiled) -> BatchProviderResult[str]:
        return BatchProviderResult(responses={"item-a": "released-item-a"})  # item-b omitted entirely

    spec = _batchable_spec(tmp_path, call_provider_batch=call_provider_batch)
    runner = _runner(tmp_path)

    results = runner.run_batch(spec, ["item-a", "item-b"])
    assert results["item-a"].status == FeatureOutcomeStatus.RELEASED
    assert results["item-b"].status == FeatureOutcomeStatus.UNAVAILABLE


def test_batch_respects_max_batch_size_by_chunking(tmp_path: Path) -> None:
    batch_calls = {"chunks": []}

    def call_provider_batch(requests: list[str], compiled) -> BatchProviderResult[str]:
        batch_calls["chunks"].append(list(requests))
        return BatchProviderResult(responses={r: f"released-{r}" for r in requests})

    spec = _batchable_spec(tmp_path, call_provider_batch=call_provider_batch, max_batch_size=2)
    runner = _runner(tmp_path)

    results = runner.run_batch(spec, ["a", "b", "c", "d", "e"])
    assert len(batch_calls["chunks"]) == 3  # 2 + 2 + 1
    assert all(len(chunk) <= 2 for chunk in batch_calls["chunks"])
    assert all(outcome.status == FeatureOutcomeStatus.RELEASED for outcome in results.values())


def test_batch_skips_items_already_resolved_by_deterministic_handler(tmp_path: Path) -> None:
    def det_handler(request: str) -> TierResult[str] | None:
        return TierResult(value=f"det-{request}", confidence=1.0) if request == "det-item" else None

    batch_calls = {"seen": []}

    def call_provider_batch(requests: list[str], compiled) -> BatchProviderResult[str]:
        batch_calls["seen"].extend(requests)
        return BatchProviderResult(responses={r: f"released-{r}" for r in requests})

    spec = _batchable_spec(tmp_path, call_provider_batch=call_provider_batch, deterministic_handler=det_handler)
    runner = _runner(tmp_path)

    results = runner.run_batch(spec, ["det-item", "frontier-item"])
    assert results["det-item"].status == FeatureOutcomeStatus.RELEASED
    assert results["det-item"].tier == Tier.DETERMINISTIC
    assert results["det-item"].value == "det-det-item"
    assert "det-item" not in batch_calls["seen"]  # never entered the batch call
    assert batch_calls["seen"] == ["frontier-item"]


def test_batch_per_item_validation_failure_is_blocked_others_still_released(tmp_path: Path) -> None:
    def call_provider_batch(requests: list[str], compiled) -> BatchProviderResult[str]:
        return BatchProviderResult(responses={r: f"released-{r}" for r in requests})

    def validator(request: str, response: str) -> ValidationResult:
        return ValidationResult(valid=(request != "item-b"), reason="unsupported claim")

    spec = _batchable_spec(tmp_path, call_provider_batch=call_provider_batch, semantic_validator=validator)
    runner = _runner(tmp_path)

    results = runner.run_batch(spec, ["item-a", "item-b"])
    assert results["item-a"].status == FeatureOutcomeStatus.RELEASED
    assert results["item-b"].status == FeatureOutcomeStatus.BLOCKED


def test_batch_context_compile_rejected_applies_evidence_gap_to_whole_chunk(tmp_path: Path) -> None:
    def build_batch_context(requests: list[str]) -> ContextBuildResult:
        raise ContextCompileRejected("batch reserved tokens exceed max_input_tokens")

    spec = _batchable_spec(
        tmp_path,
        call_provider_batch=lambda rs, c: BatchProviderResult(responses={}),
        build_batch_context=build_batch_context,
    )
    runner = _runner(tmp_path)

    results = runner.run_batch(spec, ["item-a", "item-b"])
    assert results["item-a"].status == FeatureOutcomeStatus.EVIDENCE_GAP
    assert results["item-b"].status == FeatureOutcomeStatus.EVIDENCE_GAP


def test_batch_ceiling_exhausted_falls_back_for_whole_chunk_without_calling_provider(tmp_path: Path) -> None:
    calls = {"n": 0}

    def call_provider_batch(requests: list[str], compiled) -> BatchProviderResult[str]:
        calls["n"] += 1
        return BatchProviderResult(responses={r: f"released-{r}" for r in requests})

    spec = _batchable_spec(tmp_path, call_provider_batch=call_provider_batch, max_remote_calls_per_run=0)
    runner = _runner(tmp_path)

    results = runner.run_batch(spec, ["item-a", "item-b"])
    assert calls["n"] == 0
    assert all(outcome.status == FeatureOutcomeStatus.DETERMINISTIC_FALLBACK for outcome in results.values())


@pytest.mark.parametrize("first_is_local", [False, True])
def test_batch_rejects_colliding_item_ids_before_work(tmp_path: Path, first_is_local: bool) -> None:
    handler = Mock(side_effect=lambda r: TierResult("local", 1.0) if first_is_local and r == "same:1" else None)
    build = Mock(return_value=ContextBuildResult(compiled=_compiled()))
    provider = Mock(return_value=BatchProviderResult(responses={"same": "remote", "other": "remote"}))
    decisions = Mock()
    spec = _batchable_spec(
        tmp_path, call_provider_batch=provider,
        item_id=lambda r: r.split(":")[0], build_batch_context=build,
        deterministic_handler=handler, max_batch_size=1,
        request_hash=lambda r: r, deterministic_engine_version="v1",
        serialize_response=str, deserialize_response=str,
    )
    runner = _runner(tmp_path, decision_sink=decisions)

    with pytest.raises(StateError, match="unique") as error:
        runner.run_batch(spec, ["same:1", "other:2", "same:3"])

    assert "same" not in str(error.value)
    handler.assert_not_called()
    build.assert_not_called()
    provider.assert_not_called()
    decisions.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_fallback_batch_rejects_colliding_request_hashes_before_work(tmp_path: Path) -> None:
    handler = Mock(return_value=TierResult("local", 1.0))
    decisions = Mock()
    spec = _spec(
        tmp_path, deterministic_handler=handler, request_hash=lambda r: "private-hash",
        deterministic_engine_version="v1", serialize_response=str, deserialize_response=str,
    )
    runner = _runner(tmp_path, decision_sink=decisions)

    with pytest.raises(StateError, match="unique") as error:
        runner.run_batch(spec, ["first", "second"])

    assert "private-hash" not in str(error.value)
    handler.assert_not_called()
    decisions.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("mode", ["batch", "fallback"])
@pytest.mark.parametrize("bad_key", [None, "", "  ", 17, []], ids=["null", "empty", "blank", "number", "list"])
def test_batch_rejects_invalid_result_keys_before_work(tmp_path: Path, mode: str, bad_key: object) -> None:
    handler = Mock(return_value=TierResult("local", 1.0))
    build = Mock(return_value=ContextBuildResult(compiled=_compiled()))
    provider = Mock(return_value=BatchProviderResult(responses={}))
    decisions = Mock()
    key = lambda r: "valid" if r == "first" else bad_key
    options = dict(
        deterministic_handler=handler, deterministic_engine_version="v1",
        serialize_response=str, deserialize_response=str,
    )
    if mode == "batch":
        spec = _batchable_spec(
            tmp_path, call_provider_batch=provider, item_id=key, build_batch_context=build,
            request_hash=lambda r: r, **options,
        )
    else:
        spec = _spec(tmp_path, request_hash=key, **options)
    runner = _runner(tmp_path, decision_sink=decisions)

    with pytest.raises(StateError, match="nonblank"):
        runner.run_batch(spec, ["first", "second"])

    handler.assert_not_called()
    build.assert_not_called()
    provider.assert_not_called()
    decisions.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "2"], ids=["zero", "negative", "bool", "float", "string"])
@pytest.mark.parametrize("local_hit", [False, True])
def test_batch_rejects_invalid_size_before_work(tmp_path: Path, limit: object, local_hit: bool) -> None:
    handler = Mock(return_value=TierResult("local", 1.0) if local_hit else None)
    build = Mock(return_value=ContextBuildResult(compiled=_compiled()))
    provider = Mock(return_value=BatchProviderResult(responses={"one": "remote", "two": "remote"}))
    decisions = Mock()
    spec = _batchable_spec(
        tmp_path, call_provider_batch=provider, build_batch_context=build,
        deterministic_handler=handler, max_batch_size=limit,
        request_hash=lambda r: r, deterministic_engine_version="v1",
        serialize_response=str, deserialize_response=str,
    )
    runner = _runner(tmp_path, decision_sink=decisions)

    with pytest.raises(StateError, match="max_batch_size"):
        runner.run_batch(spec, ["one", "two"])

    handler.assert_not_called()
    build.assert_not_called()
    provider.assert_not_called()
    decisions.assert_not_called()
    assert not list(tmp_path.iterdir())


def test_fallback_batch_keeps_index_keys_for_repeated_requests(tmp_path: Path) -> None:
    handler = Mock(side_effect=[TierResult("first", 1.0), TierResult("second", 1.0)])
    spec = _spec(tmp_path, deterministic_handler=handler, max_batch_size=0)

    outcomes = _runner(tmp_path).run_batch(spec, ["repeated", "repeated"])

    assert list(outcomes) == ["0", "1"]
    assert [outcome.value for outcome in outcomes.values()] == ["first", "second"]


def test_fallback_batch_keeps_distinct_request_hash_keys(tmp_path: Path) -> None:
    handler = Mock(side_effect=lambda request: TierResult(f"value:{request}", 1.0))
    spec = _spec(tmp_path, deterministic_handler=handler, request_hash=lambda request: f"hash:{request}")

    outcomes = _runner(tmp_path).run_batch(spec, ["first", "second"])

    assert list(outcomes) == ["hash:first", "hash:second"]
    assert [outcome.value for outcome in outcomes.values()] == ["value:first", "value:second"]


def test_batch_keeps_exact_distinct_keys_across_chunks(tmp_path: Path) -> None:
    provider = Mock(side_effect=lambda requests, compiled: BatchProviderResult(
        responses={request: f"value:{request}" for request in requests},
    ))
    spec = _batchable_spec(tmp_path, call_provider_batch=provider, max_batch_size=1)

    outcomes = _runner(tmp_path).run_batch(spec, [" item ", "item"])

    assert list(outcomes) == [" item ", "item"]
    assert [outcome.value for outcome in outcomes.values()] == ["value: item ", "value:item"]
    assert provider.call_count == 2


@pytest.mark.parametrize("batchable", [False, True])
def test_empty_batch_does_not_process_items(tmp_path: Path, batchable: bool) -> None:
    handler = Mock()
    provider = Mock()
    if batchable:
        spec = _batchable_spec(tmp_path, call_provider_batch=provider, deterministic_handler=handler)
    else:
        spec = _spec(tmp_path, deterministic_handler=handler)

    assert _runner(tmp_path).run_batch(spec, []) == {}
    handler.assert_not_called()
    provider.assert_not_called()
    assert not list(tmp_path.iterdir())


def _frontier_test_spec(
    tmp_path: Path, batch: bool, provider: Mock, remote: int | None, frontier: int | None,
) -> StructuredFeatureSpec[str, str]:
    limits = dict(max_remote_calls_per_run=remote, max_frontier_calls_per_run=frontier)
    if batch:
        return _batchable_spec(tmp_path, call_provider_batch=provider, **limits)
    return _spec(tmp_path, build_context=lambda _: ContextBuildResult(compiled=_compiled()), call_provider=provider, **limits)


def _dispatch_frontier(
    runner: StructuredFeatureRunner, spec: StructuredFeatureSpec[str, str], batch: bool,
) -> list[FeatureOutcome[str]]:
    if batch:
        return list(runner.run_batch(spec, ["first", "second"]).values())
    return [runner.run(spec, "first")]


def _ceiling_counts(path: Path, scope_key: str) -> dict[str, int]:
    if not path.exists():
        return {}
    with closing(sqlite3.connect(path)) as connection:
        return dict(connection.execute(
            "SELECT kind, call_count FROM call_ceiling_counters WHERE scope_key = ?", (scope_key,),
        ))


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    ("remote", "frontier", "reason"),
    [(0, 1, "remote_cap_exhausted"), (1, 0, "frontier_cap_exhausted")],
)
def test_exhausted_frontier_call_reserves_no_slots(
    tmp_path: Path, batch: bool, remote: int, frontier: int, reason: str,
) -> None:
    provider = Mock(return_value=BatchProviderResult(responses={}) if batch else "remote")
    spec = _frontier_test_spec(tmp_path, batch, provider, remote, frontier)
    runner = _runner(tmp_path)

    outcomes = _dispatch_frontier(runner, spec, batch)

    assert all(outcome.status == FeatureOutcomeStatus.DETERMINISTIC_FALLBACK for outcome in outcomes)
    assert all(outcome.limitations == (reason,) for outcome in outcomes)
    provider.assert_not_called()
    assert _ceiling_counts(tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1") == {}


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    ("remote", "frontier", "reason"),
    [(0, 1, "remote_cap_exhausted"), (1, 0, "frontier_cap_exhausted")],
)
@pytest.mark.parametrize(
    "status",
    [
        FeatureOutcomeStatus.DETERMINISTIC_FALLBACK,
        FeatureOutcomeStatus.ABSTAINED,
        FeatureOutcomeStatus.PARTIAL,
        FeatureOutcomeStatus.BLOCKED,
        FeatureOutcomeStatus.UNAVAILABLE,
        FeatureOutcomeStatus.EVIDENCE_GAP,
    ],
)
def test_exhausted_ceiling_preserves_nonreleased_fallback_and_reports_reason(
    tmp_path: Path, batch: bool, remote: int, frontier: int, reason: str, status: FeatureOutcomeStatus,
) -> None:
    fallback = FeatureOutcome(
        status=status,
        value="partial" if status in {FeatureOutcomeStatus.PARTIAL, FeatureOutcomeStatus.DETERMINISTIC_FALLBACK} else None,
        tier=Tier.LOCAL,
        limitations=("existing limitation",),
        evidence_refs=("evidence-1",),
        retryable=True,
        next_action="Existing next action.",
    )
    handler = Mock(return_value=fallback)
    provider = Mock()
    spec = replace(_frontier_test_spec(tmp_path, batch, provider, remote, frontier), fallback_handler=handler)

    outcomes = _dispatch_frontier(_runner(tmp_path), spec, batch)

    assert outcomes == [replace(fallback, limitations=("existing limitation", reason))] * (2 if batch else 1)
    assert handler.call_count == (2 if batch else 1)
    provider.assert_not_called()
    assert _ceiling_counts(tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1") == {}


@pytest.mark.parametrize("batch", [False, True])
def test_ineligible_failure_preserves_nonreleased_fallback(tmp_path: Path, batch: bool) -> None:
    fallback = FeatureOutcome(
        status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE,
        limitations=("existing limitation",), evidence_refs=("evidence-1",),
        retryable=True, next_action="Existing next action.",
    )
    provider = Mock()
    spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)

    outcomes = _dispatch_frontier(_runner(tmp_path, frontier_eligible=False), spec, batch)

    assert all(outcome is fallback for outcome in outcomes)
    provider.assert_not_called()


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("retryable", [False, True])
@pytest.mark.parametrize(
    "status",
    [
        FeatureOutcomeStatus.DETERMINISTIC_FALLBACK,
        FeatureOutcomeStatus.ABSTAINED,
        FeatureOutcomeStatus.PARTIAL,
        FeatureOutcomeStatus.BLOCKED,
        FeatureOutcomeStatus.UNAVAILABLE,
        FeatureOutcomeStatus.EVIDENCE_GAP,
    ],
)
def test_budget_failure_preserves_nonreleased_fallback_but_is_not_retryable(
    tmp_path: Path, batch: bool, retryable: bool, status: FeatureOutcomeStatus,
) -> None:
    fallback = FeatureOutcome(
        status=status,
        value="partial" if status in {FeatureOutcomeStatus.PARTIAL, FeatureOutcomeStatus.DETERMINISTIC_FALLBACK} else None,
        tier=Tier.LOCAL,
        limitations=("existing limitation",),
        evidence_refs=("evidence-1",),
        retryable=retryable,
        next_action="Existing next action.",
    )
    provider = Mock(side_effect=BudgetExceeded("synthetic budget"))
    spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)

    outcomes = _dispatch_frontier(_runner(tmp_path), spec, batch)

    expected = replace(fallback, limitations=("existing limitation", "budget_exceeded"), retryable=False)
    assert outcomes == [expected] * (2 if batch else 1)
    provider.assert_called_once()
    assert _ceiling_counts(tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1") == {"remote": 1, "frontier": 1}


@pytest.mark.parametrize("batch", [False, True])
def test_budget_failure_keeps_released_fallback_mapping(tmp_path: Path, batch: bool) -> None:
    provider = Mock(side_effect=BudgetExceeded("synthetic budget"))
    spec = _frontier_test_spec(tmp_path, batch, provider, 1, 1)

    outcomes = _dispatch_frontier(_runner(tmp_path), spec, batch)

    assert all(outcome.status == FeatureOutcomeStatus.DETERMINISTIC_FALLBACK for outcome in outcomes)
    assert all(outcome.limitations == ("budget_exceeded",) and not outcome.retryable for outcome in outcomes)
    provider.assert_called_once()


@pytest.mark.parametrize("batch", [False, True])
def test_actual_client_budget_refusal_is_nonretryable_without_provider_dispatch(
    tmp_path: Path, batch: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = Mock()
    monkeypatch.setattr("src.ai.client.AIRequestRouter", Mock(return_value=router))
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    client = AIClient(
        "summary-model", 0.2, 0.5, endpoint="https://example.invalid", api_key="test-key",
        trace_context=AITraceContext(
            edition="acme_weekly", run_id="budget-refusal", caller="test",
            metadata={"run_budget_usd": 0.000001},
        ),
    )
    provider = Mock(side_effect=lambda *_: client.chat("system", "user"))
    fallback = FeatureOutcome(
        status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE, retryable=True,
    )
    spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)

    outcomes = _dispatch_frontier(_runner(tmp_path), spec, batch)

    provider.assert_called_once()
    router.route.assert_not_called()
    state = CostGuard(
        edition="acme_weekly", run_id="budget-refusal", budget_usd=0.000001,
        programs_root=tmp_path / "programs",
    ).current_state()
    assert state.spent_usd == 0.0 and state.reserved_usd == 0.0 and state.ai_calls == 0
    assert _ceiling_counts(tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1") == {"remote": 1, "frontier": 1}
    assert all(outcome.status == FeatureOutcomeStatus.ABSTAINED for outcome in outcomes)
    assert all(outcome.limitations == ("budget_exceeded",) and not outcome.retryable for outcome in outcomes)


@pytest.mark.parametrize("batch", [False, True])
def test_actual_client_post_usage_budget_failure_retains_consumed_call_slots(
    tmp_path: Path, batch: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    router = Mock()
    router.route.return_value = SimpleNamespace(
        usage=SimpleNamespace(prompt_tokens=10000, completion_tokens=10000),
        choices=[SimpleNamespace(message=SimpleNamespace(content="response"))],
    )
    monkeypatch.setattr("src.ai.client.AIRequestRouter", Mock(return_value=router))
    monkeypatch.setattr("src.ai.client.get_ai_mode", lambda: AIMode.ACTIVE)
    monkeypatch.setattr("src.ai.cost_guard.PROGRAMS_ROOT", tmp_path / "programs")
    monkeypatch.setattr(
        "src.core.ai_telemetry.append_ai_telemetry",
        lambda record: append_ai_telemetry(record, programs_root=tmp_path / "programs"),
    )
    monkeypatch.delenv("VERTEX_LLM_TRACE", raising=False)
    client = AIClient(
        "summary-model", 0.2, 0.5, endpoint="https://example.invalid", api_key="test-key",
        trace_context=AITraceContext(
            edition="acme_weekly", run_id="budget-overspend", caller="test",
            metadata={"run_budget_usd": 0.01},
        ),
    )
    provider = Mock(side_effect=lambda *_: client.chat("system", "user"))
    fallback = FeatureOutcome(
        status=FeatureOutcomeStatus.ABSTAINED, value=None, tier=Tier.NONE, retryable=True,
    )
    spec = replace(_frontier_test_spec(tmp_path, batch, provider, 1, 1), fallback_handler=lambda _: fallback)

    outcomes = _dispatch_frontier(_runner(tmp_path), spec, batch)

    provider.assert_called_once()
    router.route.assert_called_once()
    state = CostGuard(
        edition="acme_weekly", run_id="budget-overspend", budget_usd=0.01,
        programs_root=tmp_path / "programs",
    ).current_state()
    assert state.spent_usd > 0.01 and state.reserved_usd == 0.0 and state.ai_calls == 1
    assert _ceiling_counts(tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1") == {"remote": 1, "frontier": 1}
    assert all(outcome.status == FeatureOutcomeStatus.ABSTAINED for outcome in outcomes)
    assert all(outcome.limitations == ("budget_exceeded",) and not outcome.retryable for outcome in outcomes)


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    ("remote", "frontier", "expected"),
    [(None, None, {}), (1, None, {"remote": 1}), (None, 1, {"frontier": 1}), (1, 1, {"remote": 1, "frontier": 1})],
)
def test_successful_frontier_call_reserves_only_bounded_slots(
    tmp_path: Path, batch: bool, remote: int | None, frontier: int | None, expected: dict[str, int],
) -> None:
    response = BatchProviderResult(responses={"first": "first", "second": "second"}) if batch else "first"
    provider = Mock(return_value=response)
    spec = _frontier_test_spec(tmp_path, batch, provider, remote, frontier)

    outcomes = _dispatch_frontier(_runner(tmp_path), spec, batch)

    assert all(outcome.status == FeatureOutcomeStatus.RELEASED for outcome in outcomes)
    provider.assert_called_once()
    assert _ceiling_counts(tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1") == expected


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize(
    ("remote", "frontier", "reason"),
    [(1, 2, "remote_cap_exhausted"), (2, 1, "frontier_cap_exhausted")],
)
def test_exhausted_frontier_call_preserves_prior_counts(
    tmp_path: Path, batch: bool, remote: int, frontier: int, reason: str,
) -> None:
    response = BatchProviderResult(responses={"first": "first", "second": "second"}) if batch else "first"
    provider = Mock(return_value=response)
    spec = _frontier_test_spec(tmp_path, batch, provider, remote, frontier)
    runner = _runner(tmp_path)

    assert all(outcome.status == FeatureOutcomeStatus.RELEASED for outcome in _dispatch_frontier(runner, spec, batch))
    outcomes = _dispatch_frontier(runner, spec, batch)

    assert all(outcome.status == FeatureOutcomeStatus.DETERMINISTIC_FALLBACK for outcome in outcomes)
    assert all(outcome.limitations == (reason,) for outcome in outcomes)
    provider.assert_called_once()
    assert _ceiling_counts(tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1") == {"remote": 1, "frontier": 1}


@pytest.mark.parametrize("batch", [False, True])
def test_frontier_counter_write_failure_rolls_back_remote_slot(tmp_path: Path, batch: bool) -> None:
    path = tmp_path / "ceilings.sqlite3"
    CallCeilingLedger(path).reserve_call(scope_key="setup", kind="remote", ceiling=1)
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(
            "CREATE TRIGGER fail_frontier BEFORE INSERT ON call_ceiling_counters "
            "WHEN NEW.kind = 'frontier' BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END"
        )
        connection.commit()
    provider = Mock()
    spec = _frontier_test_spec(tmp_path, batch, provider, 2, 2)

    with pytest.raises(StateError, match="call-ceiling ledger"):
        _dispatch_frontier(_runner(tmp_path), spec, batch)

    provider.assert_not_called()
    assert _ceiling_counts(path, "acme:run-1:test_feature:v1") == {}


@pytest.mark.parametrize("batch", [False, True])
def test_uncertain_frontier_call_keeps_reserved_slots(tmp_path: Path, batch: bool) -> None:
    provider = Mock(side_effect=TimeoutError("synthetic timeout"))
    spec = _frontier_test_spec(tmp_path, batch, provider, 1, 1)
    runner = _runner(tmp_path)

    first = _dispatch_frontier(runner, spec, batch)
    second = _dispatch_frontier(runner, spec, batch)

    assert all(outcome.status == FeatureOutcomeStatus.UNAVAILABLE for outcome in first)
    assert all(outcome.retryable for outcome in first)
    assert all(outcome.status == FeatureOutcomeStatus.DETERMINISTIC_FALLBACK for outcome in second)
    assert all(outcome.limitations == ("remote_cap_exhausted",) for outcome in second)
    provider.assert_called_once()
    assert _ceiling_counts(tmp_path / "ceilings.sqlite3", "acme:run-1:test_feature:v1") == {"remote": 1, "frontier": 1}


def test_concurrent_frontier_calls_do_not_consume_unavailable_remote_slots(tmp_path: Path) -> None:
    path = tmp_path / "ceilings.sqlite3"
    CallCeilingLedger(path).reserve_call(scope_key="setup", kind="remote", ceiling=1)
    ready = Barrier(8)
    lock = Lock()
    calls: list[str] = []

    def provider(request: str, compiled: CompiledContext) -> str:
        with lock:
            calls.append(request)
        return request

    spec = _spec(
        tmp_path, build_context=lambda _: ContextBuildResult(compiled=_compiled()),
        call_provider=provider, max_remote_calls_per_run=8, max_frontier_calls_per_run=1,
    )
    runner = _runner(tmp_path)

    def dispatch(index: int) -> FeatureOutcome[str]:
        ready.wait(timeout=15)
        return runner.run(spec, str(index))

    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = list(executor.map(dispatch, range(8)))

    assert len(calls) == 1
    assert sum(outcome.status == FeatureOutcomeStatus.RELEASED for outcome in outcomes) == 1
    rejected = [outcome for outcome in outcomes if outcome.status == FeatureOutcomeStatus.DETERMINISTIC_FALLBACK]
    assert len(rejected) == 7
    assert all(outcome.limitations == ("frontier_cap_exhausted",) for outcome in rejected)
    assert _ceiling_counts(path, "acme:run-1:test_feature:v1") == {"remote": 1, "frontier": 1}
