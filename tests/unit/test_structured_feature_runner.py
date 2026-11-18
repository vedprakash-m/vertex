from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import hashlib
import importlib
import json
from pathlib import Path
import sqlite3
from threading import Barrier, Lock
import traceback
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from src.ai.ai_mode import AIMode
from src.ai.client import AIClient, BudgetExceeded
from src.ai import context_tokenizers
from src.ai.context_tokenizers import TiktokenTokenEstimator
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
from src.ai.tiered_router import (
    RouteOutcome,
    Tier,
    TierResult,
    register_decision_sink,
    sink_failure_count,
    unregister_decision_sink,
)
from src.core.ai_telemetry import append_ai_telemetry
from src.core.context_compiler import (
    CompiledContext,
    ContentOrigin,
    ContextCompileRejected,
    ContextCompileRequest,
    ContextManifest,
    DeterministicContextCompiler,
    DeterministicTokenEstimator,
    EvidenceSpan,
    ValidationContext,
    context_manifest_path,
)
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
        context_hash=hashlib.sha256(b"prompt").hexdigest(),
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


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("invalid", ["mismatched-program", "invalid-program"])
def test_context_compiler_identity_refusal_prevents_runner_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: bool, invalid: str,
) -> None:
    programs_root = tmp_path / "programs"

    def build_context(_request: object) -> ContextBuildResult:
        program_id = r"..\outside" if invalid == "invalid-program" else "acme"
        request = ContextCompileRequest(
            program_id=program_id, edition_id=None, feature="test_feature", prompt_version="v1",
            system_instructions="sys", output_schema_text="schema", required_evidence=(), optional_evidence=(),
            max_input_tokens=1000, reserved_output_tokens=0,
        )
        validation = ValidationContext(
            program_id=program_id if invalid == "invalid-program" else "another-program",
            run_id="run-1", execution_mode="observe", classification="internal",
        )
        return ContextBuildResult(compiled=DeterministicContextCompiler(
            token_estimator=SimpleNamespace(tokenizer_id="characters", estimate=len),
            programs_root=programs_root,
        ).compile(request, validation_context=validation))

    provider = Mock(
        return_value=BatchProviderResult(responses={"item-a": "unexpected", "item-b": "unexpected"})
        if batch else "unexpected",
    )
    fallback = Mock()
    runner = _runner(tmp_path)
    reserve = Mock()
    monkeypatch.setattr(runner._ceiling_ledger, "reserve_frontier_call", reserve)
    if batch:
        spec = _batchable_spec(
            tmp_path, call_provider_batch=provider, build_batch_context=build_context, fallback_handler=fallback,
        )
        outcomes = list(runner.run_batch(spec, ["item-a", "item-b"]).values())
        assert len(outcomes) == 2
    else:
        spec = _spec(tmp_path, call_provider=provider, build_context=build_context, fallback_handler=fallback)
        outcomes = [runner.run(spec, "item-a")]

    assert all(outcome.status == FeatureOutcomeStatus.EVIDENCE_GAP for outcome in outcomes)
    assert all(outcome.value is None and not outcome.retryable for outcome in outcomes)
    assert all("program" in outcome.limitations[0] for outcome in outcomes)
    assert all("another-program" not in outcome.limitations[0] for outcome in outcomes)
    assert all("outside" not in outcome.limitations[0] for outcome in outcomes)
    provider.assert_not_called()
    fallback.assert_not_called()
    reserve.assert_not_called()
    assert not programs_root.exists()
    assert not (tmp_path / "outside").exists()


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


@pytest.mark.parametrize("failure", ["rejected", "nonboolean", "exception"])
def test_runner_validates_serialized_released_cache_payload_before_recording(
    tmp_path: Path, failure: str,
) -> None:
    provider = Mock(return_value="valid-provider-response")
    record_release = Mock(return_value="origin")
    verify_release = Mock(return_value=True)
    fallback = Mock(return_value=_fallback_released())
    ledger = Mock(spec=CallCeilingLedger)

    def validate(request: str, response: str) -> ValidationResult:
        if response == "invalid-cached-response":
            if failure == "exception":
                raise RuntimeError("validator failed")
            return ValidationResult(
                valid=cast(bool, 1) if failure == "nonboolean" else False, reason="invalid cached response",
            )
        return ValidationResult(valid=True)

    validator = Mock(side_effect=validate)
    cache_path = tmp_path / "released.sqlite3"
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger,
        released_cache=ReleasedResultCache(
            cache_path, record_release=record_release, verify_release=verify_release,
        ),
    )
    spec = _cacheable_spec(
        tmp_path, call_provider=provider, fallback_handler=fallback,
        semantic_validator=validator, serialize_response=lambda value: "invalid-cached-response",
    )

    if failure == "exception":
        with pytest.raises(RuntimeError, match="validator failed"):
            runner.run(spec, "request")
    else:
        outcome = runner.run(spec, "request")
        assert outcome.status == FeatureOutcomeStatus.BLOCKED
        assert outcome.value is None
        assert outcome.tier == Tier.FRONTIER
        assert "invalid cached response" in outcome.limitations

    assert [call.args for call in validator.call_args_list] == [
        ("request", "valid-provider-response"), ("request", "invalid-cached-response"),
    ]
    provider.assert_called_once()
    ledger.reserve_frontier_call.assert_called_once()
    record_release.assert_not_called()
    fallback.assert_not_called()
    with sqlite3.connect(cache_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 0


@pytest.mark.parametrize("execution", ["single", "per-item-batch"])
@pytest.mark.parametrize("failure", ["rejected", "nonboolean", "nonboolean-zero", "exception", "exception-json"])
def test_runner_revalidates_released_cache_hit_for_current_request(
    tmp_path: Path, execution: str, failure: str,
) -> None:
    provider = Mock(return_value="approved")
    record_release = Mock(return_value="origin")
    verify_release = Mock(return_value=True)
    fallback = Mock(return_value=_fallback_released())
    ledger = Mock(spec=CallCeilingLedger)

    def validate(request: str, response: str) -> ValidationResult:
        if request == "replay":
            if failure == "exception":
                raise RuntimeError("validator failed")
            if failure == "exception-json":
                raise json.JSONDecodeError("validator failed", response, 0)
            return ValidationResult(
                valid=cast(bool, 1 if failure == "nonboolean" else 0) if failure.startswith("nonboolean") else False,
                reason="not valid for this request",
            )
        return ValidationResult(valid=True, evidence_refs=("wi:1001",))

    validator = Mock(side_effect=validate)
    cache_path = tmp_path / "released.sqlite3"
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger,
        released_cache=ReleasedResultCache(
            cache_path, record_release=record_release, verify_release=verify_release,
        ),
    )
    spec = _cacheable_spec(
        tmp_path, call_provider=provider, fallback_handler=fallback,
        semantic_validator=validator, request_hash=lambda request: "same-identity",
    )
    assert runner.run(spec, "origin").released
    with sqlite3.connect(cache_path) as connection:
        before = connection.execute("SELECT * FROM released_result_cache").fetchall()

    def replay() -> FeatureOutcome[str]:
        if execution == "single":
            return runner.run(spec, "replay")
        return runner.run_batch(spec, ["replay"])["same-identity"]

    if failure == "exception":
        with pytest.raises(RuntimeError, match="validator failed"):
            replay()
    elif failure == "exception-json":
        with pytest.raises(json.JSONDecodeError, match="validator failed"):
            replay()
    else:
        outcome = replay()
        assert outcome.status == FeatureOutcomeStatus.BLOCKED
        assert outcome.value is None
        assert outcome.tier == Tier.CACHE
        assert "not valid for this request" in outcome.limitations

    assert validator.call_args_list[-1].args == ("replay", "approved")
    provider.assert_called_once()
    ledger.reserve_frontier_call.assert_called_once()
    record_release.assert_called_once()
    verify_release.assert_called_once()
    fallback.assert_not_called()
    with sqlite3.connect(cache_path) as connection:
        after = connection.execute("SELECT * FROM released_result_cache").fetchall()
    assert after == ([] if failure == "rejected" else before)


@pytest.mark.parametrize(
    "replacement",
    ["payload", "feature", "principal", "classification", "released_content_hash", "release_run_id", "cached_at"],
)
def test_semantic_cache_rejection_preserves_concurrent_replacement(tmp_path: Path, replacement: str) -> None:
    cache_path = tmp_path / "released_cache.sqlite3"
    provider = Mock(return_value="original")
    fallback = Mock(return_value=_fallback_released())
    replacement_row: tuple[object, ...] | None = None

    def validate(request: str, response: str) -> ValidationResult:
        nonlocal replacement_row
        if request == "origin":
            return ValidationResult(valid=True)
        with sqlite3.connect(cache_path) as connection:
            if replacement == "payload":
                connection.execute(
                    "UPDATE released_result_cache SET payload = ?, released_content_hash = ?",
                    ("replacement", hashlib.sha256(b"replacement").hexdigest()),
                )
            else:
                connection.execute(f"UPDATE released_result_cache SET {replacement} = ?", ("replacement",))
            replacement_row = connection.execute("SELECT * FROM released_result_cache").fetchone()
        return ValidationResult(valid=False, reason="rejected")

    spec = _cacheable_spec(
        tmp_path, call_provider=provider, fallback_handler=fallback, semantic_validator=validate,
        request_hash=lambda request: "same-identity",
    )
    runner = _runner(tmp_path)
    assert runner.run(spec, "origin").released

    result = runner.run(spec, "replay")

    assert result.status == FeatureOutcomeStatus.BLOCKED
    assert result.tier == Tier.CACHE
    provider.assert_called_once()
    fallback.assert_not_called()
    assert replacement_row is not None
    with sqlite3.connect(cache_path) as connection:
        assert connection.execute("SELECT * FROM released_result_cache").fetchone() == replacement_row


@pytest.mark.parametrize("failure", ["sqlite", "os"])
def test_semantic_cache_rejection_surfaces_failed_eviction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str,
) -> None:
    cache_path = tmp_path / "released.sqlite3"
    provider = Mock(return_value="private-cached-value")
    fallback = Mock(return_value=_fallback_released())
    record_release = Mock(return_value="origin")
    ledger = Mock(spec=CallCeilingLedger)
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger,
        released_cache=ReleasedResultCache(
            cache_path, record_release=record_release, verify_release=lambda *args: True,
        ),
    )
    def validate(request: str, response: str) -> ValidationResult:
        if request == "replay" and failure == "os":
            module = importlib.import_module("src.ai.structured_feature_runner")
            monkeypatch.setattr(module, "open_program_db", Mock(side_effect=PermissionError("private-path-detail")))
        return ValidationResult(valid=request == "origin", reason="rejected")

    spec = _cacheable_spec(
        tmp_path, call_provider=provider, fallback_handler=fallback, request_hash=lambda request: "same-identity",
        semantic_validator=validate,
    )
    assert runner.run(spec, "origin").released
    with sqlite3.connect(cache_path) as connection:
        before = connection.execute("SELECT * FROM released_result_cache").fetchall()
        if failure == "sqlite":
            connection.execute(
                "CREATE TRIGGER reject_cache_delete BEFORE DELETE ON released_result_cache "
                "BEGIN SELECT RAISE(ABORT, 'private-trigger-detail'); END"
            )

    with pytest.raises(StateError, match="Unable to evict invalid released-result cache entry") as error:
        runner.run(spec, "replay")

    diagnostic = "".join(traceback.format_exception(error.value))
    assert "private-cached-value" not in diagnostic
    assert "private-trigger-detail" not in diagnostic
    assert "private-path-detail" not in diagnostic
    provider.assert_called_once()
    fallback.assert_not_called()
    ledger.reserve_frontier_call.assert_called_once()
    record_release.assert_called_once()
    with sqlite3.connect(cache_path) as connection:
        assert connection.execute("SELECT * FROM released_result_cache").fetchall() == before


def test_semantic_cache_rejection_allows_later_admitted_call(tmp_path: Path) -> None:
    provider = Mock(side_effect=["first", "second"])
    fallback = Mock(return_value=_fallback_released())
    spec = _cacheable_spec(
        tmp_path, call_provider=provider, fallback_handler=fallback, request_hash=lambda request: "same-identity",
        semantic_validator=lambda request, response: ValidationResult(valid=request != "reject", reason="rejected"),
    )
    runner = _runner(tmp_path)
    assert runner.run(spec, "origin").value == "first"

    rejected = runner.run(spec, "reject")
    assert rejected.status == FeatureOutcomeStatus.BLOCKED
    assert rejected.tier == Tier.CACHE
    provider.assert_called_once()

    later = runner.run(spec, "later")
    assert later.status == FeatureOutcomeStatus.RELEASED
    assert later.tier == Tier.FRONTIER
    assert later.value == "second"
    assert provider.call_count == 2
    fallback.assert_not_called()


def test_legacy_cache_override_forwards_runner_validation(tmp_path: Path) -> None:
    class ForwardingCache(ReleasedResultCache):
        def get(self, identity, *, deserialize):
            return super().get(identity, deserialize=deserialize)

    cache_path = tmp_path / "released.sqlite3"
    provider = Mock(side_effect=["first", "second"])
    spec = _cacheable_spec(
        tmp_path, call_provider=provider, request_hash=lambda request: "same-identity",
        semantic_validator=lambda request, response: ValidationResult(valid=request != "reject", reason="rejected"),
    )
    runner = _runner(tmp_path, released_cache=ForwardingCache(cache_path))

    assert runner.run(spec, "origin").value == "first"
    assert runner.run(spec, "valid").value == "first"
    rejected = runner.run(spec, "reject")
    assert rejected.status == FeatureOutcomeStatus.BLOCKED
    with sqlite3.connect(cache_path) as connection:
        assert connection.execute("SELECT * FROM released_result_cache").fetchall() == []
    assert runner.run(spec, "later").value == "second"
    assert provider.call_count == 2


@pytest.mark.parametrize("replacement", ["other-value", "miss"])
def test_injected_cache_cannot_substitute_a_different_validated_value(
    tmp_path: Path, replacement: str,
) -> None:
    class SubstitutingCache(ReleasedResultCache):
        def get(self, identity, *, deserialize, **kwargs):
            value = super().get(identity, deserialize=deserialize, **kwargs)
            if value is None:
                return None
            return "unvalidated" if replacement == "other-value" else None

    cache = SubstitutingCache(tmp_path / "released.sqlite3")
    provider = Mock(return_value="approved")
    runner = _runner(tmp_path, released_cache=cache)
    spec = _cacheable_spec(tmp_path, call_provider=provider)
    assert runner.run(spec, "request").released

    with pytest.raises(StateError, match="cache did not validate"):
        runner.run(spec, "request")

    provider.assert_called_once()


def test_injected_cache_must_run_semantic_eviction_check(tmp_path: Path) -> None:
    class IgnoringCache(ReleasedResultCache):
        def get(self, identity, *, deserialize, **kwargs):
            return super().get(identity, deserialize=lambda payload: deserialize(payload))

    cache = IgnoringCache(tmp_path / "released.sqlite3")
    provider = Mock(return_value="approved")
    runner = _runner(tmp_path, released_cache=cache)
    spec = _cacheable_spec(tmp_path, call_provider=provider)
    assert runner.run(spec, "request").released

    with pytest.raises(StateError, match="cache did not validate"):
        runner.run(spec, "request")

    provider.assert_called_once()


def test_runner_accepts_semantically_valid_sanitized_cache_payload(tmp_path: Path) -> None:
    provider = Mock(return_value="  approved  ")
    validator = Mock(side_effect=lambda request, value: ValidationResult(
        valid=bool(value.strip()), evidence_refs=(f"ref:{value}",),
    ))
    spec = _cacheable_spec(
        tmp_path, call_provider=provider, semantic_validator=validator,
        serialize_response=lambda value: value.strip(),
    )
    runner = _runner(tmp_path)

    fresh = runner.run(spec, "request")
    cached = runner.run(spec, "request")

    assert fresh.released and fresh.value == "  approved  "
    assert cached.released and cached.value == "approved" and cached.tier == Tier.CACHE
    assert cached.evidence_refs == ("ref:approved",)
    assert [call.args for call in validator.call_args_list] == [
        ("request", "  approved  "), ("request", "approved"), ("request", "approved"),
    ]
    provider.assert_called_once()


@pytest.mark.parametrize("execution", ["single", "native-batch"])
@pytest.mark.parametrize("verdict", [1, "approved"])
def test_runner_rejects_nonboolean_semantic_verdict_per_item(
    tmp_path: Path, execution: str, verdict: object,
) -> None:
    def validate(request: str, response: str) -> ValidationResult:
        return ValidationResult(
            valid=True if request == "good" else cast(bool, verdict),
            reason="invalid validator verdict",
        )

    spec = _spec(
        tmp_path,
        build_context=lambda request: ContextBuildResult(compiled=_compiled()),
        call_provider=lambda request, compiled: request,
        semantic_validator=validate,
        item_id=lambda request: request,
        build_batch_context=lambda requests: ContextBuildResult(compiled=_compiled()),
        call_provider_batch=lambda requests, compiled: BatchProviderResult(
            responses={request: request for request in requests},
        ),
    )
    runner = _runner(tmp_path)
    if execution == "single":
        outcome = runner.run(spec, "bad")
    else:
        outcomes = runner.run_batch(spec, ["good", "bad"])
        assert outcomes["good"].released
        outcome = outcomes["bad"]
    assert outcome.status == FeatureOutcomeStatus.BLOCKED
    assert outcome.value is None


def test_model_cache_distinguishes_default_estimator_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.core.context_budget._resolve_strict_tokenizer",
        lambda model: ("tiktoken:synthetic", len),
    )
    model = "gpt-4o-mini"
    request = ContextCompileRequest(
        program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
        system_instructions="same prompt", output_schema_text="schema", required_evidence=(),
        optional_evidence=(), max_input_tokens=1000, reserved_output_tokens=0,
    )
    validation = ValidationContext(
        program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
    )

    def build_context(_request: str) -> ContextBuildResult:
        compiled = DeterministicContextCompiler(
            token_estimator=DeterministicTokenEstimator(model=model), programs_root=tmp_path / "programs",
        ).compile(request, validation_context=validation)
        return ContextBuildResult(compiled=compiled)

    provider = Mock(side_effect=["first-value", "second-value"])
    spec = _cacheable_spec(tmp_path, call_provider=provider, build_context=build_context)
    runner = _runner(tmp_path)

    first = runner.run(spec, "request")
    repeat_first = runner.run(spec, "request")
    model = "gpt-4"
    second = runner.run(spec, "request")
    repeat_second = runner.run(spec, "request")

    assert [outcome.tier for outcome in (first, repeat_first, second, repeat_second)] == [
        Tier.FRONTIER, Tier.CACHE, Tier.FRONTIER, Tier.CACHE,
    ]
    assert [outcome.value for outcome in (first, repeat_first, second, repeat_second)] == [
        "first-value", "first-value", "second-value", "second-value",
    ]
    assert provider.call_count == 2
    with sqlite3.connect(tmp_path / "released_cache.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 2


def test_default_estimator_backend_partitions_cache_and_preserves_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    original_import = importlib.import_module
    available = False
    module = SimpleNamespace(
        encoding_name_for_model=lambda _model: "synthetic-encoding",
        get_encoding=lambda _name: SimpleNamespace(encode_ordinary=lambda _text: [1, 2, 3]),
    )

    def import_module(name: str, package: str | None = None) -> object:
        if name != "tiktoken":
            return original_import(name, package)
        if not available:
            raise ModuleNotFoundError("absent", name="tiktoken")
        return module

    monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
    request = ContextCompileRequest(
        program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
        system_instructions="same prompt", output_schema_text="schema", required_evidence=(),
        optional_evidence=(), max_input_tokens=1000, reserved_output_tokens=0,
    )
    validation = ValidationContext(
        program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
    )
    compiled: list[CompiledContext] = []

    def build_context(_request: str) -> ContextBuildResult:
        result = DeterministicContextCompiler(
            token_estimator=DeterministicTokenEstimator(model="synthetic"),
            programs_root=tmp_path / "programs",
        ).compile(request, validation_context=validation)
        compiled.append(result)
        return ContextBuildResult(compiled=result)

    provider = Mock(side_effect=["heuristic-value", "encoding-value"])
    spec = _cacheable_spec(tmp_path, call_provider=provider, build_context=build_context)
    runner = _runner(tmp_path)
    first = runner.run(spec, "request")
    repeat_first = runner.run(spec, "request")
    path = context_manifest_path(compiled[0].manifest, programs_root=tmp_path / "programs")
    original_bytes = path.read_bytes()

    available = True
    second = runner.run(spec, "request")
    repeat_second = runner.run(spec, "request")

    assert [outcome.tier for outcome in (first, repeat_first, second, repeat_second)] == [
        Tier.FRONTIER, Tier.CACHE, Tier.FRONTIER, Tier.CACHE,
    ]
    assert [outcome.value for outcome in (first, repeat_first, second, repeat_second)] == [
        "heuristic-value", "heuristic-value", "encoding-value", "encoding-value",
    ]
    assert provider.call_count == 2
    assert compiled[0].manifest.context_hash == compiled[2].manifest.context_hash
    assert compiled[0].manifest.tokenizer_id != compiled[2].manifest.tokenizer_id
    assert compiled[0].manifest.cache_key != compiled[2].manifest.cache_key
    assert path.read_bytes() == original_bytes
    assert "existing context manifest differs from compilation" in caplog.text
    with sqlite3.connect(tmp_path / "released_cache.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 2


def test_default_estimator_does_not_replay_model_only_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = importlib.import_module

    def import_module(name: str, package: str | None = None) -> object:
        if name == "tiktoken":
            raise ModuleNotFoundError("absent", name="tiktoken")
        return original_import(name, package)

    monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
    request = ContextCompileRequest(
        program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
        system_instructions="same prompt", output_schema_text="schema", required_evidence=(),
        optional_evidence=(), max_input_tokens=1000, reserved_output_tokens=0,
    )
    validation = ValidationContext(
        program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
    )

    class LegacyEstimator:
        tokenizer_id = "context_budget.estimate_tokens:synthetic"

        def estimate(self, text: str) -> int:
            return max(1, len(text) // 4) if text else 0

    legacy = DeterministicContextCompiler(
        token_estimator=LegacyEstimator(), programs_root=tmp_path / "programs",
    ).compile(request, validation_context=validation)
    current = DeterministicContextCompiler(
        token_estimator=DeterministicTokenEstimator(model="synthetic"),
        programs_root=tmp_path / "programs",
    ).compile(request, validation_context=validation)
    provider = Mock(return_value="new-value")
    spec = _cacheable_spec(
        tmp_path, call_provider=provider, build_context=lambda _: ContextBuildResult(compiled=current),
    )
    runner = _runner(tmp_path)
    identity = runner._cache_identity(spec, "request", ContextBuildResult(compiled=legacy))
    assert identity is not None
    cache = ReleasedResultCache(tmp_path / "released_cache.sqlite3")
    cache.put(
        identity, "legacy-value", serialize=lambda value: value,
        released_content_hash=hashlib.sha256(b"legacy-value").hexdigest(), release_run_id="old-run",
    )
    with sqlite3.connect(tmp_path / "released_cache.sqlite3") as connection:
        original_row = connection.execute(
            "SELECT * FROM released_result_cache WHERE cache_key = ?", (identity.cache_key(),),
        ).fetchone()

    first = runner.run(spec, "request")
    second = runner.run(spec, "request")

    assert (first.tier, first.value) == (Tier.FRONTIER, "new-value")
    assert (second.tier, second.value) == (Tier.CACHE, "new-value")
    assert provider.call_count == 1
    with sqlite3.connect(tmp_path / "released_cache.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 2
        assert connection.execute(
            "SELECT * FROM released_result_cache WHERE cache_key = ?", (identity.cache_key(),),
        ).fetchone() == original_row


def test_default_estimator_does_not_replay_legacy_model_agnostic_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "src.core.context_budget._resolve_strict_tokenizer",
        lambda model: ("tiktoken:synthetic", len),
    )
    request = ContextCompileRequest(
        program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
        system_instructions="same prompt", output_schema_text="schema", required_evidence=(),
        optional_evidence=(), max_input_tokens=1000, reserved_output_tokens=0,
    )
    validation = ValidationContext(
        program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
    )
    compiled = DeterministicContextCompiler(programs_root=tmp_path / "programs").compile(
        request, validation_context=validation,
    )
    build = ContextBuildResult(compiled=compiled)
    provider = Mock(return_value="new-value")
    spec = _cacheable_spec(tmp_path, call_provider=provider, build_context=lambda _: build)
    runner = _runner(tmp_path)
    identity = runner._cache_identity(spec, "request", build)
    assert identity is not None
    legacy = replace(identity, tokenizer_id="context_budget.estimate_tokens")
    cache = ReleasedResultCache(tmp_path / "released_cache.sqlite3")
    cache.put(
        legacy, "legacy-value", serialize=lambda value: value,
        released_content_hash=hashlib.sha256(b"legacy-value").hexdigest(), release_run_id="old-run",
    )
    with sqlite3.connect(tmp_path / "released_cache.sqlite3") as connection:
        original_row = connection.execute(
            "SELECT * FROM released_result_cache WHERE cache_key = ?", (legacy.cache_key(),),
        ).fetchone()

    first = runner.run(spec, "request")
    second = runner.run(spec, "request")

    assert (first.tier, first.value) == (Tier.FRONTIER, "new-value")
    assert (second.tier, second.value) == (Tier.CACHE, "new-value")
    assert provider.call_count == 1
    with sqlite3.connect(tmp_path / "released_cache.sqlite3") as connection:
        assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 2
        assert connection.execute(
            "SELECT * FROM released_result_cache WHERE cache_key = ?", (legacy.cache_key(),),
        ).fetchone() == original_row


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


@pytest.mark.parametrize("eviction_fails", [False, True])
def test_corrupt_runner_cache_stops_before_deserialization_and_dispatch(
    tmp_path: Path, eviction_fails: bool,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = ReleasedResultCache(db_path)
    provider = Mock(return_value="released")
    deserialize = Mock(side_effect=lambda value: value)
    ledger = Mock(spec=CallCeilingLedger)
    runner = StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=True,
        ceiling_ledger=ledger,
        released_cache=cache,
        deterministic_cache=DeterministicResultCache(tmp_path / "deterministic.sqlite3"),
    )
    spec = _cacheable_spec(tmp_path, call_provider=provider, deserialize_response=deserialize)

    assert runner.run(spec, "request").value == "released"
    deserialize.reset_mock()
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE released_result_cache SET payload = ?", ("tampered-secret",))
        if eviction_fails:
            connection.execute(
                "CREATE TRIGGER reject_cache_delete BEFORE DELETE ON released_result_cache "
                "BEGIN SELECT RAISE(ABORT, 'delete rejected'); END"
            )

    with pytest.raises(StateError) as error:
        runner.run(spec, "request")

    assert "tampered-secret" not in str(error.value)
    deserialize.assert_not_called()
    assert provider.call_count == 1
    assert ledger.reserve_frontier_call.call_count == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == int(eviction_fails)


def test_runner_serializes_cached_response_once(tmp_path: Path) -> None:
    serialize: Mock = Mock(side_effect=lambda value: f"{value}:{serialize.call_count}")
    provider = Mock(return_value="released")
    spec = _cacheable_spec(
        tmp_path,
        call_provider=provider,
        serialize_response=serialize,
        deserialize_response=lambda value: value.rsplit(":", 1)[0],
    )
    runner = _runner(tmp_path)

    assert runner.run(spec, "request").value == "released"
    assert serialize.call_count == 1
    assert runner.run(spec, "request").value == "released"
    assert serialize.call_count == 1
    assert provider.call_count == 1
    with sqlite3.connect(tmp_path / "released_cache.sqlite3") as connection:
        payload, digest = connection.execute(
            "SELECT payload, released_content_hash FROM released_result_cache"
        ).fetchone()
    assert digest == hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_cache_disabled_when_spec_omits_any_required_field(tmp_path: Path) -> None:
    calls = {"provider": 0}

    def provider(request: str, compiled) -> str:
        calls["provider"] += 1
        return "released"

    # request_hash omitted -> caching never engages, even though every
    # other cache field is present.
    serialize = Mock(side_effect=AssertionError("uncached serialization"))
    deserialize = Mock(side_effect=AssertionError("uncached deserialization"))
    spec = _cacheable_spec(
        tmp_path, call_provider=provider, request_hash=None,
        serialize_response=serialize, deserialize_response=deserialize,
    )
    runner = _runner(tmp_path)

    runner.run(spec, "request")
    runner.run(spec, "request")
    assert calls["provider"] == 2
    serialize.assert_not_called()
    deserialize.assert_not_called()


# ---------------------------------------------------------------------------
# CacheIdentity / ReleasedResultCache in isolation.
# ---------------------------------------------------------------------------


def _identity(**overrides) -> CacheIdentity:
    base = dict(
        program_id="acme",
        feature="f",
        request_hash="rh",
        context_hash="ch",
        compiler_cache_key="compiler-key",
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
        "program_id", "feature", "request_hash", "context_hash", "compiler_cache_key", "prompt_version", "policy_version", "compiler_policy_version",
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

    cache.put(identity, "the-value", serialize=lambda v: v, released_content_hash=hashlib.sha256(b"the-value").hexdigest(), release_run_id="run-1")
    assert cache.get(identity, deserialize=lambda v: v) == "the-value"


@pytest.mark.parametrize("missing", ["recorder", "verifier"])
def test_release_binding_callbacks_must_be_paired(tmp_path: Path, missing: str) -> None:
    record = None if missing == "recorder" else lambda _identity, _payload: "audit-run"
    verify = None if missing == "verifier" else lambda _identity, _payload, _run_id: True

    with pytest.raises(StateError, match="both release callbacks"):
        ReleasedResultCache(tmp_path / "cache.sqlite3", record_release=record, verify_release=verify)


def test_release_binding_callbacks_record_before_write_and_verify_observed_row(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    identity = _identity()
    events: list[tuple[object, ...]] = []

    def record(observed: CacheIdentity, payload: str) -> str:
        with sqlite3.connect(db_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 0
        events.append(("record", observed, payload))
        return "audit-run"

    def verify(observed: CacheIdentity, payload: str, run_id: str) -> bool:
        events.append(("verify", observed, payload, run_id))
        return True

    cache = ReleasedResultCache(db_path, record_release=record, verify_release=verify)
    assert cache.get(identity, deserialize=lambda value: value) is None
    cache.put(
        identity, "value", serialize=lambda value: value,
        released_content_hash=hashlib.sha256(b"value").hexdigest(), release_run_id="execution-run",
    )
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT release_run_id FROM released_result_cache").fetchone() == ("audit-run",)

    def deserialize(value: str) -> str:
        events.append(("decode", value))
        return value

    assert cache.get(identity, deserialize=deserialize) == "value"
    assert events == [
        ("record", identity, "value"),
        ("verify", identity, "value", "audit-run"),
        ("decode", "value"),
    ]


@pytest.mark.parametrize("failure", ["invalid-origin", "blank-origin", "verifier-error", "verifier-result"])
def test_release_binding_refuses_before_decoding(
    tmp_path: Path, failure: str,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    identity = _identity()
    verifier = Mock(return_value=False)
    if failure == "verifier-error":
        verifier.side_effect = StateError("audit unavailable")
    elif failure == "verifier-result":
        verifier.return_value = None
    cache = ReleasedResultCache(
        db_path, record_release=lambda _identity, _payload: "origin-run", verify_release=verifier,
    )
    payload = '{"synthetic":'
    cache.put(
        identity, payload, serialize=lambda value: value,
        released_content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(), release_run_id="run",
    )
    if failure == "blank-origin":
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE released_result_cache SET release_run_id = ' '")
    with sqlite3.connect(db_path) as connection:
        before = connection.execute("SELECT * FROM released_result_cache").fetchall()
    deserialize = Mock(side_effect=json.loads)
    message = {
        "invalid-origin": "originating release",
        "blank-origin": "originating release",
        "verifier-error": "audit unavailable",
        "verifier-result": "verification result",
    }[failure]

    with pytest.raises(StateError, match=message):
        cache.get(identity, deserialize=deserialize)

    deserialize.assert_not_called()
    if failure == "blank-origin":
        verifier.assert_not_called()
    else:
        verifier.assert_called_once_with(identity, payload, "origin-run")
    with sqlite3.connect(db_path) as connection:
        after = connection.execute("SELECT * FROM released_result_cache").fetchall()
    assert after == ([] if failure in {"invalid-origin", "blank-origin"} else before)


@pytest.mark.parametrize("failure", ["malformed-json", "decoder-none", "decoder-error"])
def test_release_binding_verified_payload_keeps_decoder_failure_behavior(
    tmp_path: Path, failure: str,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    identity = _identity()
    verifier = Mock(return_value=True)
    cache = ReleasedResultCache(
        db_path, record_release=lambda _identity, _payload: "origin-run", verify_release=verifier,
    )
    payload = '{"synthetic":' if failure == "malformed-json" else '"synthetic"'
    cache.put(
        identity, payload, serialize=lambda value: value,
        released_content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(), release_run_id="run",
    )
    with sqlite3.connect(db_path) as connection:
        before = connection.execute("SELECT * FROM released_result_cache").fetchall()
    custom_error = ValueError("synthetic decoder error")
    deserialize = Mock(side_effect={
        "malformed-json": json.loads,
        "decoder-none": lambda _value: None,
        "decoder-error": custom_error,
    }[failure])

    with pytest.raises(ValueError if failure == "decoder-error" else StateError) as error:
        cache.get(identity, deserialize=deserialize)

    if failure == "decoder-error":
        assert error.value is custom_error
    elif failure == "decoder-none":
        assert "decoder returned no value" in str(error.value)
    else:
        assert "JSON payload is invalid" in str(error.value)
    verifier.assert_called_once_with(identity, payload, "origin-run")
    deserialize.assert_called_once_with(payload)
    with sqlite3.connect(db_path) as connection:
        after = connection.execute("SELECT * FROM released_result_cache").fetchall()
    assert after == ([] if failure == "malformed-json" else before)


@pytest.mark.parametrize("run_id", [None, "", "   ", 42])
def test_release_binding_rejects_invalid_recorded_run_id(tmp_path: Path, run_id: object) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = ReleasedResultCache(
        db_path, record_release=Mock(return_value=run_id), verify_release=Mock(return_value=True),
    )
    with pytest.raises(StateError, match="release run ID"):
        cache.put(
            _identity(), "value", serialize=lambda value: value,
            released_content_hash=hashlib.sha256(b"value").hexdigest(), release_run_id="execution-run",
        )
    if db_path.exists():
        with sqlite3.connect(db_path) as connection:
            assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 0


def test_release_binding_verifier_error_preserves_observed_row(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = ReleasedResultCache(
        db_path, record_release=lambda _identity, _payload: "origin-run",
        verify_release=Mock(side_effect=StateError("audit unavailable")),
    )
    cache.put(
        _identity(), "value", serialize=lambda value: value,
        released_content_hash=hashlib.sha256(b"value").hexdigest(), release_run_id="execution-run",
    )
    with pytest.raises(StateError, match="audit unavailable"):
        cache.get(_identity(), deserialize=lambda value: value)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT payload, release_run_id FROM released_result_cache"
        ).fetchone() == ("value", "origin-run")


@pytest.mark.parametrize("result", [None, 1, "true"])
def test_release_binding_invalid_verifier_result_preserves_observed_row(
    tmp_path: Path, result: object,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = ReleasedResultCache(
        db_path, record_release=lambda _identity, _payload: "origin-run",
        verify_release=Mock(return_value=result),
    )
    cache.put(
        _identity(), "value", serialize=lambda value: value,
        released_content_hash=hashlib.sha256(b"value").hexdigest(), release_run_id="execution-run",
    )
    with pytest.raises(StateError, match="verification result"):
        cache.get(_identity(), deserialize=lambda value: value)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT payload, release_run_id FROM released_result_cache"
        ).fetchone() == ("value", "origin-run")


def test_release_binding_invalid_origin_evicts_only_observed_row(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    identity = _identity()

    def verify(_identity: CacheIdentity, _payload: str, _run_id: str) -> bool:
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                "UPDATE released_result_cache SET payload = ?, released_content_hash = ?, release_run_id = ?",
                ("replacement", hashlib.sha256(b"replacement").hexdigest(), "replacement-run"),
            )
        return False

    cache = ReleasedResultCache(
        db_path, record_release=lambda _identity, _payload: "origin-run", verify_release=verify,
    )
    cache.put(
        identity, "original", serialize=lambda value: value,
        released_content_hash=hashlib.sha256(b"original").hexdigest(), release_run_id="execution-run",
    )
    deserialize = Mock(side_effect=AssertionError("unverified payload decoded"))
    with pytest.raises(StateError, match="originating release"):
        cache.get(identity, deserialize=deserialize)
    deserialize.assert_not_called()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT payload, release_run_id FROM released_result_cache"
        ).fetchone() == ("replacement", "replacement-run")


@pytest.mark.parametrize("origin", ["", "   "])
def test_release_binding_blank_origin_is_evicted_without_verification(tmp_path: Path, origin: str) -> None:
    db_path = tmp_path / "cache.sqlite3"
    verifier = Mock(return_value=True)
    cache = ReleasedResultCache(
        db_path, record_release=lambda _identity, _payload: "origin-run", verify_release=verifier,
    )
    cache.put(
        _identity(), "value", serialize=lambda value: value,
        released_content_hash=hashlib.sha256(b"value").hexdigest(), release_run_id="execution-run",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE released_result_cache SET release_run_id = ?", (origin,))

    with pytest.raises(StateError, match="originating release"):
        cache.get(_identity(), deserialize=lambda value: value)

    verifier.assert_not_called()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 0


def test_release_binding_eviction_failure_is_explicit_and_preserves_row(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    verifier = Mock(return_value=False)
    cache = ReleasedResultCache(
        db_path, record_release=lambda _identity, _payload: "origin-run", verify_release=verifier,
    )
    cache.put(
        _identity(), "private-payload", serialize=lambda value: value,
        released_content_hash=hashlib.sha256(b"private-payload").hexdigest(), release_run_id="execution-run",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_cache_delete BEFORE DELETE ON released_result_cache "
            "BEGIN SELECT RAISE(ABORT, 'private-trigger-detail'); END"
        )

    with pytest.raises(StateError, match="evict invalid") as error:
        cache.get(_identity(), deserialize=lambda value: value)

    verifier.assert_called_once()
    diagnostic = "".join(traceback.format_exception(error.value))
    assert "private-payload" not in diagnostic
    assert "private-trigger-detail" not in diagnostic
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT payload, release_run_id FROM released_result_cache"
        ).fetchone() == ("private-payload", "origin-run")


@pytest.mark.parametrize("execution", ["single", "per-item-batch"])
def test_release_binding_refusal_stops_single_and_fallback_batch(
    tmp_path: Path, execution: str,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    provider = Mock(return_value="frontier-value")
    ledger = Mock(spec=CallCeilingLedger)
    cache = ReleasedResultCache(
        db_path, record_release=lambda _identity, _payload: "origin-run",
        verify_release=lambda _identity, _payload, _run_id: False,
    )
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger,
        released_cache=cache, deterministic_cache=DeterministicResultCache(db_path),
    )
    spec = _cacheable_spec(tmp_path, call_provider=provider)
    assert runner.run(spec, "request").value == "frontier-value"

    with pytest.raises(StateError, match="originating release"):
        if execution == "single":
            runner.run(spec, "request")
        else:
            runner.run_batch(spec, ["request"])

    provider.assert_called_once()
    ledger.reserve_frontier_call.assert_called_once()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 0


def test_release_binding_does_not_change_native_batch_cache_scope(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    provider = Mock(return_value="frontier-value")
    verifier = Mock(return_value=False)
    cache = ReleasedResultCache(
        db_path, record_release=lambda _identity, _payload: "origin-run", verify_release=verifier,
    )
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True,
        ceiling_ledger=Mock(spec=CallCeilingLedger), released_cache=cache,
        deterministic_cache=DeterministicResultCache(db_path),
    )
    spec = _cacheable_spec(tmp_path, call_provider=provider)
    assert runner.run(spec, "request").value == "frontier-value"
    serialize = Mock(side_effect=AssertionError("native batch serialization"))
    deserialize = Mock(side_effect=AssertionError("native batch deserialization"))
    batch_spec = replace(
        spec, item_id=lambda request: request,
        serialize_response=serialize, deserialize_response=deserialize,
        build_batch_context=lambda _requests: ContextBuildResult(compiled=_compiled()),
        call_provider_batch=lambda _requests, _compiled: BatchProviderResult(responses={"request": "batch-value"}),
    )

    outcome = runner.run_batch(batch_spec, ["request"])["request"]
    assert outcome.value == "batch-value"
    assert outcome.tier == Tier.FRONTIER
    verifier.assert_not_called()
    serialize.assert_not_called()
    deserialize.assert_not_called()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 1


@pytest.mark.parametrize("column", ["feature", "principal", "classification"])
@pytest.mark.parametrize("bound", [False, True])
def test_released_result_cache_checks_stored_identity_on_read(tmp_path: Path, column: str, bound: bool) -> None:
    db_path = tmp_path / "cache.sqlite3"
    verify = Mock(return_value=True)
    record = Mock(side_effect=["run-1", "run-2"])
    cache = ReleasedResultCache(
        db_path,
        record_release=record if bound else None,
        verify_release=verify if bound else None,
    )
    identity = _identity(principal="operator")
    cache.put(identity, "secret-value", serialize=lambda v: v, released_content_hash=hashlib.sha256(b"secret-value").hexdigest(), release_run_id="run-1")
    assert cache.get(identity, deserialize=lambda v: v) == "secret-value"
    verify.reset_mock()

    with sqlite3.connect(db_path) as connection:
        connection.execute(
            f"UPDATE released_result_cache SET {column} = ? WHERE cache_key = ?",
            ("different", identity.cache_key()),
        )
        before = connection.execute("SELECT * FROM released_result_cache").fetchall()

    deserialize = Mock(side_effect=AssertionError("mismatched cache value decoded"))
    assert cache.get(identity, deserialize=deserialize) is None
    deserialize.assert_not_called()
    verify.assert_not_called()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT * FROM released_result_cache").fetchall() == before

    cache.put(
        identity, "fresh-value", serialize=lambda value: value,
        released_content_hash=hashlib.sha256(b"fresh-value").hexdigest(),
        release_run_id="run-2",
    )
    assert cache.get(identity, deserialize=lambda value: value) == "fresh-value"
    if bound:
        assert record.call_count == 2
        verify.assert_called_once_with(identity, "fresh-value", "run-2")


@pytest.mark.parametrize("cache_kind", ["released", "deterministic"])
@pytest.mark.parametrize("decode_kind", ["json-null", "decoder-none"])
def test_cache_rejects_decoded_none_without_changing_the_row(
    tmp_path: Path, cache_kind: str, decode_kind: str,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    payload = "null" if decode_kind == "json-null" else '"synthetic-private-marker"'
    deserialize = Mock(side_effect=json.loads if decode_kind == "json-null" else lambda _: None)
    if cache_kind == "released":
        released_identity = _identity()
        released_cache = ReleasedResultCache(db_path)
        released_cache.put(
            released_identity, payload, serialize=lambda value: value,
            released_content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            release_run_id="run-1",
        )
        table = "released_result_cache"
    else:
        deterministic_identity = DeterministicCacheIdentity(
            program_id="acme", feature="f", request_hash="rh", snapshot_id="snap-1",
            deterministic_engine_version="v1", principal="operator", classification="internal",
        )
        deterministic_cache = DeterministicResultCache(db_path)
        deterministic_cache.put(deterministic_identity, payload, serialize=lambda value: value)
        table = "deterministic_result_cache"

    with sqlite3.connect(db_path) as connection:
        before = connection.execute(f"SELECT * FROM {table}").fetchall()
    with pytest.raises(StateError) as error:
        if cache_kind == "released":
            released_cache.get(released_identity, deserialize=deserialize)
        else:
            deterministic_cache.get(deterministic_identity, deserialize=deserialize)

    assert str(error.value) == f"{cache_kind.capitalize()}-result cache decoder returned no value."
    assert error.value.__cause__ is None
    deserialize.assert_called_once_with(payload)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"SELECT * FROM {table}").fetchall() == before


@pytest.mark.parametrize("cache_kind", ["released", "deterministic"])
@pytest.mark.parametrize("existing", [False, True], ids=["new-cache", "existing-row"])
def test_cache_refuses_direct_none_before_callbacks_or_storage(
    tmp_path: Path, cache_kind: str, existing: bool,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    serialize = Mock(return_value="null")
    record_release = Mock(return_value="release-run")
    if cache_kind == "released":
        released_identity = _identity()
        released_cache = ReleasedResultCache(
            db_path, record_release=record_release, verify_release=lambda *_: True,
        )
        table = "released_result_cache"
        if existing:
            released_cache.put(
                released_identity, "valid", serialize=lambda value: value,
                released_content_hash=hashlib.sha256(b"valid").hexdigest(),
                release_run_id="run-1",
            )
            record_release.reset_mock()
    else:
        deterministic_identity = DeterministicCacheIdentity(
            program_id="acme", feature="f", request_hash="rh", snapshot_id="snap-1",
            deterministic_engine_version="v1", principal="operator", classification="internal",
        )
        deterministic_cache = DeterministicResultCache(db_path)
        table = "deterministic_result_cache"
        if existing:
            deterministic_cache.put(deterministic_identity, "valid", serialize=lambda value: value)

    if existing:
        with sqlite3.connect(db_path) as connection:
            before = connection.execute(f"SELECT * FROM {table}").fetchall()
    with pytest.raises(StateError, match=f"^{cache_kind.capitalize()}-result cache received no value\\.$"):
        if cache_kind == "released":
            released_cache.put(
                released_identity, None, serialize=serialize,
                released_content_hash=hashlib.sha256(b"null").hexdigest(), release_run_id="run-1",
            )
        else:
            deterministic_cache.put(deterministic_identity, None, serialize=serialize)

    serialize.assert_not_called()
    record_release.assert_not_called()
    if existing:
        with sqlite3.connect(db_path) as connection:
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == before
    else:
        assert not db_path.exists()


@pytest.mark.parametrize("cache_kind", ["released", "deterministic"])
@pytest.mark.parametrize("value", [False, 0, "", [], {}], ids=["false", "zero", "empty-string", "empty-list", "empty-object"])
def test_cache_preserves_non_none_falsey_values(tmp_path: Path, cache_kind: str, value: object) -> None:
    db_path = tmp_path / "cache.sqlite3"
    if cache_kind == "released":
        released_identity = _identity()
        released_cache = ReleasedResultCache(db_path)
        payload = json.dumps(value)
        released_cache.put(
            released_identity, value, serialize=json.dumps,
            released_content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
            release_run_id="run-1",
        )
        actual = released_cache.get(released_identity, deserialize=json.loads)
    else:
        deterministic_identity = DeterministicCacheIdentity(
            program_id="acme", feature="f", request_hash="rh", snapshot_id="snap-1",
            deterministic_engine_version="v1", principal="operator", classification="internal",
        )
        deterministic_cache = DeterministicResultCache(db_path)
        deterministic_cache.put(deterministic_identity, value, serialize=json.dumps)
        actual = deterministic_cache.get(deterministic_identity, deserialize=json.loads)

    assert type(actual) is type(value)
    assert actual == value


def test_released_result_cache_preserves_read_error(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    db_path.write_bytes(b"synthetic-not-a-sqlite-database")
    deserialize = Mock()

    with pytest.raises(StateError, match="Unable to read released-result cache") as error:
        ReleasedResultCache(db_path).get(_identity(), deserialize=deserialize)

    assert isinstance(error.value.__cause__, sqlite3.DatabaseError)
    deserialize.assert_not_called()


def test_released_result_cache_evict_removes_entry(tmp_path: Path) -> None:
    cache = ReleasedResultCache(tmp_path / "cache.sqlite3")
    identity = _identity()
    cache.put(identity, "value", serialize=lambda v: v, released_content_hash=hashlib.sha256(b"value").hexdigest(), release_run_id="run-1")
    assert cache.get(identity, deserialize=lambda v: v) == "value"

    cache.evict(identity)
    assert cache.get(identity, deserialize=lambda v: v) is None


@pytest.mark.parametrize(
    ("payload", "digest"),
    [
        ("tampered-secret", hashlib.sha256(b"valid").hexdigest()),
        ("valid", ""),
        ("valid", "not-a-digest"),
        (b"tampered-secret", hashlib.sha256(b"valid").hexdigest()),
        ("valid", hashlib.sha256(b"valid").hexdigest().encode("ascii")),
    ],
    ids=["altered-payload", "empty-digest", "malformed-digest", "binary-payload", "binary-digest"],
)
def test_released_cache_evicts_invalid_payload_hash_before_deserialization(
    tmp_path: Path, payload: str | bytes, digest: str | bytes,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = ReleasedResultCache(db_path)
    identity = _identity()
    cache.put(identity, "valid", serialize=lambda value: value, released_content_hash=hashlib.sha256(b"valid").hexdigest(), release_run_id="run")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE released_result_cache SET payload = ?, released_content_hash = ? WHERE cache_key = ?",
            (payload, digest, identity.cache_key()),
        )
    deserialize = Mock()

    with pytest.raises(StateError, match="integrity") as error:
        cache.get(identity, deserialize=deserialize)

    assert "tampered-secret" not in str(error.value)
    deserialize.assert_not_called()
    assert cache.get(identity, deserialize=lambda value: value) is None


@pytest.mark.parametrize("digest", ["", "not-a-digest", hashlib.sha256(b"different").hexdigest()])
def test_released_cache_refuses_invalid_write_digest_without_replacing_entry(tmp_path: Path, digest: str) -> None:
    cache = ReleasedResultCache(tmp_path / "cache.sqlite3")
    identity = _identity()
    cache.put(identity, "original", serialize=lambda value: value, released_content_hash=hashlib.sha256(b"original").hexdigest(), release_run_id="run")

    with pytest.raises(StateError, match="integrity"):
        cache.put(identity, "new-secret", serialize=lambda value: value, released_content_hash=digest, release_run_id="run")

    assert cache.get(identity, deserialize=lambda value: value) == "original"


@pytest.mark.parametrize("serialized", [b"private-data", "\ud800"], ids=["binary", "invalid-utf8"])
def test_released_cache_refuses_invalid_serialized_payload_without_replacing_entry(
    tmp_path: Path, serialized: str | bytes,
) -> None:
    cache = ReleasedResultCache(tmp_path / "cache.sqlite3")
    identity = _identity()
    cache.put(identity, "original", serialize=lambda value: value, released_content_hash=hashlib.sha256(b"original").hexdigest(), release_run_id="run")

    with pytest.raises(StateError, match="integrity") as error:
        cache.put(identity, "new", serialize=Mock(return_value=serialized), released_content_hash="irrelevant", release_run_id="run")

    assert "private-data" not in "".join(traceback.format_exception(error.value))
    assert cache.get(identity, deserialize=lambda value: value) == "original"


@pytest.mark.parametrize("payload", ["", "valid-\u03b1"], ids=["empty", "utf8"])
def test_released_cache_round_trips_utf8_payload(tmp_path: Path, payload: str) -> None:
    cache = ReleasedResultCache(tmp_path / "cache.sqlite3")
    identity = _identity()
    cache.put(identity, payload, serialize=lambda value: value, released_content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(), release_run_id="run")

    assert cache.get(identity, deserialize=lambda value: value) == payload


@pytest.mark.parametrize("corrupt_field", ["payload", "digest"])
def test_released_cache_preserves_concurrent_valid_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corrupt_field: str,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = ReleasedResultCache(db_path)
    identity = _identity()
    cache.put(identity, "valid", serialize=lambda value: value, released_content_hash=hashlib.sha256(b"valid").hexdigest(), release_run_id="run")
    with sqlite3.connect(db_path) as connection:
        if corrupt_field == "payload":
            connection.execute("UPDATE released_result_cache SET payload = ?", ("corrupt",))
        else:
            connection.execute("UPDATE released_result_cache SET released_content_hash = ?", ("corrupt",))
    payload_at_read = b"corrupt" if corrupt_field == "payload" else b"valid"
    replacement = "replacement" if corrupt_field == "payload" else "valid"
    replacement_hash = hashlib.sha256(replacement.encode("utf-8")).hexdigest()
    real_sha256 = hashlib.sha256
    replaced = False

    def replace_before_digest(data: bytes):
        nonlocal replaced
        if data == payload_at_read and not replaced:
            replaced = True
            cache.put(identity, replacement, serialize=lambda value: value, released_content_hash=replacement_hash, release_run_id="replacement-run")
        return real_sha256(data)

    monkeypatch.setattr("src.ai.structured_feature_runner.hashlib.sha256", replace_before_digest)
    with pytest.raises(StateError, match="integrity"):
        cache.get(identity, deserialize=lambda value: value)

    assert replaced
    assert cache.get(identity, deserialize=lambda value: value) == replacement


@pytest.mark.parametrize("failure", ["integrity", "json"])
@pytest.mark.parametrize("column", ["feature", "principal", "classification", "release_run_id", "cached_at"])
def test_invalid_cache_eviction_preserves_metadata_only_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, column: str,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = ReleasedResultCache(db_path)
    identity = _identity()
    key = identity.cache_key()
    payload = '{"synthetic":'
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    cache.put(identity, payload, serialize=lambda value: value, released_content_hash=digest, release_run_id="run")
    if failure == "integrity":
        with sqlite3.connect(db_path) as connection:
            connection.execute("UPDATE released_result_cache SET released_content_hash = ?", ("invalid",))

    fields = ("feature", "principal", "classification", "payload", "released_content_hash", "release_run_id", "cached_at")
    with sqlite3.connect(db_path) as connection:
        original = connection.execute(
            "SELECT feature, principal, classification, payload, released_content_hash, release_run_id, cached_at "
            "FROM released_result_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
    expected = list(original)
    expected[fields.index(column)] = "replacement"
    replaced = False

    def replace_metadata() -> None:
        nonlocal replaced
        with sqlite3.connect(db_path) as connection:
            connection.execute(
                f"UPDATE released_result_cache SET {column} = ? WHERE cache_key = ?",
                ("replacement", key),
            )
        replaced = True

    if failure == "integrity":
        real_sha256 = hashlib.sha256

        def replace_before_digest(data: bytes):
            if data == payload.encode("utf-8") and not replaced:
                replace_metadata()
            return real_sha256(data)

        monkeypatch.setattr("src.ai.structured_feature_runner.hashlib.sha256", replace_before_digest)
        deserialize = Mock()
    else:
        def replace_before_decode(value: str) -> object:
            replace_metadata()
            return json.loads(value)

        deserialize = Mock(side_effect=replace_before_decode)

    with pytest.raises(StateError, match="integrity" if failure == "integrity" else "JSON"):
        cache.get(identity, deserialize=deserialize)

    assert replaced
    if failure == "integrity":
        deserialize.assert_not_called()
    with sqlite3.connect(db_path) as connection:
        actual = connection.execute(
            "SELECT feature, principal, classification, payload, released_content_hash, release_run_id, cached_at "
            "FROM released_result_cache WHERE cache_key = ?",
            (key,),
        ).fetchone()
    assert actual == tuple(expected)


def test_released_cache_surfaces_eviction_failure_without_deserializing(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = ReleasedResultCache(db_path)
    identity = _identity()
    cache.put(identity, "valid", serialize=lambda value: value, released_content_hash=hashlib.sha256(b"valid").hexdigest(), release_run_id="run")
    with sqlite3.connect(db_path) as connection:
        connection.execute("UPDATE released_result_cache SET payload = ?", ("tampered-secret",))
        connection.execute(
            "CREATE TRIGGER reject_cache_delete BEFORE DELETE ON released_result_cache "
            "BEGIN SELECT RAISE(ABORT, 'private-trigger-detail'); END"
        )
    deserialize = Mock()

    with pytest.raises(StateError) as error:
        cache.get(identity, deserialize=deserialize)

    diagnostic = "".join(traceback.format_exception(error.value))
    assert "tampered-secret" not in diagnostic
    assert "private-trigger-detail" not in diagnostic
    deserialize.assert_not_called()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT payload FROM released_result_cache").fetchone()[0] == "tampered-secret"


@pytest.mark.parametrize("stage", ["origin", "json"])
@pytest.mark.parametrize("execution", ["single", "per-item-batch"])
@pytest.mark.parametrize("failure_type", [PermissionError, FileNotFoundError, sqlite3.OperationalError])
def test_runner_released_cache_eviction_reopen_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, execution: str, failure_type: type[Exception],
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    provider = Mock(return_value="private-cached-value")
    fallback = Mock(return_value=_fallback_released())
    ledger = Mock(spec=CallCeilingLedger)
    record_release = Mock(return_value="origin-run")
    verify_release = Mock(return_value=stage == "json")
    decoder = Mock(side_effect=json.loads)
    cache = ReleasedResultCache(db_path, record_release=record_release, verify_release=verify_release)
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger, released_cache=cache,
    )
    spec = _cacheable_spec(
        tmp_path, call_provider=provider, fallback_handler=fallback,
        serialize_response=json.dumps, deserialize_response=decoder,
    )
    assert runner.run(spec, "request").value == "private-cached-value"
    with sqlite3.connect(db_path) as connection:
        if stage == "json":
            payload = '{"private-example":'
            connection.execute(
                "UPDATE released_result_cache SET payload = ?, released_content_hash = ?",
                (payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()),
            )
        before = connection.execute("SELECT * FROM released_result_cache").fetchall()

    module = importlib.import_module("src.ai.structured_feature_runner")
    open_database = module.open_program_db
    failure = failure_type("private-path-detail")
    opens = 0

    def fail_eviction_open(path: Path, **kwargs):
        nonlocal opens
        if path == db_path:
            opens += 1
            if opens == 2:
                raise failure
        return open_database(path, **kwargs)

    monkeypatch.setattr(module, "open_program_db", fail_eviction_open)
    with pytest.raises(StateError, match="^Unable to evict invalid released-result cache entry\\.$") as error:
        if execution == "single":
            runner.run(spec, "request")
        else:
            runner.run_batch(spec, ["request"])

    diagnostic = "".join(traceback.format_exception(error.value))
    assert "private-cached-value" not in diagnostic
    assert "private-example" not in diagnostic
    assert "private-path-detail" not in diagnostic
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__
    assert opens == 2
    assert decoder.call_count == (1 if stage == "origin" else 2)
    verify_release.assert_called_once()
    provider.assert_called_once()
    ledger.reserve_frontier_call.assert_called_once()
    record_release.assert_called_once()
    fallback.assert_not_called()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT * FROM released_result_cache").fetchall() == before


@pytest.mark.parametrize("stage", ["read", "verify", "decode"])
def test_released_cache_oserror_outside_eviction_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    failure = PermissionError("synthetic-outside-eviction")
    verify = Mock(side_effect=failure if stage == "verify" else None, return_value=True)
    decoder = Mock(side_effect=failure if stage == "decode" else json.loads)
    cache = ReleasedResultCache(db_path, record_release=Mock(return_value="origin"), verify_release=verify)
    payload = json.dumps("value")
    cache.put(
        _identity(), payload, serialize=lambda value: value,
        released_content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(), release_run_id="run",
    )
    with sqlite3.connect(db_path) as connection:
        before = connection.execute("SELECT * FROM released_result_cache").fetchall()
    if stage == "read":
        module = importlib.import_module("src.ai.structured_feature_runner")
        monkeypatch.setattr(module, "open_program_db", Mock(side_effect=failure))

    with pytest.raises(PermissionError) as error:
        cache.get(_identity(), deserialize=decoder)

    assert error.value is failure
    assert verify.call_count == (0 if stage == "read" else 1)
    assert decoder.call_count == (1 if stage == "decode" else 0)
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT * FROM released_result_cache").fetchall() == before


@pytest.mark.parametrize("payload", ['{"private-example":', "private-example"])
def test_released_cache_evicts_malformed_json_with_matching_digest(tmp_path: Path, payload: str) -> None:
    cache = ReleasedResultCache(tmp_path / "cache.sqlite3")
    identity = _identity()
    cache.put(
        identity, payload, serialize=lambda value: value,
        released_content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(), release_run_id="run",
    )

    with pytest.raises(StateError, match="JSON") as error:
        cache.get(identity, deserialize=json.loads)

    assert "private-example" not in "".join(traceback.format_exception(error.value))
    assert cache.get(identity, deserialize=json.loads) is None


def test_malformed_json_cache_stops_runner_before_provider_or_reservation(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = ReleasedResultCache(db_path)
    provider = Mock(return_value="released")
    ledger = Mock(spec=CallCeilingLedger)
    runner = StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=True,
        ceiling_ledger=ledger,
        released_cache=cache,
        deterministic_cache=DeterministicResultCache(tmp_path / "deterministic.sqlite3"),
    )
    spec = _cacheable_spec(
        tmp_path, call_provider=provider, serialize_response=json.dumps, deserialize_response=json.loads,
    )
    assert runner.run(spec, "request").value == "released"
    malformed = '{"private-example":'
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE released_result_cache SET payload = ?, released_content_hash = ?",
            (malformed, hashlib.sha256(malformed.encode("utf-8")).hexdigest()),
        )

    with pytest.raises(StateError, match="JSON") as error:
        runner.run(spec, "request")

    assert "private-example" not in "".join(traceback.format_exception(error.value))
    assert provider.call_count == 1
    assert ledger.reserve_frontier_call.call_count == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 0

    assert runner.run(spec, "request").value == "released"
    assert provider.call_count == 2
    assert ledger.reserve_frontier_call.call_count == 2


def test_malformed_json_eviction_preserves_concurrent_replacement(tmp_path: Path) -> None:
    cache = ReleasedResultCache(tmp_path / "cache.sqlite3")
    identity = _identity()
    malformed = '{"bad":'
    cache.put(
        identity, malformed, serialize=lambda value: value,
        released_content_hash=hashlib.sha256(malformed.encode("utf-8")).hexdigest(), release_run_id="run",
    )
    replacement = '{"valid":true}'

    def replace_before_decode(payload: str) -> object:
        cache.put(
            identity, replacement, serialize=lambda value: value,
            released_content_hash=hashlib.sha256(replacement.encode("utf-8")).hexdigest(),
            release_run_id="replacement",
        )
        return json.loads(payload)

    with pytest.raises(StateError, match="JSON"):
        cache.get(identity, deserialize=replace_before_decode)

    assert cache.get(identity, deserialize=json.loads) == {"valid": True}


def test_malformed_json_eviction_preserves_same_payload_cache_rewrite(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = ReleasedResultCache(db_path)
    identity = _identity()
    payload = '{"synthetic":'
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    cache.put(identity, payload, serialize=lambda value: value, released_content_hash=digest, release_run_id="run")

    def rewrite_before_decode(value: str) -> object:
        cache.put(identity, value, serialize=lambda item: item, released_content_hash=digest, release_run_id="replacement")
        return json.loads(value)

    with pytest.raises(StateError, match="JSON"):
        cache.get(identity, deserialize=rewrite_before_decode)

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT payload, released_content_hash, release_run_id FROM released_result_cache WHERE cache_key = ?",
            (identity.cache_key(),),
        ).fetchone()
    assert row == (payload, digest, "replacement")


def test_malformed_json_eviction_failure_is_safe_and_preserves_entry(tmp_path: Path) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = ReleasedResultCache(db_path)
    identity = _identity()
    payload = '{"private-example":'
    cache.put(
        identity, payload, serialize=lambda value: value,
        released_content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(), release_run_id="run",
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "CREATE TRIGGER reject_cache_delete BEFORE DELETE ON released_result_cache "
            "BEGIN SELECT RAISE(ABORT, 'private-trigger-detail'); END"
        )

    with pytest.raises(StateError, match="evict") as error:
        cache.get(identity, deserialize=json.loads)

    diagnostic = "".join(traceback.format_exception(error.value))
    assert "private-example" not in diagnostic
    assert "private-trigger-detail" not in diagnostic
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT payload FROM released_result_cache").fetchone()[0] == payload


@pytest.mark.parametrize("failure", ["nested-json", "valid-json", "programming"])
def test_deserializer_errors_do_not_evict_valid_cached_json(tmp_path: Path, failure: str) -> None:
    cache = ReleasedResultCache(tmp_path / "cache.sqlite3")
    identity = _identity()
    payload = '{"nested":"not-json"}'
    cache.put(
        identity, payload, serialize=lambda value: value,
        released_content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(), release_run_id="run",
    )

    def deserialize(value: str) -> object:
        if failure == "nested-json":
            return json.loads(json.loads(value)["nested"])
        if failure == "valid-json":
            raise json.JSONDecodeError("Deserializer error", value, 0)
        raise RuntimeError("Deserializer programming error")

    expected_error = RuntimeError if failure == "programming" else json.JSONDecodeError
    with pytest.raises(expected_error):
        cache.get(identity, deserialize=deserialize)

    assert cache.get(identity, deserialize=json.loads) == {"nested": "not-json"}


@pytest.mark.parametrize("verification_error", [ValueError, RecursionError])
def test_unconfirmed_json_error_preserves_deserializer_failure_and_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verification_error: type[Exception],
) -> None:
    cache = ReleasedResultCache(tmp_path / "cache.sqlite3")
    identity = _identity()
    payload = '{"private-example":'
    cache.put(
        identity, payload, serialize=lambda value: value,
        released_content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(), release_run_id="run",
    )
    expected = json.JSONDecodeError("decoder failure", payload, 0)

    def deserialize(value: str) -> object:
        raise expected

    with monkeypatch.context() as patch:
        patch.setattr("src.ai.structured_feature_runner.json.loads", Mock(side_effect=verification_error("verification failure")))
        with pytest.raises(json.JSONDecodeError) as error:
            cache.get(identity, deserialize=deserialize)

    assert error.value is expected
    assert cache.get(identity, deserialize=lambda value: value) == payload


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


@pytest.mark.parametrize("truthiness", ["false", "raises"])
@pytest.mark.parametrize("execution", ["single", "native-batch"])
def test_runner_uses_supplied_ceiling_ledger_without_truth_testing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, truthiness: str, execution: str,
) -> None:
    class Ledger(CallCeilingLedger):
        def __bool__(self) -> bool:
            if truthiness == "raises":
                raise AssertionError("ledger truth tested")
            return False

    default_path = tmp_path / "default-ceiling.sqlite3"
    monkeypatch.setattr(
        "src.ai.structured_feature_runner.call_ceiling_ledger_path", lambda _program: default_path,
    )
    ledger = Ledger(tmp_path / "supplied-ceiling.sqlite3")
    ledger.reserve_frontier_call(
        scope_key="acme:run-1:test_feature:v1", remote_ceiling=1, frontier_ceiling=1,
    )
    provider = Mock(return_value="frontier")
    if execution == "single":
        spec = _spec(
            tmp_path, build_context=lambda _: ContextBuildResult(compiled=_compiled()),
            call_provider=provider, max_remote_calls_per_run=1, max_frontier_calls_per_run=1,
        )
    else:
        spec = _batchable_spec(
            tmp_path, call_provider_batch=provider, max_remote_calls_per_run=1, max_frontier_calls_per_run=1,
        )
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger,
        released_cache=ReleasedResultCache(tmp_path / "released.sqlite3"),
        deterministic_cache=DeterministicResultCache(tmp_path / "deterministic.sqlite3"),
    )

    outcomes = (
        [runner.run(spec, "request")]
        if execution == "single" else list(runner.run_batch(spec, ["request"]).values())
    )

    assert len(outcomes) == 1
    assert outcomes[0].status == FeatureOutcomeStatus.DETERMINISTIC_FALLBACK
    assert "remote_cap_exhausted" in outcomes[0].limitations
    provider.assert_not_called()
    assert not default_path.exists()


def test_runner_constructs_default_stores_for_none_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    for kind in ("call_ceiling_ledger", "released_result_cache", "deterministic_result_cache"):
        monkeypatch.setattr(
            f"src.ai.structured_feature_runner.{kind}_path",
            lambda _program, kind=kind: tmp_path / f"{kind}.sqlite3",
        )
    runner = StructuredFeatureRunner(context=_context(tmp_path), frontier_eligible=True)
    handler = Mock(return_value=TierResult(value="deterministic", confidence=1.0))
    deterministic = _det_cacheable_spec(tmp_path, deterministic_handler=handler)
    provider = Mock(return_value="frontier")
    released = _cacheable_spec(tmp_path, call_provider=provider, max_remote_calls_per_run=1)

    assert runner.run(deterministic, "det").value == "deterministic"
    assert runner.run(deterministic, "det").tier == Tier.CACHE
    assert runner.run(released, "frontier").value == "frontier"
    assert runner.run(released, "frontier").tier == Tier.CACHE
    handler.assert_called_once()
    provider.assert_called_once()
    assert all(
        (tmp_path / f"{kind}.sqlite3").exists()
        for kind in ("call_ceiling_ledger", "released_result_cache", "deterministic_result_cache")
    )


@pytest.mark.parametrize("truthiness", ["false", "raises"])
@pytest.mark.parametrize("execution", ["single", "native-batch"])
def test_runner_uses_supplied_local_router_mapping_without_truth_testing(
    tmp_path: Path, truthiness: str, execution: str,
) -> None:
    class Routers(dict):
        def __bool__(self) -> bool:
            if truthiness == "raises":
                raise AssertionError("router mapping truth tested")
            return False

    router = Mock()
    router.route.return_value = TierResult(value="local", confidence=1.0)
    provider = Mock(
        return_value=(
            "frontier" if execution == "single" else BatchProviderResult(responses={"request": "frontier"})
        ),
    )
    routers = Routers({"test-router": router})
    runner = _runner(tmp_path, local_routers=routers)
    if execution == "single":
        spec = _spec(
            tmp_path, local_router_name="test-router",
            build_context=lambda _: ContextBuildResult(compiled=_compiled()), call_provider=provider,
        )
        outcomes = [runner.run(spec, "request")]
    else:
        spec = _batchable_spec(tmp_path, local_router_name="test-router", call_provider_batch=provider)
        outcomes = list(runner.run_batch(spec, ["request"]).values())

    assert len(outcomes) == 1
    assert outcomes[0].value == "local"
    assert outcomes[0].tier == Tier.LOCAL
    router.route.assert_called_once_with("request")
    provider.assert_not_called()


def test_runner_retains_initially_empty_local_router_mapping(tmp_path: Path) -> None:
    routers = {}
    runner = _runner(tmp_path, local_routers=routers)
    router = Mock()
    router.route.return_value = TierResult(value="local", confidence=1.0)
    routers["test-router"] = router

    outcome = runner.run(_spec(tmp_path, local_router_name="test-router"), "request")

    assert outcome.value == "local"
    assert outcome.tier == Tier.LOCAL
    router.route.assert_called_once_with("request")


@pytest.mark.parametrize("truthiness", ["false", "raises"])
@pytest.mark.parametrize("cache_kind", ["released", "deterministic"])
def test_runner_uses_supplied_cache_without_truth_testing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, truthiness: str, cache_kind: str,
) -> None:
    class FalseValued:
        def __bool__(self) -> bool:
            if truthiness == "raises":
                raise AssertionError("cache truth tested")
            return False

    class ReleasedCache(FalseValued, ReleasedResultCache):
        pass

    class DeterministicCache(FalseValued, DeterministicResultCache):
        pass

    default_path = tmp_path / "default-cache.sqlite3"
    monkeypatch.setattr(
        f"src.ai.structured_feature_runner.{cache_kind}_result_cache_path", lambda _program: default_path,
    )
    provider = Mock(return_value="frontier")
    handler = Mock(return_value=TierResult(value="deterministic", confidence=1.0))
    record_release = Mock(return_value="origin")
    verify_release = Mock(return_value=True)
    if cache_kind == "released":
        released_cache = ReleasedCache(
            tmp_path / "supplied-cache.sqlite3", record_release=record_release, verify_release=verify_release,
        )
        runner = _runner(tmp_path, released_cache=released_cache)
        spec = _cacheable_spec(tmp_path, call_provider=provider)
    else:
        deterministic_cache = DeterministicCache(tmp_path / "supplied-cache.sqlite3")
        runner = _runner(tmp_path, deterministic_cache=deterministic_cache)
        spec = _det_cacheable_spec(tmp_path, deterministic_handler=handler)

    first = runner.run(spec, "request")
    second = runner.run(spec, "request")

    assert first.value == second.value == ("frontier" if cache_kind == "released" else "deterministic")
    assert second.tier == Tier.CACHE
    assert provider.call_count == int(cache_kind == "released")
    assert handler.call_count == int(cache_kind == "deterministic")
    assert record_release.call_count == int(cache_kind == "released")
    assert verify_release.call_count == int(cache_kind == "released")
    assert (tmp_path / "supplied-cache.sqlite3").exists()
    assert not default_path.exists()


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


@pytest.mark.parametrize("column", ["feature", "principal", "classification"])
def test_deterministic_result_cache_checks_stored_identity_on_read(tmp_path: Path, column: str) -> None:
    db_path = tmp_path / "det_cache.sqlite3"
    cache = DeterministicResultCache(db_path)
    identity = DeterministicCacheIdentity(
        program_id="acme", feature="f", request_hash="rh", snapshot_id="snap-1",
        deterministic_engine_version="v1", principal="operator", classification="internal",
    )
    cache.put(identity, "secret-value", serialize=lambda value: value)
    assert cache.get(identity, deserialize=lambda value: value) == "secret-value"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            f"UPDATE deterministic_result_cache SET {column} = ? WHERE cache_key = ?",
            ("different", identity.cache_key()),
        )
        before = connection.execute("SELECT * FROM deterministic_result_cache").fetchall()

    deserialize = Mock(side_effect=AssertionError("mismatched cache value decoded"))
    assert cache.get(identity, deserialize=deserialize) is None
    deserialize.assert_not_called()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT * FROM deterministic_result_cache").fetchall() == before

    cache.put(identity, "fresh-value", serialize=lambda value: value)
    assert cache.get(identity, deserialize=lambda value: value) == "fresh-value"


@pytest.mark.parametrize(
    ("cache_kind", "failure"),
    [("released", "release"), ("released", "hash"), ("deterministic", "serialize")],
)
def test_cache_failed_refresh_preserves_identity_mismatch(
    tmp_path: Path, cache_kind: str, failure: str,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    recorder = Mock(return_value="old-run")
    if cache_kind == "released":
        released_identity = _identity()
        released_cache = ReleasedResultCache(
            db_path, record_release=recorder, verify_release=lambda *_: True,
        )
        released_cache.put(
            released_identity, "old-value", serialize=lambda value: value,
            released_content_hash=hashlib.sha256(b"old-value").hexdigest(),
            release_run_id="old-run",
        )
        table = "released_result_cache"
    else:
        deterministic_identity = DeterministicCacheIdentity(
            program_id="acme", feature="f", request_hash="rh", snapshot_id="snap-1",
            deterministic_engine_version="v1", principal="operator", classification="internal",
        )
        deterministic_cache = DeterministicResultCache(db_path)
        deterministic_cache.put(deterministic_identity, "old-value", serialize=lambda value: value)
        table = "deterministic_result_cache"

    with sqlite3.connect(db_path) as connection:
        connection.execute(f"UPDATE {table} SET principal = ?", ("different",))
        before = connection.execute(f"SELECT * FROM {table}").fetchall()
    recorder.reset_mock()
    if failure == "release":
        recorder.side_effect = StateError("release refused")
    with pytest.raises(RuntimeError if failure == "serialize" else StateError):
        if cache_kind == "released":
            released_cache.put(
                released_identity, "fresh-value", serialize=lambda value: value,
                released_content_hash=(
                    "invalid" if failure == "hash" else hashlib.sha256(b"fresh-value").hexdigest()
                ),
                release_run_id="fresh-run",
            )
        else:
            deterministic_cache.put(
                deterministic_identity, "fresh-value",
                serialize=Mock(side_effect=RuntimeError("serialization failed")),
            )
    assert recorder.call_count == int(failure == "release")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"SELECT * FROM {table}").fetchall() == before


@pytest.mark.parametrize("column", ["feature", "principal", "classification"])
@pytest.mark.parametrize(
    ("cache_kind", "execution"),
    [
        ("released", "single"),
        ("released", "per-item-batch"),
        ("deterministic", "single"),
        ("deterministic", "per-item-batch"),
        ("deterministic", "native-batch"),
    ],
)
def test_runner_identity_mismatch_refreshes_before_reusing_cached_value(
    tmp_path: Path, cache_kind: str, execution: str, column: str,
) -> None:
    deserialize = Mock(side_effect=lambda value: value)
    provider = Mock(side_effect=["old-value", "fresh-1", "fresh-2"])
    handler = Mock(side_effect=[
        TierResult(value="old-value", confidence=1.0),
        TierResult(value="fresh-1", confidence=1.0),
        TierResult(value="fresh-2", confidence=1.0),
    ])
    runner = _runner(tmp_path)
    if cache_kind == "released":
        spec = _cacheable_spec(tmp_path, call_provider=provider, deserialize_response=deserialize)
        db_path = tmp_path / "released_cache.sqlite3"
        table = "released_result_cache"
        expected_tier = Tier.FRONTIER
    else:
        spec = _det_cacheable_spec(tmp_path, deterministic_handler=handler, deserialize_response=deserialize)
        db_path = tmp_path / "deterministic_cache.sqlite3"
        table = "deterministic_result_cache"
        expected_tier = Tier.DETERMINISTIC
        if execution == "native-batch":
            spec = replace(
                spec, item_id=lambda request: request,
                build_batch_context=Mock(side_effect=AssertionError("unexpected batch context")),
                call_provider_batch=Mock(side_effect=AssertionError("unexpected batch provider")),
            )

    assert runner.run(spec, "request").value == "old-value"
    deserialize.reset_mock()
    with sqlite3.connect(db_path) as connection:
        original_identity = connection.execute(
            f"SELECT feature, principal, classification FROM {table}"
        ).fetchone()
        connection.execute(f"UPDATE {table} SET {column} = ?", ("different",))

    for expected_tier_after_refresh in (expected_tier, Tier.CACHE):
        if execution == "single":
            outcome = runner.run(spec, "request")
        else:
            results = runner.run_batch(spec, ["request"])
            outcome = next(iter(results.values()))
        assert outcome.value == "fresh-1"
        assert outcome.tier == expected_tier_after_refresh

    assert [call.args[0] for call in deserialize.call_args_list] == ["fresh-1", "fresh-1"]
    assert (provider if cache_kind == "released" else handler).call_count == 2
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            f"SELECT feature, principal, classification FROM {table}"
        ).fetchone() == original_identity


@pytest.mark.parametrize("failure", ["malformed-json", "custom-error"])
def test_deterministic_cache_preserves_decoder_errors_and_row(tmp_path: Path, failure: str) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = DeterministicResultCache(db_path)
    identity = DeterministicCacheIdentity(
        program_id="acme", feature="f", request_hash="rh", snapshot_id="snap-1",
        deterministic_engine_version="v1", principal="operator", classification="internal",
    )
    payload = "{" if failure == "malformed-json" else '"value"'
    cache.put(identity, payload, serialize=lambda value: value)
    with sqlite3.connect(db_path) as connection:
        before = connection.execute("SELECT * FROM deterministic_result_cache").fetchall()
    custom_error = RuntimeError("synthetic decoder failure")
    deserialize = json.loads if failure == "malformed-json" else Mock(side_effect=custom_error)

    with pytest.raises(json.JSONDecodeError if failure == "malformed-json" else RuntimeError) as error:
        cache.get(identity, deserialize=deserialize)

    if failure == "custom-error":
        assert error.value is custom_error
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT * FROM deterministic_result_cache").fetchall() == before


@pytest.mark.parametrize(
    ("cache_kind", "execution"),
    [
        ("released", "single"),
        ("released", "per-item-batch"),
        ("deterministic", "single"),
        ("deterministic", "per-item-batch"),
        ("deterministic", "native-batch"),
    ],
)
@pytest.mark.parametrize("decode_kind", ["json-null", "decoder-none"])
def test_runner_rejects_decoded_none_before_new_execution(
    tmp_path: Path, cache_kind: str, execution: str, decode_kind: str,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    ledger = Mock(spec=CallCeilingLedger)
    provider = Mock(return_value="frontier-value")
    handler = Mock(return_value=TierResult(value="deterministic-value", confidence=1.0))
    fallback = Mock(return_value=_fallback_released())
    batch_builder = Mock()
    batch_provider = Mock()
    deserialize = Mock(side_effect=json.loads)
    runner = StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=True,
        ceiling_ledger=ledger,
        released_cache=ReleasedResultCache(db_path),
        deterministic_cache=DeterministicResultCache(db_path),
    )
    if cache_kind == "released":
        spec = _cacheable_spec(
            tmp_path, call_provider=provider, fallback_handler=fallback,
            serialize_response=json.dumps, deserialize_response=deserialize,
        )
        table = "released_result_cache"
        assert runner.run(spec, "request").value == "frontier-value"
    else:
        spec = _det_cacheable_spec(
            tmp_path, deterministic_handler=handler, fallback_handler=fallback,
            serialize_response=json.dumps, deserialize_response=deserialize,
        )
        if execution == "native-batch":
            spec = replace(
                spec, item_id=lambda request: request,
                build_batch_context=batch_builder, call_provider_batch=batch_provider,
            )
        table = "deterministic_result_cache"
        assert runner.run(spec, "request").value == "deterministic-value"

    deserialize.reset_mock()
    if decode_kind == "decoder-none":
        deserialize.side_effect = lambda _: None
    payload = "null" if decode_kind == "json-null" else '"synthetic-private-marker"'
    with sqlite3.connect(db_path) as connection:
        if cache_kind == "released":
            connection.execute(
                "UPDATE released_result_cache SET payload = ?, released_content_hash = ?",
                (payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()),
            )
        else:
            connection.execute("UPDATE deterministic_result_cache SET payload = ?", (payload,))
        before = connection.execute(f"SELECT * FROM {table}").fetchall()

    with pytest.raises(StateError) as error:
        if execution == "single":
            runner.run(spec, "request")
        else:
            runner.run_batch(spec, ["request"])

    assert str(error.value) == f"{cache_kind.capitalize()}-result cache decoder returned no value."
    assert error.value.__cause__ is None
    deserialize.assert_called_once_with(payload)
    assert provider.call_count == int(cache_kind == "released")
    assert handler.call_count == int(cache_kind == "deterministic")
    assert ledger.reserve_frontier_call.call_count == int(cache_kind == "released")
    fallback.assert_not_called()
    batch_builder.assert_not_called()
    batch_provider.assert_not_called()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"SELECT * FROM {table}").fetchall() == before


@pytest.mark.parametrize(
    ("cache_kind", "execution"),
    [
        ("released", "single"),
        ("released", "per-item-batch"),
        ("deterministic", "single"),
        ("deterministic", "per-item-batch"),
        ("deterministic", "native-batch"),
    ],
)
@pytest.mark.parametrize("decoded", [False, 0, [], {"marker": "synthetic-private"}])
def test_runner_rejects_wrong_decoded_cache_type_without_new_execution(
    tmp_path: Path, cache_kind: str, execution: str, decoded: object,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    events: list[str] = []
    provider = Mock(return_value="frontier-value")
    handler = Mock(return_value=TierResult(value="deterministic-value", confidence=1.0))
    validator = Mock(return_value=ValidationResult(valid=True))
    fallback = Mock(return_value=_fallback_released())
    batch_builder = Mock()
    batch_provider = Mock()
    record_release = Mock(return_value="origin-run")

    def verify(identity: CacheIdentity, payload: str, run_id: str) -> bool:
        events.append("verify")
        return True

    def decode(payload: str) -> object:
        events.append("decode")
        return json.loads(payload)

    verify_release = Mock(side_effect=verify)
    deserialize = Mock(side_effect=decode)
    ledger = Mock(spec=CallCeilingLedger)
    decisions = Mock()
    runner = StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=True,
        ceiling_ledger=ledger,
        released_cache=ReleasedResultCache(
            db_path, record_release=record_release, verify_release=verify_release,
        ),
        deterministic_cache=DeterministicResultCache(db_path),
        decision_sink=decisions,
    )
    if cache_kind == "released":
        spec = _cacheable_spec(
            tmp_path, call_provider=provider, fallback_handler=fallback,
            semantic_validator=validator, serialize_response=json.dumps, deserialize_response=deserialize,
        )
        table = "released_result_cache"
        assert runner.run(spec, "request").value == "frontier-value"
    else:
        spec = _det_cacheable_spec(
            tmp_path, deterministic_handler=handler, fallback_handler=fallback,
            semantic_validator=validator, serialize_response=json.dumps, deserialize_response=deserialize,
        )
        if execution == "native-batch":
            spec = replace(
                spec, item_id=lambda request: request,
                build_batch_context=batch_builder, call_provider_batch=batch_provider,
            )
        table = "deterministic_result_cache"
        assert runner.run(spec, "request").value == "deterministic-value"

    events.clear()
    deserialize.reset_mock()
    validator.reset_mock()
    record_release.reset_mock()
    decisions.reset_mock()
    payload = json.dumps(decoded)
    with sqlite3.connect(db_path) as connection:
        if cache_kind == "released":
            connection.execute(
                "UPDATE released_result_cache SET payload = ?, released_content_hash = ?",
                (payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()),
            )
        else:
            connection.execute("UPDATE deterministic_result_cache SET payload = ?", (payload,))
        before = connection.execute(f"SELECT * FROM {table}").fetchall()

    with pytest.raises(StateError) as error:
        if execution == "single":
            runner.run(spec, "request")
        else:
            runner.run_batch(spec, ["request"])

    assert str(error.value) == f"{cache_kind.capitalize()}-result cache decoder returned an unexpected response type."
    assert error.value.__cause__ is None
    assert events == (["verify", "decode"] if cache_kind == "released" else ["decode"])
    deserialize.assert_called_once_with(payload)
    validator.assert_not_called()
    record_release.assert_not_called()
    fallback.assert_not_called()
    decisions.assert_not_called()
    batch_builder.assert_not_called()
    batch_provider.assert_not_called()
    assert provider.call_count == int(cache_kind == "released")
    assert handler.call_count == int(cache_kind == "deterministic")
    assert ledger.reserve_frontier_call.call_count == int(cache_kind == "released")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"SELECT * FROM {table}").fetchall() == before


@pytest.mark.parametrize(
    ("cache_kind", "execution"),
    [
        ("released", "single"),
        ("released", "per-item-batch"),
        ("deterministic", "single"),
        ("deterministic", "per-item-batch"),
        ("deterministic", "native-batch"),
    ],
)
@pytest.mark.parametrize("decoded", [False, 0, [], {"marker": "synthetic-private"}])
@pytest.mark.parametrize("retained", [False, True], ids=["no-row", "retained-row"])
def test_runner_rejects_wrong_decoded_write_type_before_release_or_storage(
    tmp_path: Path, cache_kind: str, execution: str, decoded: object, retained: bool,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    payload = json.dumps(decoded)
    serialize = Mock(return_value=payload)
    deserialize = Mock(side_effect=json.loads)
    provider = Mock(return_value="frontier-value")
    handler = Mock(return_value=TierResult(value="deterministic-value", confidence=1.0))
    validator = Mock(return_value=ValidationResult(valid=True))
    fallback = Mock(return_value=_fallback_released())
    batch_builder = Mock()
    batch_provider = Mock()
    record_release = Mock(return_value="origin-run")
    verify_release = Mock(return_value=True)
    ledger = Mock(spec=CallCeilingLedger)
    decisions = Mock()
    runner = StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=True,
        ceiling_ledger=ledger,
        released_cache=ReleasedResultCache(
            db_path, record_release=record_release, verify_release=verify_release,
        ),
        deterministic_cache=DeterministicResultCache(db_path),
        decision_sink=decisions,
    )
    if cache_kind == "released":
        spec = _cacheable_spec(
            tmp_path, call_provider=provider, fallback_handler=fallback,
            semantic_validator=validator, serialize_response=serialize, deserialize_response=deserialize,
        )
        table = "released_result_cache"
        if retained:
            released_identity = runner._cache_identity(spec, "request", ContextBuildResult(compiled=_compiled()))
            assert released_identity is not None
            old_payload = '"retained"'
            ReleasedResultCache(db_path).put(
                released_identity, old_payload, serialize=lambda value: value,
                released_content_hash=hashlib.sha256(old_payload.encode("utf-8")).hexdigest(),
                release_run_id="old-run",
            )
    else:
        spec = _det_cacheable_spec(
            tmp_path, deterministic_handler=handler, fallback_handler=fallback,
            semantic_validator=validator, serialize_response=serialize, deserialize_response=deserialize,
        )
        if execution == "native-batch":
            spec = replace(
                spec, item_id=lambda request: request,
                build_batch_context=batch_builder, call_provider_batch=batch_provider,
            )
        table = "deterministic_result_cache"
        if retained:
            deterministic_identity = runner._deterministic_cache_identity(spec, "request")
            assert deterministic_identity is not None
            DeterministicResultCache(db_path).put(
                deterministic_identity, '"retained"', serialize=lambda value: value,
            )

    before = []
    if retained:
        with sqlite3.connect(db_path) as connection:
            connection.execute(f"UPDATE {table} SET principal = ?", ("different-operator",))
            before = connection.execute(f"SELECT * FROM {table}").fetchall()

    with pytest.raises(StateError) as error:
        if execution == "single":
            runner.run(spec, "request")
        else:
            runner.run_batch(spec, ["request"])

    assert str(error.value) == f"{cache_kind.capitalize()}-result cache decoder returned an unexpected response type."
    assert error.value.__cause__ is None
    serialize.assert_called_once_with("frontier-value" if cache_kind == "released" else "deterministic-value")
    deserialize.assert_called_once_with(payload)
    assert validator.call_count == int(cache_kind == "released")
    record_release.assert_not_called()
    verify_release.assert_not_called()
    fallback.assert_not_called()
    decisions.assert_not_called()
    batch_builder.assert_not_called()
    batch_provider.assert_not_called()
    assert provider.call_count == int(cache_kind == "released")
    assert handler.call_count == int(cache_kind == "deterministic")
    assert ledger.reserve_frontier_call.call_count == int(cache_kind == "released")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"SELECT * FROM {table}").fetchall() == before


@pytest.mark.parametrize(
    ("cache_kind", "execution"),
    [
        ("released", "single"),
        ("released", "per-item-batch"),
        ("deterministic", "single"),
        ("deterministic", "per-item-batch"),
        ("deterministic", "native-batch"),
    ],
)
@pytest.mark.parametrize("fresh", [None, False, {"marker": "synthetic-private"}])
@pytest.mark.parametrize("retained", [False, True], ids=["no-row", "retained-row"])
def test_runner_rejects_wrong_fresh_cache_write_type_before_serialization(
    tmp_path: Path, cache_kind: str, execution: str, fresh: object, retained: bool,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    serialize = Mock(return_value='"compatible"')
    deserialize = Mock(return_value="compatible")
    provider = Mock(return_value=fresh)
    handler = Mock(return_value=TierResult(value=fresh, confidence=1.0))
    validator = Mock(return_value=ValidationResult(valid=True))
    record_release = Mock(return_value="origin-run")
    verify_release = Mock(return_value=True)
    ledger = Mock(spec=CallCeilingLedger)
    decisions = Mock()
    runner = StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=True,
        ceiling_ledger=ledger,
        released_cache=ReleasedResultCache(
            db_path, record_release=record_release, verify_release=verify_release,
        ),
        deterministic_cache=DeterministicResultCache(db_path),
        decision_sink=decisions,
    )
    if cache_kind == "released":
        spec = _cacheable_spec(
            tmp_path, call_provider=provider, semantic_validator=validator,
            serialize_response=serialize, deserialize_response=deserialize,
        )
        table = "released_result_cache"
        if retained:
            released_identity = runner._cache_identity(spec, "request", ContextBuildResult(compiled=_compiled()))
            assert released_identity is not None
            payload = '"retained"'
            ReleasedResultCache(db_path).put(
                released_identity, payload, serialize=lambda value: value,
                released_content_hash=hashlib.sha256(payload.encode("utf-8")).hexdigest(),
                release_run_id="old-run",
            )
    else:
        spec = _det_cacheable_spec(
            tmp_path, deterministic_handler=handler, semantic_validator=validator,
            serialize_response=serialize, deserialize_response=deserialize,
        )
        if execution == "native-batch":
            spec = replace(
                spec, item_id=lambda request: request,
                build_batch_context=Mock(), call_provider_batch=Mock(),
            )
        table = "deterministic_result_cache"
        if retained:
            deterministic_identity = runner._deterministic_cache_identity(spec, "request")
            assert deterministic_identity is not None
            DeterministicResultCache(db_path).put(deterministic_identity, '"retained"', serialize=lambda value: value)

    before = []
    if retained:
        with sqlite3.connect(db_path) as connection:
            connection.execute(f"UPDATE {table} SET principal = ?", ("different-operator",))
            before = connection.execute(f"SELECT * FROM {table}").fetchall()

    with pytest.raises(StateError) as error:
        if execution == "single":
            runner.run(spec, "request")
        else:
            runner.run_batch(spec, ["request"])

    assert str(error.value) == f"{cache_kind.capitalize()}-result cache received an unexpected response type."
    assert error.value.__cause__ is None
    serialize.assert_not_called()
    deserialize.assert_not_called()
    record_release.assert_not_called()
    verify_release.assert_not_called()
    if cache_kind == "released":
        decisions.assert_called_once()
        decision = decisions.call_args.args[0]
        assert decision.feature == spec.feature
        assert decision.tier == Tier.FRONTIER
        assert decision.outcome == RouteOutcome.FRONTIER_CALL
        assert decision.frontier_called is True
        assert decision.confidence == 0.0
        assert decision.trace_id == "run-1"
    else:
        decisions.assert_not_called()
    validator.assert_not_called()
    assert provider.call_count == int(cache_kind == "released")
    assert handler.call_count == int(cache_kind == "deterministic")
    assert ledger.reserve_frontier_call.call_count == int(cache_kind == "released")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"SELECT * FROM {table}").fetchall() == before


@pytest.mark.parametrize("cache_kind", ["released", "deterministic"])
def test_runner_cache_accepts_fresh_declared_response_type_subclasses(tmp_path: Path, cache_kind: str) -> None:
    class Label(str):
        pass

    value = Label("value")
    runner = _runner(tmp_path)
    if cache_kind == "released":
        spec = _cacheable_spec(
            tmp_path, call_provider=Mock(return_value=value),
            serialize_response=json.dumps, deserialize_response=json.loads,
        )
    else:
        spec = _det_cacheable_spec(
            tmp_path, deterministic_handler=Mock(return_value=TierResult(value=value, confidence=1.0)),
            serialize_response=json.dumps, deserialize_response=json.loads,
        )

    assert runner.run(spec, "request").value is value
    replay = runner.run(spec, "request")
    assert replay.tier == Tier.CACHE
    assert replay.value == value


@pytest.mark.parametrize("cache_kind", ["released", "deterministic"])
def test_runner_cache_accepts_declared_response_type_subclasses(tmp_path: Path, cache_kind: str) -> None:
    class Label(str):
        pass

    deserialize = Mock(side_effect=lambda payload: Label(json.loads(payload)))
    provider = Mock(return_value="value")
    handler = Mock(return_value=TierResult(value="value", confidence=1.0))
    runner = _runner(tmp_path)
    if cache_kind == "released":
        spec = _cacheable_spec(
            tmp_path, call_provider=provider, serialize_response=json.dumps, deserialize_response=deserialize,
        )
    else:
        spec = _det_cacheable_spec(
            tmp_path, deterministic_handler=handler, serialize_response=json.dumps, deserialize_response=deserialize,
        )

    assert runner.run(spec, "request").value == "value"
    replay = runner.run(spec, "request")
    assert replay.tier == Tier.CACHE
    assert type(replay.value) is Label
    assert replay.value == "value"
    assert deserialize.call_count == 2
    assert provider.call_count == int(cache_kind == "released")
    assert handler.call_count == int(cache_kind == "deterministic")


@pytest.mark.parametrize("execution", ["single", "per-item-batch", "native-batch"])
@pytest.mark.parametrize(
    ("source", "invalid"),
    [
        ("deterministic", None),
        ("deterministic", False),
        ("local", {"private": "synthetic"}),
        ("fallback-released", False),
        ("fallback-existing", False),
        ("fallback-partial", {"private": "synthetic"}),
    ],
)
def test_runner_rejects_unexpected_uncached_result_type(
    tmp_path: Path, execution: str, source: str, invalid: object,
) -> None:
    fallback_status = {
        "fallback-released": FeatureOutcomeStatus.RELEASED,
        "fallback-existing": FeatureOutcomeStatus.DETERMINISTIC_FALLBACK,
        "fallback-partial": FeatureOutcomeStatus.PARTIAL,
    }.get(source, FeatureOutcomeStatus.RELEASED)
    handler = Mock(return_value=TierResult(value=invalid, confidence=1.0))
    router = Mock()
    router.route.return_value = TierResult(value=invalid, confidence=1.0)
    fallback = Mock(return_value=FeatureOutcome(status=fallback_status, value=invalid, tier=Tier.DETERMINISTIC))
    provider = Mock(return_value="remote")
    validator = Mock(return_value=ValidationResult(valid=True))
    batch_builder, batch_provider = Mock(), Mock()
    ledger, decisions = Mock(spec=CallCeilingLedger), Mock()
    spec = _spec(
        tmp_path,
        deterministic_handler=handler if source == "deterministic" else None,
        local_router_name="test-router" if source == "local" else None,
        fallback_handler=fallback,
        semantic_validator=validator,
        call_provider=provider,
        build_context=Mock(return_value=ContextBuildResult(compiled=_compiled())),
    )
    if execution == "native-batch":
        spec = replace(
            spec, item_id=lambda request: request,
            build_batch_context=batch_builder, call_provider_batch=batch_provider,
        )
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=not source.startswith("fallback"),
        ceiling_ledger=ledger, local_routers={"test-router": router}, decision_sink=decisions,
        released_cache=ReleasedResultCache(tmp_path / "released.sqlite3"),
        deterministic_cache=DeterministicResultCache(tmp_path / "deterministic.sqlite3"),
    )

    with pytest.raises(StateError) as error:
        if execution == "single":
            runner.run(spec, "request")
        else:
            runner.run_batch(spec, ["request"])

    expected_source = (
        "Deterministic handler" if source == "deterministic"
        else "Local router" if source == "local" else "Fallback"
    )
    assert str(error.value) == f"{expected_source} returned an unexpected response type."
    assert error.value.__cause__ is None
    assert fallback.call_count == int(source.startswith("fallback"))
    assert decisions.call_count == int(source.startswith("fallback"))
    provider.assert_not_called()
    validator.assert_not_called()
    batch_builder.assert_not_called()
    batch_provider.assert_not_called()
    ledger.reserve_frontier_call.assert_not_called()


@pytest.mark.parametrize("execution", ["single", "per-item-batch"])
@pytest.mark.parametrize("invalid", [None, False, {"private": "synthetic"}])
def test_runner_rejects_unexpected_provider_type_before_semantic_validation(
    tmp_path: Path, execution: str, invalid: object,
) -> None:
    provider = Mock(return_value=invalid)
    validator = Mock(return_value=ValidationResult(valid=True))
    ledger, decisions = Mock(spec=CallCeilingLedger), Mock()
    spec = _spec(
        tmp_path, call_provider=provider, semantic_validator=validator,
        build_context=lambda _: ContextBuildResult(compiled=_compiled()),
    )
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger,
        decision_sink=decisions,
        released_cache=ReleasedResultCache(tmp_path / "released.sqlite3"),
        deterministic_cache=DeterministicResultCache(tmp_path / "deterministic.sqlite3"),
    )

    with pytest.raises(StateError, match="^Provider returned an unexpected response type\\.$"):
        if execution == "single":
            runner.run(spec, "request")
        else:
            runner.run_batch(spec, ["request"])

    provider.assert_called_once()
    ledger.reserve_frontier_call.assert_called_once()
    validator.assert_not_called()
    decisions.assert_called_once()
    decision = decisions.call_args.args[0]
    assert decision.feature == spec.feature
    assert decision.tier == Tier.FRONTIER
    assert decision.outcome == RouteOutcome.FRONTIER_CALL
    assert decision.frontier_called is True
    assert decision.confidence == 0.0
    assert decision.trace_id == "run-1"


@pytest.mark.parametrize("execution", ["single", "per-item-batch"])
@pytest.mark.parametrize("cache_enabled", [False, True])
def test_unexpected_provider_type_preserves_state_error_when_decision_sink_fails(
    tmp_path: Path, execution: str, cache_enabled: bool,
) -> None:
    provider = Mock(return_value=False)
    validator = Mock(return_value=ValidationResult(valid=True))
    decisions = Mock(side_effect=RuntimeError("synthetic private sink failure"))
    ledger = Mock(spec=CallCeilingLedger)
    if cache_enabled:
        spec = _cacheable_spec(tmp_path, call_provider=provider, semantic_validator=validator)
    else:
        spec = _spec(
            tmp_path, call_provider=provider, semantic_validator=validator,
            build_context=lambda _: ContextBuildResult(compiled=_compiled()),
        )
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger,
        decision_sink=decisions,
        released_cache=ReleasedResultCache(tmp_path / "released.sqlite3"),
        deterministic_cache=DeterministicResultCache(tmp_path / "deterministic.sqlite3"),
    )
    initial_failures = sink_failure_count()

    with pytest.raises(StateError) as error:
        if execution == "single":
            runner.run(spec, "request")
        else:
            runner.run_batch(spec, ["request"])

    expected_source = "Released-result cache received" if cache_enabled else "Provider returned"
    assert str(error.value) == f"{expected_source} an unexpected response type."
    assert error.value.__cause__ is None
    assert error.value.__context__ is None
    assert sink_failure_count() == initial_failures + 1
    decisions.assert_called_once()
    provider.assert_called_once()
    ledger.reserve_frontier_call.assert_called_once()
    validator.assert_not_called()


@pytest.mark.parametrize("source", ["deterministic", "local", "fallback", "provider", "native-batch"])
def test_runner_decision_sink_failure_is_counted_without_changing_outcome(
    tmp_path: Path, source: str,
) -> None:
    sink = Mock(side_effect=OSError("synthetic private sink failure"))
    router = Mock()
    router.route.return_value = TierResult(value="local", confidence=1.0)
    provider = Mock(return_value="frontier")
    spec = _spec(
        tmp_path,
        deterministic_handler=Mock(return_value=TierResult(value="deterministic", confidence=1.0))
        if source == "deterministic" else None,
        local_router_name="test-router" if source == "local" else None,
        fallback_handler=Mock(return_value=_fallback_released()),
        call_provider=provider,
        build_context=lambda _: ContextBuildResult(compiled=_compiled()),
    )
    if source == "native-batch":
        spec = replace(
            spec, item_id=lambda request: request,
            build_batch_context=lambda _: ContextBuildResult(compiled=_compiled()),
            call_provider_batch=Mock(return_value=BatchProviderResult(responses={"first": "one", "second": False})),
        )
    runner = _runner(
        tmp_path, frontier_eligible=source != "fallback",
        local_routers={"test-router": router}, decision_sink=sink,
    )
    initial_failures = sink_failure_count()

    if source == "native-batch":
        outcomes = runner.run_batch(spec, ["first", "second"])
        assert outcomes["first"].status == FeatureOutcomeStatus.RELEASED
        assert outcomes["first"].value == "one"
        assert outcomes["second"].status == FeatureOutcomeStatus.BLOCKED
        assert outcomes["second"].value is None
        assert sink.call_count == 2
    else:
        outcome = runner.run(spec, "request")
        expected = {
            "deterministic": "deterministic", "local": "local",
            "fallback": "fallback-value", "provider": "frontier",
        }[source]
        assert outcome.value == expected
        assert outcome.status == (
            FeatureOutcomeStatus.DETERMINISTIC_FALLBACK
            if source == "fallback" else FeatureOutcomeStatus.RELEASED
        )
        sink.assert_called_once()

    assert sink_failure_count() == initial_failures + sink.call_count


@pytest.mark.parametrize("inject", [False, True])
def test_runner_notifies_only_its_injected_decision_sink(tmp_path: Path, inject: bool) -> None:
    sink = Mock()
    runner = _runner(tmp_path, decision_sink=sink if inject else None)
    spec = _spec(
        tmp_path, deterministic_handler=lambda _: TierResult(value="deterministic", confidence=1.0),
    )
    register_decision_sink(sink)
    try:
        assert runner.run(spec, "request").value == "deterministic"
    finally:
        unregister_decision_sink(sink)

    assert sink.call_count == int(inject)


@pytest.mark.parametrize("after_write", [False, True])
@pytest.mark.parametrize("invalid", [False, True])
def test_runner_counts_durable_sink_failure_without_changing_provider_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, after_write: bool, invalid: bool,
) -> None:
    import src.core.measurement_store as measurement_store

    append = measurement_store.append_measurement

    def fail_write(path: Path, record: object) -> None:
        if after_write:
            append(path, record)
        raise OSError("synthetic write failure")

    monkeypatch.setattr(measurement_store, "append_measurement", fail_write)
    sink = measurement_store.make_tier_decision_sink(
        measurement_store.TierDecisionSinkConfig(
            program_id="acme", edition_id=None, run_id="run-1",
            execution_mode="enforce", programs_root=tmp_path,
        )
    )
    spec = _spec(
        tmp_path, call_provider=lambda _request, _context: False if invalid else "result",
        build_context=lambda _: ContextBuildResult(compiled=_compiled()),
    )
    runner = _runner(tmp_path, decision_sink=sink)
    initial_failures = sink_failure_count()

    if invalid:
        with pytest.raises(StateError) as error:
            runner.run(spec, "request")
        assert str(error.value) == "Provider returned an unexpected response type."
        assert error.value.__cause__ is None
        assert error.value.__context__ is None
    else:
        assert runner.run(spec, "request").value == "result"

    assert sink_failure_count() == initial_failures + 1
    rows = measurement_store.read_measurements(
        measurement_store.tier_decision_store_path("acme", programs_root=tmp_path),
    )
    assert len(rows) == int(after_write)
    if rows:
        assert rows[0]["outcome"] == "frontier_call"
        assert rows[0]["frontier_called"] is True


@pytest.mark.parametrize("invalid", [None, False, {"private": "synthetic"}])
def test_batch_blocks_unexpected_provider_types_per_item(tmp_path: Path, invalid: object) -> None:
    provider = Mock(return_value=BatchProviderResult(
        responses={"first": "first-value", "invalid": invalid, "failed": invalid, "last": "last-value"},
        failed_item_ids=("failed",),
    ))
    validator = Mock(return_value=ValidationResult(valid=True, evidence_refs=("synthetic-evidence",)))
    ledger, decisions = Mock(spec=CallCeilingLedger), Mock()
    spec = _batchable_spec(tmp_path, call_provider_batch=provider, semantic_validator=validator)
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger,
        decision_sink=decisions,
        released_cache=ReleasedResultCache(tmp_path / "released.sqlite3"),
        deterministic_cache=DeterministicResultCache(tmp_path / "deterministic.sqlite3"),
    )

    outcomes = runner.run_batch(spec, ["first", "invalid", "failed", "missing", "last"])

    assert outcomes["first"].status == outcomes["last"].status == FeatureOutcomeStatus.RELEASED
    assert outcomes["first"].value == "first-value"
    assert outcomes["last"].value == "last-value"
    assert outcomes["invalid"].status == FeatureOutcomeStatus.BLOCKED
    assert outcomes["invalid"].value is None
    assert outcomes["invalid"].limitations == ("Batched provider returned an unexpected response type.",)
    assert outcomes["invalid"].retryable is False
    assert outcomes["failed"].status == outcomes["missing"].status == FeatureOutcomeStatus.UNAVAILABLE
    assert outcomes["failed"].retryable and outcomes["missing"].retryable
    assert validator.call_count == 2
    validator.assert_any_call("first", "first-value")
    validator.assert_any_call("last", "last-value")
    assert decisions.call_count == 5
    provider.assert_called_once()
    ledger.reserve_frontier_call.assert_called_once()


@pytest.mark.parametrize("source", ["deterministic", "local", "fallback", "provider", "native-batch"])
def test_runner_accepts_uncached_response_type_subclasses(tmp_path: Path, source: str) -> None:
    class Label(str):
        pass

    value = Label("valid")
    router = Mock()
    router.route.return_value = TierResult(value=value, confidence=1.0)
    spec = _spec(
        tmp_path,
        deterministic_handler=Mock(return_value=TierResult(value=value, confidence=1.0))
        if source == "deterministic" else None,
        local_router_name="test-router" if source == "local" else None,
        fallback_handler=Mock(return_value=_fallback_released(value)),
        call_provider=Mock(return_value=value),
        build_context=lambda _: ContextBuildResult(compiled=_compiled()),
    )
    if source == "native-batch":
        spec = replace(
            spec, item_id=lambda request: request,
            build_batch_context=lambda _: ContextBuildResult(compiled=_compiled()),
            call_provider_batch=Mock(return_value=BatchProviderResult(responses={"request": value})),
        )
    runner = _runner(tmp_path, frontier_eligible=source != "fallback", local_routers={"test-router": router})

    outcome = runner.run_batch(spec, ["request"])["request"] if source == "native-batch" else runner.run(spec, "request")

    assert outcome.value is value
    assert outcome.status == (
        FeatureOutcomeStatus.DETERMINISTIC_FALLBACK if source == "fallback" else FeatureOutcomeStatus.RELEASED
    )


@pytest.mark.parametrize("status", [
    FeatureOutcomeStatus.RELEASED, FeatureOutcomeStatus.DETERMINISTIC_FALLBACK, FeatureOutcomeStatus.PARTIAL,
])
def test_runner_fallback_preserves_absent_value(tmp_path: Path, status: FeatureOutcomeStatus) -> None:
    fallback: FeatureOutcome[str] = FeatureOutcome(status=status, value=None, tier=Tier.NONE)
    outcome = _runner(tmp_path, frontier_eligible=False).run(
        _spec(tmp_path, fallback_handler=lambda _: fallback), "request",
    )
    assert outcome.value is None
    assert outcome.status == (
        FeatureOutcomeStatus.DETERMINISTIC_FALLBACK if status == FeatureOutcomeStatus.RELEASED else status
    )


@pytest.mark.parametrize(
    ("cache_kind", "execution"),
    [
        ("released", "single"),
        ("released", "per-item-batch"),
        ("deterministic", "single"),
        ("deterministic", "per-item-batch"),
        ("deterministic", "native-batch"),
    ],
)
@pytest.mark.parametrize("failure", ["json-null", "decoder-none", "decoder-error"])
@pytest.mark.parametrize("retained", [False, True], ids=["no-row", "retained-row"])
def test_runner_rejects_unreadable_cache_write_before_release_or_storage(
    tmp_path: Path, cache_kind: str, execution: str, failure: str, retained: bool,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    payload = "null" if failure == "json-null" else '"synthetic-private-marker"'
    decoder_error = ValueError("synthetic decoder error")
    deserialize = Mock(side_effect={
        "json-null": json.loads,
        "decoder-none": lambda _: None,
        "decoder-error": decoder_error,
    }[failure])
    serialize = Mock(return_value=payload)
    record_release = Mock(return_value="origin-run")
    verify_release = Mock(return_value=True)
    provider = Mock(return_value="value")
    handler = Mock(return_value=TierResult(value="value", confidence=1.0))
    fallback = Mock(return_value=_fallback_released())
    batch_builder = Mock()
    batch_provider = Mock()
    ledger = Mock(spec=CallCeilingLedger)
    decisions = Mock()
    runner = StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=True,
        ceiling_ledger=ledger,
        released_cache=ReleasedResultCache(
            db_path, record_release=record_release, verify_release=verify_release,
        ),
        deterministic_cache=DeterministicResultCache(db_path),
        decision_sink=decisions,
    )
    if cache_kind == "released":
        spec = _cacheable_spec(
            tmp_path, call_provider=provider, fallback_handler=fallback,
            serialize_response=serialize, deserialize_response=deserialize,
        )
        table = "released_result_cache"
        if retained:
            released_identity = runner._cache_identity(spec, "request", ContextBuildResult(compiled=_compiled()))
            assert released_identity is not None
            old_payload = '"retained"'
            ReleasedResultCache(db_path).put(
                released_identity, old_payload, serialize=lambda value: value,
                released_content_hash=hashlib.sha256(old_payload.encode("utf-8")).hexdigest(),
                release_run_id="old-run",
            )
    else:
        spec = _det_cacheable_spec(
            tmp_path, deterministic_handler=handler, fallback_handler=fallback,
            serialize_response=serialize, deserialize_response=deserialize,
        )
        if execution == "native-batch":
            spec = replace(
                spec, item_id=lambda request: request,
                build_batch_context=batch_builder, call_provider_batch=batch_provider,
            )
        table = "deterministic_result_cache"
        if retained:
            deterministic_identity = runner._deterministic_cache_identity(spec, "request")
            assert deterministic_identity is not None
            DeterministicResultCache(db_path).put(
                deterministic_identity, '"retained"', serialize=lambda value: value,
            )

    before = []
    if retained:
        with sqlite3.connect(db_path) as connection:
            connection.execute(f"UPDATE {table} SET principal = ?", ("different-operator",))
            before = connection.execute(f"SELECT * FROM {table}").fetchall()

    expected_error = ValueError if failure == "decoder-error" else StateError
    with pytest.raises(expected_error) as error:
        if execution == "single":
            runner.run(spec, "request")
        else:
            runner.run_batch(spec, ["request"])

    if failure == "decoder-error":
        assert error.value is decoder_error
    else:
        assert str(error.value) == f"{cache_kind.capitalize()}-result cache decoder returned no value."
        assert error.value.__cause__ is None
    serialize.assert_called_once_with("value")
    deserialize.assert_called_once_with(payload)
    record_release.assert_not_called()
    verify_release.assert_not_called()
    fallback.assert_not_called()
    decisions.assert_not_called()
    batch_builder.assert_not_called()
    batch_provider.assert_not_called()
    assert provider.call_count == int(cache_kind == "released")
    assert handler.call_count == int(cache_kind == "deterministic")
    assert ledger.reserve_frontier_call.call_count == int(cache_kind == "released")
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"SELECT * FROM {table}").fetchall() == before


@pytest.mark.parametrize("cache_kind", ["released", "deterministic"])
def test_runner_checks_serialized_cache_payload_once_before_write(
    tmp_path: Path, cache_kind: str,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    events: list[tuple[str, str]] = []

    def serialize(value: str) -> str:
        events.append(("serialize", value))
        return json.dumps(value)

    def deserialize(payload: str) -> str:
        events.append(("deserialize", payload))
        return json.loads(payload)

    def record_release(_identity: CacheIdentity, payload: str) -> str:
        events.append(("record", payload))
        return "origin-run"

    verify_release = Mock(return_value=True)
    provider = Mock(return_value="value")
    handler = Mock(return_value=TierResult(value="value", confidence=1.0))
    runner = StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=True,
        ceiling_ledger=Mock(spec=CallCeilingLedger),
        released_cache=ReleasedResultCache(
            db_path, record_release=record_release, verify_release=verify_release,
        ),
        deterministic_cache=DeterministicResultCache(db_path),
    )
    if cache_kind == "released":
        spec = _cacheable_spec(
            tmp_path, call_provider=provider,
            serialize_response=serialize, deserialize_response=deserialize,
        )
    else:
        spec = _det_cacheable_spec(
            tmp_path, deterministic_handler=handler,
            serialize_response=serialize, deserialize_response=deserialize,
        )

    assert runner.run(spec, "request").value == "value"
    assert runner.run(spec, "request").tier == Tier.CACHE
    expected = [("serialize", "value"), ("deserialize", '"value"')]
    if cache_kind == "released":
        expected.append(("record", '"value"'))
    expected.append(("deserialize", '"value"'))
    assert events == expected
    assert provider.call_count == int(cache_kind == "released")
    assert handler.call_count == int(cache_kind == "deterministic")
    assert verify_release.call_count == int(cache_kind == "released")


@pytest.mark.parametrize("failure_stage", ["serialize", "deserialize"])
def test_deterministic_runner_propagates_sqlite_errors_from_payload_callbacks(
    tmp_path: Path, failure_stage: str,
) -> None:
    failure = sqlite3.DatabaseError("synthetic callback failure")
    serialize = Mock(side_effect=failure if failure_stage == "serialize" else None, return_value='"value"')
    deserialize = Mock(side_effect=failure if failure_stage == "deserialize" else None, return_value="value")
    handler = Mock(return_value=TierResult(value="value", confidence=1.0))
    cache = DeterministicResultCache(tmp_path / "cache.sqlite3")
    runner = _runner(tmp_path, deterministic_cache=cache)
    spec = _det_cacheable_spec(
        tmp_path, deterministic_handler=handler,
        serialize_response=serialize, deserialize_response=deserialize,
    )

    with pytest.raises(sqlite3.DatabaseError) as error:
        runner.run(spec, "request")
    assert error.value is failure
    serialize.assert_called_once_with("value")
    assert deserialize.call_count == int(failure_stage == "deserialize")

    if failure_stage == "serialize":
        identity = runner._deterministic_cache_identity(spec, "request")
        assert identity is not None
        with pytest.raises(StateError) as direct_error:
            cache.put(identity, "value", serialize=serialize)
        assert direct_error.value.__cause__ is failure


def test_deterministic_runner_checks_payload_before_put_database_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = importlib.import_module("src.ai.structured_feature_runner")
    open_database = module.open_program_db
    failure = sqlite3.OperationalError("synthetic put-open failure")
    opens = 0

    def fail_second_open(*args, **kwargs):
        nonlocal opens
        opens += 1
        if opens == 2:
            raise failure
        return open_database(*args, **kwargs)

    monkeypatch.setattr(module, "open_program_db", fail_second_open)
    serialize = Mock(return_value='"value"')
    deserialize = Mock(return_value="value")
    spec = _det_cacheable_spec(
        tmp_path, deterministic_handler=lambda _: TierResult(value="value", confidence=1.0),
        serialize_response=serialize, deserialize_response=deserialize,
    )

    with pytest.raises(StateError) as error:
        _runner(tmp_path).run(spec, "request")
    assert error.value.__cause__ is failure
    serialize.assert_called_once_with("value")
    deserialize.assert_called_once_with('"value"')


@pytest.mark.parametrize(
    ("cache_kind", "execution"),
    [("released", "per-item-batch"), ("deterministic", "per-item-batch"), ("deterministic", "native-batch")],
)
@pytest.mark.parametrize(("payload", "reason"), [("null", "no value"), ("false", "an unexpected response type")])
def test_runner_cache_write_refusal_preserves_earlier_batch_item(
    tmp_path: Path, cache_kind: str, execution: str, payload: str, reason: str,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    serialize = Mock(side_effect=lambda value: json.dumps(value) if value == "value-first" else payload)
    provider = Mock(side_effect=lambda request, _compiled: f"value-{request}")
    handler = Mock(side_effect=lambda request: TierResult(value=f"value-{request}", confidence=1.0))
    record_release = Mock(return_value="origin-run")
    ledger = Mock(spec=CallCeilingLedger)
    batch_builder = Mock()
    batch_provider = Mock()
    runner = StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=True,
        ceiling_ledger=ledger,
        released_cache=ReleasedResultCache(
            db_path, record_release=record_release, verify_release=lambda *_: True,
        ),
        deterministic_cache=DeterministicResultCache(db_path),
    )
    if cache_kind == "released":
        spec = _cacheable_spec(
            tmp_path, call_provider=provider,
            serialize_response=serialize, deserialize_response=json.loads,
        )
        table = "released_result_cache"
    else:
        spec = _det_cacheable_spec(
            tmp_path, deterministic_handler=handler,
            serialize_response=serialize, deserialize_response=json.loads,
        )
        if execution == "native-batch":
            spec = replace(
                spec, item_id=lambda request: request,
                build_batch_context=batch_builder, call_provider_batch=batch_provider,
            )
        table = "deterministic_result_cache"

    with pytest.raises(StateError, match=f"decoder returned {reason}"):
        runner.run_batch(spec, ["first", "later"])

    assert serialize.call_count == 2
    assert provider.call_count == 2 * int(cache_kind == "released")
    assert handler.call_count == 2 * int(cache_kind == "deterministic")
    assert ledger.reserve_frontier_call.call_count == 2 * int(cache_kind == "released")
    assert record_release.call_count == int(cache_kind == "released")
    batch_builder.assert_not_called()
    batch_provider.assert_not_called()
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(f"SELECT payload FROM {table}").fetchall() == [('"value-first"',)]


@pytest.mark.parametrize(("payload", "reason"), [("null", "no value"), ("false", "an unexpected response type")])
def test_released_cache_refusal_does_not_undo_prior_handler_execution(
    tmp_path: Path, payload: str, reason: str,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    handler = Mock(return_value=None)
    provider = Mock(return_value="frontier-value")
    ledger = Mock(spec=CallCeilingLedger)
    spec = _cacheable_spec(
        tmp_path, call_provider=provider, deterministic_handler=handler,
        serialize_response=json.dumps, deserialize_response=json.loads,
    )
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger,
        released_cache=ReleasedResultCache(db_path),
        deterministic_cache=DeterministicResultCache(db_path),
    )
    runner.run(spec, "request")
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "UPDATE released_result_cache SET payload = ?, released_content_hash = ?",
            (payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()),
        )

    with pytest.raises(StateError, match=f"Released-result cache decoder returned {reason}"):
        runner.run(spec, "request")

    assert handler.call_count == 2
    provider.assert_called_once()
    ledger.reserve_frontier_call.assert_called_once()


@pytest.mark.parametrize(
    ("cache_kind", "execution"),
    [
        ("released", "single"),
        ("released", "per-item-batch"),
        ("deterministic", "single"),
        ("deterministic", "per-item-batch"),
        ("deterministic", "native-batch"),
    ],
)
@pytest.mark.parametrize("value", [False, 0, "", [], {}], ids=["false", "zero", "empty-string", "empty-list", "empty-object"])
def test_runner_reuses_non_none_falsey_cache_values(
    tmp_path: Path, cache_kind: str, execution: str, value: object,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    ledger = Mock(spec=CallCeilingLedger)
    provider = Mock(return_value=value)
    handler = Mock(return_value=TierResult(value=value, confidence=1.0))
    batch_builder = Mock()
    batch_provider = Mock()
    runner = StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=True,
        ceiling_ledger=ledger,
        released_cache=ReleasedResultCache(db_path),
        deterministic_cache=DeterministicResultCache(db_path),
    )
    if cache_kind == "released":
        spec = _cacheable_spec(
            tmp_path, call_provider=provider, response_type=type(value),
            serialize_response=json.dumps, deserialize_response=json.loads,
        )
    else:
        spec = _det_cacheable_spec(
            tmp_path, deterministic_handler=handler, response_type=type(value),
            serialize_response=json.dumps, deserialize_response=json.loads,
        )
        if execution == "native-batch":
            spec = replace(
                spec, item_id=lambda request: request,
                build_batch_context=batch_builder, call_provider_batch=batch_provider,
            )

    first = runner.run(spec, "request")
    if execution == "single":
        second = runner.run(spec, "request")
    else:
        second = next(iter(runner.run_batch(spec, ["request"]).values()))

    assert first.value == value
    assert type(second.value) is type(value)
    assert second.value == value
    assert second.tier == Tier.CACHE
    assert provider.call_count == int(cache_kind == "released")
    assert handler.call_count == int(cache_kind == "deterministic")
    assert ledger.reserve_frontier_call.call_count == int(cache_kind == "released")
    batch_builder.assert_not_called()
    batch_provider.assert_not_called()


@pytest.mark.parametrize("execution", ["per-item-batch", "native-batch"])
@pytest.mark.parametrize(("payload", "reason"), [("null", "no value"), ("false", "an unexpected response type")])
def test_batch_preserves_earlier_deterministic_actions_when_later_cache_refused(
    tmp_path: Path, execution: str, payload: str, reason: str,
) -> None:
    db_path = tmp_path / "cache.sqlite3"
    cache = DeterministicResultCache(db_path)
    later_identity = DeterministicCacheIdentity(
        program_id="acme", feature="test_feature", request_hash="hash-later", snapshot_id="snap-1",
        deterministic_engine_version="regex-v1", principal="operator", classification="internal",
    )
    cache.put(later_identity, payload, serialize=lambda value: value)
    with sqlite3.connect(db_path) as connection:
        later_before = connection.execute(
            "SELECT * FROM deterministic_result_cache WHERE cache_key = ?", (later_identity.cache_key(),),
        ).fetchone()

    handler = Mock(side_effect=lambda request: TierResult(value=f"value-{request}", confidence=1.0))
    batch_builder = Mock()
    batch_provider = Mock()
    ledger = Mock(spec=CallCeilingLedger)
    spec = _det_cacheable_spec(
        tmp_path, deterministic_handler=handler,
        serialize_response=json.dumps, deserialize_response=json.loads,
    )
    if execution == "native-batch":
        spec = replace(
            spec, item_id=lambda request: request,
            build_batch_context=batch_builder, call_provider_batch=batch_provider,
        )
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger,
        released_cache=ReleasedResultCache(db_path), deterministic_cache=cache,
    )

    with pytest.raises(StateError, match=f"Deterministic-result cache decoder returned {reason}"):
        runner.run_batch(spec, ["first", "later"])

    handler.assert_called_once_with("first")
    batch_builder.assert_not_called()
    batch_provider.assert_not_called()
    ledger.reserve_frontier_call.assert_not_called()
    earlier_identity = replace(later_identity, request_hash="hash-first")
    assert cache.get(earlier_identity, deserialize=json.loads) == "value-first"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT * FROM deterministic_result_cache WHERE cache_key = ?", (later_identity.cache_key(),),
        ).fetchone() == later_before


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


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("failure_call", [1, 3], ids=["first-span", "rendered-prompt"])
def test_resolved_tokenizer_failure_blocks_compiler_and_frontier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: bool, failure_call: int,
) -> None:
    encode = Mock(side_effect=[[1]] * (failure_call - 1) + [ValueError("private encoder detail")])
    encoding = SimpleNamespace(encode_ordinary=encode)
    module = SimpleNamespace(
        encoding_name_for_model=lambda _: "synthetic_encoding",
        get_encoding=lambda _: encoding,
    )
    monkeypatch.setattr(context_tokenizers, "_try_import_tiktoken", lambda: module)
    compiler = DeterministicContextCompiler(
        token_estimator=TiktokenTokenEstimator("synthetic-model"),
        programs_root=tmp_path / "programs",
    )
    required = EvidenceSpan(
        evidence_id="required", source_family="ado", text="private evidence", required=True,
        origin=ContentOrigin.SYSTEM, trust_level="high", verification_state="verified", injection_screen="pass",
    )
    optional = replace(required, evidence_id="optional", required=False)
    request = ContextCompileRequest(
        program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
        system_instructions="private instructions", output_schema_text="schema", required_evidence=(required,),
        optional_evidence=(optional,), max_input_tokens=1000, reserved_output_tokens=10,
    )

    def build_context(_request: object) -> ContextBuildResult:
        return ContextBuildResult(compiled=compiler.compile(
            request, validation_context=ValidationContext(
                program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
            ),
        ))

    provider = Mock(return_value=BatchProviderResult(responses={}) if batch else "unexpected")
    fallback = Mock()
    runner = _runner(tmp_path)
    reserve = Mock()
    cache_get = Mock()
    monkeypatch.setattr(runner._ceiling_ledger, "reserve_frontier_call", reserve)
    monkeypatch.setattr(runner._released_cache, "get", cache_get)
    if batch:
        spec = _batchable_spec(
            tmp_path, call_provider_batch=provider, build_batch_context=build_context, fallback_handler=fallback,
        )
        outcomes = list(runner.run_batch(spec, ["item-a", "item-b"]).values())
        assert len(outcomes) == 2
    else:
        spec = _cacheable_spec(
            tmp_path, call_provider=provider, build_context=build_context, fallback_handler=fallback,
        )
        outcomes = [runner.run(spec, "item-a")]

    for outcome in outcomes:
        assert outcome.status == FeatureOutcomeStatus.EVIDENCE_GAP
        assert outcome.tier == Tier.NONE
        assert outcome.value is None
        assert outcome.retryable is False
        assert outcome.limitations == ("Tokenizer failed to estimate input.",)
        assert "private" not in repr(outcome)
    assert encode.call_count == failure_call
    assert not (tmp_path / "programs").exists()
    provider.assert_not_called()
    fallback.assert_not_called()
    reserve.assert_not_called()
    cache_get.assert_not_called()


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("failure", ["import", "asset", "encode"])
def test_default_tokenizer_failure_blocks_compiler_and_frontier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: bool, failure: str,
) -> None:
    detail = "synthetic-private tokenizer error"

    def encode(_text: str) -> list[int]:
        if failure == "encode":
            raise ValueError(detail)
        return [1]

    def load(name: str) -> SimpleNamespace:
        if failure == "asset" and name == "synthetic-known":
            raise OSError(detail)
        return SimpleNamespace(encode_ordinary=encode)

    def legacy_resolve(_model: str) -> SimpleNamespace:
        if failure == "asset":
            raise OSError(detail)
        return SimpleNamespace(encode_ordinary=encode)

    module = SimpleNamespace(
        encoding_name_for_model=lambda _model: "synthetic-known",
        encoding_for_model=legacy_resolve,
        get_encoding=load,
    )
    original_import = importlib.import_module

    def import_module(name: str, package: str | None = None) -> object:
        if name == "tiktoken":
            if failure == "import":
                raise ImportError(detail)
            return module
        return original_import(name, package)

    monkeypatch.setattr("src.core.context_budget.importlib.import_module", import_module)
    span = EvidenceSpan(
        evidence_id="required", source_family="ado", text="synthetic-private evidence", required=True,
        origin=ContentOrigin.SYSTEM, trust_level="high", verification_state="verified", injection_screen="pass",
    )
    request = ContextCompileRequest(
        program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
        system_instructions="synthetic-private instructions", output_schema_text="schema",
        required_evidence=(span,), optional_evidence=(), max_input_tokens=1000, reserved_output_tokens=10,
    )

    def build_context(_request: object) -> ContextBuildResult:
        return ContextBuildResult(compiled=DeterministicContextCompiler(
            programs_root=tmp_path / "compiler-programs",
        ).compile(
            request, validation_context=ValidationContext(
                program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
            ),
        ))

    provider = Mock(return_value=BatchProviderResult(responses={}) if batch else "unexpected")
    fallback = Mock()
    runner = _runner(tmp_path)
    reserve = Mock()
    cache_get = Mock()
    monkeypatch.setattr(runner._ceiling_ledger, "reserve_frontier_call", reserve)
    monkeypatch.setattr(runner._released_cache, "get", cache_get)
    if batch:
        spec = _batchable_spec(
            tmp_path, call_provider_batch=provider, build_batch_context=build_context, fallback_handler=fallback,
        )
        outcomes = list(runner.run_batch(spec, ["item-a", "item-b"]).values())
        assert len(outcomes) == 2
    else:
        spec = _cacheable_spec(
            tmp_path, call_provider=provider, build_context=build_context, fallback_handler=fallback,
        )
        outcomes = [runner.run(spec, "item-a")]

    for outcome in outcomes:
        assert outcome.status == FeatureOutcomeStatus.EVIDENCE_GAP
        assert outcome.tier == Tier.NONE
        assert outcome.value is None
        assert outcome.retryable is False
        assert outcome.limitations == ("Tokenizer failed to estimate input.",)
        assert "synthetic-private" not in repr(outcome)
    provider.assert_not_called()
    fallback.assert_not_called()
    reserve.assert_not_called()
    cache_get.assert_not_called()
    assert not (tmp_path / "compiler-programs").exists()


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("collection", ["required", "optional"])
def test_context_requiredness_rejection_blocks_runner_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: bool, collection: str,
) -> None:
    span = EvidenceSpan(
        evidence_id="synthetic-private-id", source_family="ado", text="synthetic-private-evidence",
        required=collection == "optional", origin=ContentOrigin.SYSTEM, trust_level="high",
        verification_state="verified", injection_screen="pass",
    )
    request = ContextCompileRequest(
        program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
        system_instructions="sys", output_schema_text="schema",
        required_evidence=(span,) if collection == "required" else (),
        optional_evidence=(span,) if collection == "optional" else (),
        max_input_tokens=1000, reserved_output_tokens=0,
    )

    def build_context(_request: object) -> ContextBuildResult:
        return ContextBuildResult(compiled=DeterministicContextCompiler(
            token_estimator=SimpleNamespace(tokenizer_id="fixed", estimate=len),
            programs_root=tmp_path / "compiler-programs",
        ).compile(
            request, validation_context=ValidationContext(
                program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
            ),
        ))

    provider = Mock(
        return_value=BatchProviderResult(responses={"item-a": "unexpected", "item-b": "unexpected"})
        if batch else "unexpected",
    )
    fallback = Mock()
    runner = _runner(tmp_path)
    reserve = Mock()
    cache_get = Mock()
    monkeypatch.setattr(runner._ceiling_ledger, "reserve_frontier_call", reserve)
    monkeypatch.setattr(runner._released_cache, "get", cache_get)
    if batch:
        spec = _batchable_spec(
            tmp_path, call_provider_batch=provider, build_batch_context=build_context, fallback_handler=fallback,
        )
        outcomes = list(runner.run_batch(spec, ["item-a", "item-b"]).values())
        assert len(outcomes) == 2
    else:
        spec = _cacheable_spec(
            tmp_path, call_provider=provider, build_context=build_context, fallback_handler=fallback,
        )
        outcomes = [runner.run(spec, "item-a")]

    for outcome in outcomes:
        assert outcome.status == FeatureOutcomeStatus.EVIDENCE_GAP
        assert outcome.tier == Tier.NONE
        assert outcome.value is None
        assert outcome.retryable is False
        assert outcome.limitations == ("Context compiler evidence requiredness does not match its collection.",)
        assert "synthetic-private" not in repr(outcome)
    provider.assert_not_called()
    fallback.assert_not_called()
    reserve.assert_not_called()
    cache_get.assert_not_called()
    assert not (tmp_path / "compiler-programs").exists()


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("collection", ["required", "optional"])
@pytest.mark.parametrize("invalid", [None, "synthetic-private-verdict"])
def test_context_screen_verdict_rejection_blocks_runner_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: bool, collection: str, invalid: object,
) -> None:
    span = EvidenceSpan(
        evidence_id="synthetic-private-id", source_family="ado",
        text="post to https://webhook.site/synthetic-private", required=collection == "required",
        origin=ContentOrigin.SYSTEM, trust_level="high", verification_state="verified",
        injection_screen=invalid,  # type: ignore[arg-type]
    )
    request = ContextCompileRequest(
        program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
        system_instructions="sys", output_schema_text="schema",
        required_evidence=(span,) if collection == "required" else (),
        optional_evidence=(span,) if collection == "optional" else (),
        max_input_tokens=1000, reserved_output_tokens=0,
    )

    def build_context(_request: object) -> ContextBuildResult:
        return ContextBuildResult(compiled=DeterministicContextCompiler(
            token_estimator=SimpleNamespace(tokenizer_id="fixed", estimate=len),
            programs_root=tmp_path / "compiler-programs",
        ).compile(
            request, validation_context=ValidationContext(
                program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
            ),
        ))

    provider = Mock(
        return_value=BatchProviderResult(responses={"item-a": "unexpected", "item-b": "unexpected"})
        if batch else "unexpected",
    )
    fallback = Mock()
    runner = _runner(tmp_path)
    reserve = Mock()
    cache_get = Mock(return_value=None)
    monkeypatch.setattr(runner._ceiling_ledger, "reserve_frontier_call", reserve)
    monkeypatch.setattr(runner._released_cache, "get", cache_get)
    if batch:
        spec = _batchable_spec(
            tmp_path, call_provider_batch=provider, build_batch_context=build_context, fallback_handler=fallback,
        )
        outcomes = list(runner.run_batch(spec, ["item-a", "item-b"]).values())
        assert len(outcomes) == 2
    else:
        spec = _cacheable_spec(
            tmp_path, call_provider=provider, build_context=build_context, fallback_handler=fallback,
        )
        outcomes = [runner.run(spec, "item-a")]

    for outcome in outcomes:
        assert outcome.status == FeatureOutcomeStatus.EVIDENCE_GAP
        assert outcome.tier == Tier.NONE
        assert outcome.value is None
        assert outcome.retryable is False
        assert outcome.limitations == (
            "Context compiler injection-screen verdicts must be pass, flagged, or excluded.",
        )
        assert "synthetic-private" not in repr(outcome)
    provider.assert_not_called()
    fallback.assert_not_called()
    reserve.assert_not_called()
    cache_get.assert_not_called()
    assert not (tmp_path / "compiler-programs").exists()


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("invalid", [float("nan"), "synthetic-private-value"], ids=["nan", "text"])
@pytest.mark.parametrize("source", ["inputs", "weights"])
def test_context_salience_rejection_blocks_runner_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: bool, invalid: object, source: str,
) -> None:
    span = EvidenceSpan(
        evidence_id="synthetic-private-id", source_family="ado", text="synthetic-private-evidence",
        required=False, origin=ContentOrigin.SYSTEM, trust_level="high", verification_state="verified",
        injection_screen="pass",
        salience_inputs={"source_authority": invalid if source == "inputs" else 1.0},  # type: ignore[dict-item]
    )
    request = ContextCompileRequest(
        program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
        system_instructions="sys", output_schema_text="schema", required_evidence=(),
        optional_evidence=(span,), max_input_tokens=1000, reserved_output_tokens=0,
    )

    def build_context(_request: object) -> ContextBuildResult:
        return ContextBuildResult(compiled=DeterministicContextCompiler(
            token_estimator=SimpleNamespace(tokenizer_id="fixed", estimate=len),
            salience_weights={"source_authority": invalid} if source == "weights" else None,  # type: ignore[dict-item]
            programs_root=tmp_path / "compiler-programs",
        ).compile(
            request, validation_context=ValidationContext(
                program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
            ),
        ))

    provider = Mock(
        return_value=BatchProviderResult(responses={"item-a": "unexpected", "item-b": "unexpected"})
        if batch else "unexpected",
    )
    fallback = Mock()
    runner = _runner(tmp_path)
    reserve = Mock()
    cache_get = Mock()
    monkeypatch.setattr(runner._ceiling_ledger, "reserve_frontier_call", reserve)
    monkeypatch.setattr(runner._released_cache, "get", cache_get)
    if batch:
        spec = _batchable_spec(
            tmp_path, call_provider_batch=provider, build_batch_context=build_context, fallback_handler=fallback,
        )
        outcomes = list(runner.run_batch(spec, ["item-a", "item-b"]).values())
        assert len(outcomes) == 2
    else:
        spec = _cacheable_spec(
            tmp_path, call_provider=provider, build_context=build_context, fallback_handler=fallback,
        )
        outcomes = [runner.run(spec, "item-a")]

    for outcome in outcomes:
        assert outcome.status == FeatureOutcomeStatus.EVIDENCE_GAP
        assert outcome.tier == Tier.NONE
        assert outcome.value is None
        assert outcome.retryable is False
        assert outcome.limitations == ((
            "Context compiler consumed salience inputs must be numbers between 0 and 1."
            if source == "inputs" else "Context compiler salience score must be a finite number."
        ),)
        assert "synthetic-private" not in repr(outcome)
    provider.assert_not_called()
    fallback.assert_not_called()
    reserve.assert_not_called()
    cache_get.assert_not_called()
    assert not (tmp_path / "compiler-programs").exists()


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize("quota", [-1, True], ids=["negative", "boolean"])
def test_context_quota_rejection_blocks_runner_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: bool, quota: int,
) -> None:
    request = ContextCompileRequest(
        program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
        system_instructions="sys", output_schema_text="schema",
        required_evidence=(),
        optional_evidence=(
            EvidenceSpan(
                evidence_id="synthetic-private-id", source_family="ado", text="synthetic-private-evidence",
                required=False, origin=ContentOrigin.SYSTEM, trust_level="high",
                verification_state="verified", injection_screen="pass",
            ),
        ),
        max_input_tokens=1000, reserved_output_tokens=0, per_source_quotas={"ado": quota},
    )

    def build_context(_request: object) -> ContextBuildResult:
        return ContextBuildResult(compiled=DeterministicContextCompiler(
            token_estimator=SimpleNamespace(tokenizer_id="fixed", estimate=len),
            programs_root=tmp_path / "compiler-programs",
        ).compile(
            request, validation_context=ValidationContext(
                program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
            ),
        ))

    provider = Mock(
        return_value=BatchProviderResult(responses={"item-a": "unexpected", "item-b": "unexpected"})
        if batch else "unexpected",
    )
    fallback = Mock()
    runner = _runner(tmp_path)
    reserve = Mock()
    cache_get = Mock()
    monkeypatch.setattr(runner._ceiling_ledger, "reserve_frontier_call", reserve)
    monkeypatch.setattr(runner._released_cache, "get", cache_get)
    if batch:
        spec = _batchable_spec(
            tmp_path, call_provider_batch=provider, build_batch_context=build_context, fallback_handler=fallback,
        )
        outcomes = list(runner.run_batch(spec, ["item-a", "item-b"]).values())
        assert len(outcomes) == 2
    else:
        spec = _cacheable_spec(
            tmp_path, call_provider=provider, build_context=build_context, fallback_handler=fallback,
        )
        outcomes = [runner.run(spec, "item-a")]

    for outcome in outcomes:
        assert outcome.status == FeatureOutcomeStatus.EVIDENCE_GAP
        assert outcome.tier == Tier.NONE
        assert outcome.value is None
        assert outcome.retryable is False
        assert outcome.limitations == (
            "Context compiler source quotas must map source-family strings to non-negative integers.",
        )
        assert "synthetic-private" not in repr(outcome)
    provider.assert_not_called()
    fallback.assert_not_called()
    reserve.assert_not_called()
    cache_get.assert_not_called()
    assert not (tmp_path / "compiler-programs").exists()


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
@pytest.mark.parametrize(
    "rejection",
    ["hard-injection", "overflow", "optional-overflow", "reference-mismatch", "invalid-budget", "invalid-estimate"],
)
def test_context_rejection_guidance_preserves_required_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, batch: bool, rejection: str
) -> None:
    expected_detail = {
        "hard-injection": "hard prompt-injection verdict",
        "overflow": "reserved tokens",
        "optional-overflow": "rendered prompt",
        "reference-mismatch": "evidence reference/text mismatch",
        "invalid-budget": "token budgets must be non-negative integers",
        "invalid-estimate": "token estimates must be non-negative integers",
    }[rejection]

    def build_context(_request: object) -> ContextBuildResult:
        if rejection == "reference-mismatch":
            raise ContextCompileRejected(expected_detail)
        span = EvidenceSpan(
            evidence_id="required", source_family="ado", text="required fact", required=True,
            origin=ContentOrigin.SYSTEM, trust_level="high", verification_state="verified",
            injection_screen="excluded" if rejection == "hard-injection" else "pass",
        )
        optional: tuple[EvidenceSpan, ...] = ()
        max_input_tokens = 1
        if rejection == "optional-overflow":
            optional = (
                EvidenceSpan(
                    evidence_id="first", source_family="ado", text="first optional fact", required=False,
                    origin=ContentOrigin.SYSTEM, trust_level="high", verification_state="verified",
                    injection_screen="pass", salience_inputs={"source_authority": 1.0},
                ),
                EvidenceSpan(
                    evidence_id="middle", source_family="ado", text="middle optional fact", required=False,
                    origin=ContentOrigin.SYSTEM, trust_level="high", verification_state="verified",
                    injection_screen="pass", salience_inputs={"source_authority": 0.75},
                ),
                EvidenceSpan(
                    evidence_id="opt", source_family="ado", text="optional fact", required=False,
                    origin=ContentOrigin.SYSTEM, trust_level="high", verification_state="verified",
                    injection_screen="pass", salience_inputs={"source_authority": 0.5},
                ),
            )
            expected_prompt = (
                "sys\n\nschema\n\nEvidence:\n\n"
                "[REQUIRED required origin=system trust=high verification=verified]\nrequired fact\n\n"
                "[EVIDENCE first origin=system trust=high verification=verified]\nfirst optional fact\n\n"
                "[EVIDENCE middle origin=system trust=high verification=verified]\nmiddle optional fact\n\n"
                "[EVIDENCE opt origin=system trust=high verification=verified]\noptional fact"
            )
            max_input_tokens = len(expected_prompt)
        request = ContextCompileRequest(
            program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
            system_instructions="sys", output_schema_text="schema", required_evidence=(span,),
            optional_evidence=optional, max_input_tokens=max_input_tokens,
            reserved_output_tokens=-1000 if rejection == "invalid-budget" else 0,
        )
        compiler = DeterministicContextCompiler(
            token_estimator=SimpleNamespace(
                tokenizer_id="boundary-overhead",
                estimate=lambda text: (
                    -1 if rejection == "invalid-estimate"
                    else len(text) + (5 if "\n\n[EVIDENCE opt " in text else 0)
                ),
                incremental_estimates=True,
            ),
            programs_root=tmp_path / "programs",
        )
        compiled = compiler.compile(
            request,
            validation_context=ValidationContext(
                program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
            ),
        )
        return ContextBuildResult(compiled=compiled)

    provider = Mock(
        return_value=BatchProviderResult(responses={"item-a": "unexpected", "item-b": "unexpected"})
        if batch else "unexpected"
    )
    fallback = Mock()
    runner = _runner(tmp_path)
    reserve = Mock()
    monkeypatch.setattr(runner._ceiling_ledger, "reserve_frontier_call", reserve)
    if batch:
        spec = _batchable_spec(
            tmp_path, call_provider_batch=provider, build_batch_context=build_context, fallback_handler=fallback,
        )
        outcomes = list(runner.run_batch(spec, ["item-a", "item-b"]).values())
        assert len(outcomes) == 2
    else:
        spec = _spec(tmp_path, call_provider=provider, build_context=build_context, fallback_handler=fallback)
        outcomes = [runner.run(spec, "item-a")]

    for outcome in outcomes:
        assert outcome.status == FeatureOutcomeStatus.EVIDENCE_GAP
        assert outcome.tier == Tier.NONE
        assert outcome.value is None
        assert outcome.retryable is False
        assert expected_detail in outcome.limitations[0]
        assert outcome.next_action == (
            "Review the rejection details and resolve the issue without dropping required evidence "
            "or bypassing safety checks."
        )
    provider.assert_not_called()
    fallback.assert_not_called()
    reserve.assert_not_called()
    if rejection in {"invalid-budget", "invalid-estimate"}:
        assert not (tmp_path / "programs").exists()


@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_second_optional_overflow_dispatches_bounded_context(tmp_path: Path, batch: bool) -> None:
    required = EvidenceSpan(
        evidence_id="required", source_family="ado", text="required fact", required=True,
        origin=ContentOrigin.SYSTEM, trust_level="high", verification_state="verified", injection_screen="pass",
    )
    first = EvidenceSpan(
        evidence_id="first", source_family="ado", text="first optional fact", required=False,
        origin=ContentOrigin.SYSTEM, trust_level="high", verification_state="verified",
        injection_screen="pass", salience_inputs={"source_authority": 1.0},
    )
    optional = EvidenceSpan(
        evidence_id="opt", source_family="ado", text="optional fact", required=False,
        origin=ContentOrigin.SYSTEM, trust_level="high", verification_state="verified",
        injection_screen="pass", salience_inputs={"source_authority": 0.5},
    )
    full_prompt = (
        "sys\n\nschema\n\nEvidence:\n\n"
        "[REQUIRED required origin=system trust=high verification=verified]\nrequired fact\n\n"
        "[EVIDENCE first origin=system trust=high verification=verified]\nfirst optional fact\n\n"
        "[EVIDENCE opt origin=system trust=high verification=verified]\noptional fact"
    )
    request = ContextCompileRequest(
        program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
        system_instructions="sys", output_schema_text="schema", required_evidence=(required,),
        optional_evidence=(first, optional), max_input_tokens=len(full_prompt) + 3, reserved_output_tokens=3,
    )
    estimator = SimpleNamespace(
        tokenizer_id="boundary-overhead",
        estimate=lambda text: len(text) + (5 if "\n\n[EVIDENCE opt " in text else 0),
    )

    def build_context(_request: object) -> ContextBuildResult:
        compiled = DeterministicContextCompiler(
            token_estimator=estimator, programs_root=tmp_path / "programs",
        ).compile(
            request,
            validation_context=ValidationContext(
                program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
            ),
        )
        return ContextBuildResult(compiled=compiled)

    provider = Mock(
        return_value=BatchProviderResult(responses={"item-a": "a", "item-b": "b"}) if batch else "a",
    )
    fallback = Mock()
    runner = _runner(tmp_path)
    if batch:
        spec = _batchable_spec(
            tmp_path, call_provider_batch=provider, build_batch_context=build_context, fallback_handler=fallback,
        )
        outcomes = list(runner.run_batch(spec, ["item-a", "item-b"]).values())
        assert len(outcomes) == 2
    else:
        spec = _spec(tmp_path, call_provider=provider, build_context=build_context, fallback_handler=fallback)
        outcomes = [runner.run(spec, "item-a")]

    assert all(outcome.status == FeatureOutcomeStatus.RELEASED and outcome.tier == Tier.FRONTIER for outcome in outcomes)
    provider.assert_called_once()
    fallback.assert_not_called()
    compiled = provider.call_args.args[1]
    assert compiled.manifest.included_evidence_ids == ("required", "first")
    assert [(entry.evidence_id, entry.reason) for entry in compiled.excluded] == [("opt", "token_budget")]
    assert compiled.manifest.truncated
    assert compiled.manifest.token_estimate_total == estimator.estimate(compiled.prompt_text)
    assert compiled.manifest.token_estimate_total + request.reserved_output_tokens <= request.max_input_tokens


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


def _seed_frontier_counters(
    path: Path, scope_key: str, remote: object, frontier: object, *, nullable: bool = False,
) -> None:
    if nullable:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                "CREATE TABLE call_ceiling_counters "
                "(scope_key TEXT NOT NULL, kind TEXT NOT NULL, call_count INTEGER, updated_at TEXT NOT NULL, "
                "PRIMARY KEY (scope_key, kind))"
            )
            connection.commit()
    else:
        CallCeilingLedger(path).reserve_call(scope_key="unrelated", kind="remote", ceiling=1)
    with closing(sqlite3.connect(path)) as connection:
        connection.executemany(
            "INSERT INTO call_ceiling_counters (scope_key, kind, call_count, updated_at) VALUES (?, ?, ?, ?)",
            [(scope_key, "remote", remote, "original"), (scope_key, "frontier", frontier, "original")],
        )
        connection.commit()


def _ceiling_counter_snapshot(path: Path) -> list[tuple[str, str, str, str, str]]:
    with closing(sqlite3.connect(path)) as connection:
        return list(connection.execute(
            "SELECT scope_key, kind, typeof(call_count), quote(call_count), updated_at "
            "FROM call_ceiling_counters ORDER BY scope_key, kind"
        ))


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("kind", ["remote", "frontier"])
@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param(-1, id="negative"),
        pytest.param(0.5, id="fraction"),
        pytest.param(1.5, id="larger-fraction"),
        pytest.param(float("inf"), id="infinite"),
        pytest.param("synthetic-private-counter", id="text"),
        pytest.param(b"synthetic-private-counter", id="blob"),
        pytest.param(None, id="nullable-legacy-schema"),
    ],
)
def test_retained_ceiling_counter_rejects_invalid_state_atomically(
    tmp_path: Path, batch: bool, kind: str, invalid: object,
) -> None:
    path = tmp_path / "ceilings.sqlite3"
    scope = "acme:run-1:test_feature:v1"
    remote = invalid if kind == "remote" else 0
    frontier = invalid if kind == "frontier" else 0
    _seed_frontier_counters(path, scope, remote, frontier, nullable=invalid is None)
    before = _ceiling_counter_snapshot(path)
    response = BatchProviderResult(responses={"first": "first", "second": "second"}) if batch else "first"
    provider = Mock(return_value=response)
    spec = _frontier_test_spec(tmp_path, batch, provider, 2, 2)

    with pytest.raises(StateError) as error:
        _dispatch_frontier(_runner(tmp_path), spec, batch)

    assert str(error.value) == "Call-ceiling ledger contains an invalid counter."
    provider.assert_not_called()
    assert _ceiling_counter_snapshot(path) == before


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("kind", ["remote", "frontier"])
@pytest.mark.parametrize(
    "invalid",
    [
        pytest.param(-1, id="negative"),
        pytest.param(True, id="true"),
        pytest.param(False, id="false"),
        pytest.param(0.5, id="fraction"),
        pytest.param(float("inf"), id="infinite"),
        pytest.param(float("nan"), id="nan"),
        pytest.param("synthetic-private-limit", id="text"),
    ],
)
def test_invalid_call_ceiling_limit_blocks_dispatch_atomically(
    tmp_path: Path, batch: bool, kind: str, invalid: object,
) -> None:
    path = tmp_path / "ceilings.sqlite3"
    scope = "acme:run-1:test_feature:v1"
    _seed_frontier_counters(path, scope, 0, 0)
    before = _ceiling_counter_snapshot(path)
    response = BatchProviderResult(responses={"first": "first", "second": "second"}) if batch else "first"
    provider = Mock(return_value=response)
    invalid_limit = cast(int, invalid)
    remote = invalid_limit if kind == "remote" else 2
    frontier = invalid_limit if kind == "frontier" else 2
    spec = _frontier_test_spec(tmp_path, batch, provider, remote, frontier)

    with pytest.raises(StateError) as error:
        _dispatch_frontier(_runner(tmp_path), spec, batch)

    assert str(error.value) == "Call-ceiling limit must be a nonnegative integer or None."
    provider.assert_not_called()
    assert _ceiling_counter_snapshot(path) == before


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("kind", ["remote", "frontier"])
def test_invalid_call_ceiling_limit_does_not_create_ledger(
    tmp_path: Path, batch: bool, kind: str,
) -> None:
    path = tmp_path / "ceilings.sqlite3"
    provider = Mock()
    spec = _frontier_test_spec(
        tmp_path, batch, provider, -1 if kind == "remote" else 2, -1 if kind == "frontier" else 2,
    )

    with pytest.raises(StateError, match="Call-ceiling limit must be a nonnegative integer or None"):
        _dispatch_frontier(_runner(tmp_path), spec, batch)

    provider.assert_not_called()
    assert not path.exists()


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("kind", ["remote", "frontier"])
def test_sqlite_call_counter_refuses_increment_beyond_integer_storage(
    tmp_path: Path, batch: bool, kind: str,
) -> None:
    maximum = (1 << 63) - 1
    path = tmp_path / "ceilings.sqlite3"
    scope = "acme:run-1:test_feature:v1"
    _seed_frontier_counters(
        path, scope, maximum if kind == "remote" else 0, maximum if kind == "frontier" else 0,
    )
    before = _ceiling_counter_snapshot(path)
    response = BatchProviderResult(responses={"first": "first", "second": "second"}) if batch else "first"
    provider = Mock(return_value=response)
    remote = maximum + 1 if kind == "remote" else 2
    frontier = maximum + 1 if kind == "frontier" else 2
    spec = _frontier_test_spec(tmp_path, batch, provider, remote, frontier)

    with pytest.raises(StateError) as error:
        _dispatch_frontier(_runner(tmp_path), spec, batch)

    assert str(error.value) == "Call-ceiling ledger counter cannot be incremented."
    provider.assert_not_called()
    assert _ceiling_counter_snapshot(path) == before


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("kind", ["remote", "frontier"])
@pytest.mark.parametrize("exhausted", [False, True])
def test_sqlite_call_counter_preserves_maximum_and_ceiling_outcomes(
    tmp_path: Path, batch: bool, kind: str, exhausted: bool,
) -> None:
    maximum = (1 << 63) - 1
    path = tmp_path / "ceilings.sqlite3"
    scope = "acme:run-1:test_feature:v1"
    _seed_frontier_counters(
        path, scope, maximum - 1 if kind == "remote" else 0, maximum - 1 if kind == "frontier" else 0,
    )
    response = BatchProviderResult(responses={"first": "first", "second": "second"}) if batch else "first"
    provider = Mock(return_value=response)
    limit = maximum if exhausted else maximum + 1
    remote = limit if kind == "remote" else 2
    frontier = limit if kind == "frontier" else 2
    spec = _frontier_test_spec(tmp_path, batch, provider, remote, frontier)
    runner = _runner(tmp_path)

    assert all(outcome.status == FeatureOutcomeStatus.RELEASED for outcome in _dispatch_frontier(runner, spec, batch))
    after_first = _ceiling_counter_snapshot(path)
    if exhausted:
        outcomes = _dispatch_frontier(runner, spec, batch)
        assert all(outcome.status == FeatureOutcomeStatus.DETERMINISTIC_FALLBACK for outcome in outcomes)
        assert all(outcome.limitations == (f"{kind}_cap_exhausted",) for outcome in outcomes)
    else:
        with pytest.raises(StateError, match="cannot be incremented"):
            _dispatch_frontier(runner, spec, batch)
    provider.assert_called_once()
    assert _ceiling_counter_snapshot(path) == after_first


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("kind", ["remote", "frontier"])
def test_unbounded_call_ceiling_ignores_its_retained_counter(
    tmp_path: Path, batch: bool, kind: str,
) -> None:
    path = tmp_path / "ceilings.sqlite3"
    scope = "acme:run-1:test_feature:v1"
    invalid = "synthetic-private-counter"
    _seed_frontier_counters(
        path, scope, invalid if kind == "remote" else 0, invalid if kind == "frontier" else 0,
    )
    response = BatchProviderResult(responses={"first": "first", "second": "second"}) if batch else "first"
    provider = Mock(return_value=response)
    spec = _frontier_test_spec(
        tmp_path, batch, provider, None if kind == "remote" else 1, None if kind == "frontier" else 1,
    )

    outcomes = _dispatch_frontier(_runner(tmp_path), spec, batch)

    assert all(outcome.status == FeatureOutcomeStatus.RELEASED for outcome in outcomes)
    provider.assert_called_once()
    with closing(sqlite3.connect(path)) as connection:
        rows = dict(connection.execute(
            "SELECT kind, call_count FROM call_ceiling_counters WHERE scope_key = ?", (scope,),
        ))
    assert rows[kind] == invalid
    assert rows["frontier" if kind == "remote" else "remote"] == 1


@pytest.mark.parametrize("batch", [False, True])
@pytest.mark.parametrize("seeded", [False, True])
def test_unbounded_call_ceiling_ignores_all_retained_counters(
    tmp_path: Path, batch: bool, seeded: bool,
) -> None:
    path = tmp_path / "ceilings.sqlite3"
    if seeded:
        _seed_frontier_counters(
            path, "acme:run-1:test_feature:v1", "synthetic-private-counter", b"synthetic-private-counter",
        )
        before = _ceiling_counter_snapshot(path)
    response = BatchProviderResult(responses={"first": "first", "second": "second"}) if batch else "first"
    provider = Mock(return_value=response)
    spec = _frontier_test_spec(tmp_path, batch, provider, None, None)

    outcomes = _dispatch_frontier(_runner(tmp_path), spec, batch)

    assert all(outcome.status == FeatureOutcomeStatus.RELEASED for outcome in outcomes)
    provider.assert_called_once()
    if seeded:
        assert _ceiling_counter_snapshot(path) == before
    else:
        assert not path.exists()


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
