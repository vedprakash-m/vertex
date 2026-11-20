from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from src.ai.structured_feature_runner import (
    BatchProviderResult, ContextBuildResult, DeterministicCacheIdentity, DeterministicResultCache,
    FeatureOutcomeStatus, ReleasedResultCache, StructuredFeatureRunner,
)
from src.ai.tiered_router import RouteOutcome, Tier, TierResult
from src.core.context_compiler import ContextCompileRequest, DeterministicContextCompiler, ValidationContext
from src.core.exceptions import StateError
from src.core.selection_identity import UNVERIFIED
from tests.unit.test_structured_feature_runner import _compiled, _context, _identity, _runner, _spec


@pytest.mark.parametrize("execution", ["cached", "uncached", "batch", "per-item-batch"])
@pytest.mark.parametrize("mutation", ["prompt", "hash", "non-string-hash", "non-string", "surrogate"])
def test_compiled_prompt_hash_blocks_mismatch_before_cache_reservation_or_dispatch(
    tmp_path: Path, execution: str, mutation: str,
) -> None:
    compiled = _compiled()
    if mutation == "prompt":
        compiled = replace(compiled, prompt_text="synthetic-private-changed-prompt")
    elif mutation == "hash":
        wrong_hash = hashlib.sha256(b"synthetic-private-other").hexdigest()
        compiled = replace(compiled, manifest=replace(compiled.manifest, context_hash=wrong_hash))
    elif mutation == "non-string-hash":
        compiled = replace(compiled, manifest=replace(compiled.manifest, context_hash=cast(str, None)))
    elif mutation == "non-string":
        compiled = replace(compiled, prompt_text=cast(str, None))
    else:
        compiled = replace(compiled, prompt_text=chr(0xD800))

    cache = Mock(spec=ReleasedResultCache)
    cache.get.return_value = None
    ledger = Mock()
    provider = Mock(return_value="unexpected")
    batch_provider = Mock(return_value=BatchProviderResult(responses={"one": "unexpected", "two": "unexpected"}))
    fallback = Mock()
    decisions = []
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, released_cache=cache,
        deterministic_cache=Mock(), ceiling_ledger=ledger, decision_sink=decisions.append,
    )
    build = lambda _: ContextBuildResult(compiled=compiled)
    options = dict(build_context=build, call_provider=provider, fallback_handler=fallback)
    if execution in {"cached", "per-item-batch"}:
        options.update(
            request_hash=lambda request: request, deployment_fingerprint="test", validator_version="v1",
            serialize_response=str, deserialize_response=str,
        )
    if execution == "batch":
        options.update(item_id=lambda request: request, build_batch_context=build, call_provider_batch=batch_provider)
    spec = _spec(tmp_path, **options)

    if execution in {"batch", "per-item-batch"}:
        outcomes = list(runner.run_batch(spec, ["one", "two"]).values())
        assert len(outcomes) == 2
    else:
        outcomes = [runner.run(spec, "one")]

    for outcome in outcomes:
        assert outcome.status == FeatureOutcomeStatus.BLOCKED
        assert outcome.value is None
        assert outcome.tier == Tier.NONE
        assert outcome.retryable is False
        assert outcome.next_action
        assert "synthetic-private" not in repr(outcome)
    cache.get.assert_not_called()
    cache.put.assert_not_called()
    ledger.reserve_call.assert_not_called()
    ledger.reserve_frontier_call.assert_not_called()
    provider.assert_not_called()
    batch_provider.assert_not_called()
    fallback.assert_not_called()
    assert len(decisions) == (2 if execution == "per-item-batch" else 1)
    assert all(decision.outcome == RouteOutcome.SKIPPED and not decision.frontier_called for decision in decisions)


@pytest.mark.parametrize("prompt", ["", "ascii", "café", "emoji \U0001f600"])
def test_compiled_prompt_hash_accepts_exact_utf8_text(tmp_path: Path, prompt: str) -> None:
    compiled = _compiled()
    compiled = replace(
        compiled, prompt_text=prompt,
        manifest=replace(compiled.manifest, context_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest()),
    )
    provider = Mock(return_value="valid")
    spec = _spec(tmp_path, build_context=lambda _: ContextBuildResult(compiled=compiled), call_provider=provider)

    outcome = _runner(tmp_path).run(spec, "one")

    assert outcome.status == FeatureOutcomeStatus.RELEASED
    assert outcome.value == "valid"
    provider.assert_called_once()


def test_compiled_prompt_hash_prevents_stale_released_cache_replay(tmp_path: Path) -> None:
    request = ContextCompileRequest(
        program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
        system_instructions="original café", output_schema_text="schema", required_evidence=(),
        optional_evidence=(), max_input_tokens=1000, reserved_output_tokens=0,
    )
    validation = ValidationContext(
        program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
    )
    compiler = DeterministicContextCompiler(
        token_estimator=SimpleNamespace(tokenizer_id="synthetic", estimate=len),
        programs_root=tmp_path / "programs",
    )
    original = compiler.compile(request, validation_context=validation)
    active = [original]
    provider = Mock(side_effect=lambda _request, compiled: compiled.prompt_text)
    spec = _spec(
        tmp_path, build_context=lambda _: ContextBuildResult(compiled=active[0]), call_provider=provider,
        request_hash=lambda value: value, deployment_fingerprint="test", validator_version="v1",
        serialize_response=str, deserialize_response=str,
    )
    runner = _runner(tmp_path)

    first = runner.run(spec, "same")
    assert (first.tier, first.value) == (Tier.FRONTIER, original.prompt_text)
    cache_path = tmp_path / "released_cache.sqlite3"
    with sqlite3.connect(cache_path) as connection:
        before = connection.execute("SELECT * FROM released_result_cache").fetchall()
    assert len(before) == 1

    active[0] = replace(original, prompt_text=original.prompt_text + "\nsynthetic-private-changed-prompt")
    stale = runner.run(spec, "same")

    assert stale.status == FeatureOutcomeStatus.BLOCKED
    assert stale.value is None
    assert "synthetic-private" not in repr(stale)
    provider.assert_called_once()
    with sqlite3.connect(cache_path) as connection:
        assert connection.execute("SELECT * FROM released_result_cache").fetchall() == before

    active[0] = compiler.compile(replace(request, system_instructions="fresh café"), validation_context=validation)
    fresh = runner.run(spec, "same")
    assert (fresh.tier, fresh.value) == (Tier.FRONTIER, active[0].prompt_text)
    assert provider.call_count == 2


def test_expired_released_cache_row_recomputes_after_seven_days(tmp_path: Path) -> None:
    cache = ReleasedResultCache(
        tmp_path / "released_cache.sqlite3",
        record_release=lambda *_args: "origin-run",
        verify_release=lambda *_args: True,
    )
    provider = Mock(side_effect=["first-value", "second-value"])
    runner = StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=True,
        ceiling_ledger=Mock(),
        released_cache=cache,
        deterministic_cache=DeterministicResultCache(tmp_path / "deterministic.sqlite3"),
    )
    spec = _spec(
        tmp_path,
        build_context=lambda _: ContextBuildResult(compiled=_compiled()),
        call_provider=provider,
        request_hash=lambda value: value,
        deployment_fingerprint="test",
        validator_version="v1",
        serialize_response=str,
        deserialize_response=str,
    )
    assert runner.run(spec, "same").value == "first-value"
    with sqlite3.connect(tmp_path / "released_cache.sqlite3") as connection:
        connection.execute(
            "UPDATE released_result_cache SET cached_at = ?",
            ((datetime.now(timezone.utc) - timedelta(days=7, seconds=1)).isoformat(),),
        )

    result = runner.run(spec, "same")

    assert result.tier == Tier.FRONTIER
    assert result.value == "second-value"
    assert provider.call_count == 2


@pytest.mark.parametrize("invalid_time", ["future", "non-utc"])
def test_released_cache_rejects_noncanonical_utc_time(
    tmp_path: Path, invalid_time: str,
) -> None:
    cache = ReleasedResultCache(
        tmp_path / "released_cache.sqlite3",
        record_release=lambda *_args: "origin-run",
        verify_release=lambda *_args: True,
    )
    provider = Mock(side_effect=["first-value", "second-value"])
    runner = StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=True,
        ceiling_ledger=Mock(),
        released_cache=cache,
        deterministic_cache=DeterministicResultCache(tmp_path / "deterministic.sqlite3"),
    )
    spec = _spec(
        tmp_path,
        build_context=lambda _: ContextBuildResult(compiled=_compiled()),
        call_provider=provider,
        request_hash=lambda value: value,
        deployment_fingerprint="test",
        validator_version="v1",
        serialize_response=str,
        deserialize_response=str,
    )
    assert runner.run(spec, "same").value == "first-value"
    now = datetime.now(timezone.utc)
    if invalid_time == "future":
        cached_at = (now + timedelta(seconds=30)).isoformat()
    else:
        cached_at = (now - timedelta(minutes=1)).astimezone(timezone(timedelta(hours=5))).isoformat()
    with sqlite3.connect(tmp_path / "released_cache.sqlite3") as connection:
        connection.execute("UPDATE released_result_cache SET cached_at = ?", (cached_at,))

    result = runner.run(spec, "same")

    assert result.tier == Tier.FRONTIER
    assert result.value == "second-value"
    assert provider.call_count == 2


@pytest.mark.parametrize(
    "changed",
    ["prompt", "deployment", "validator"],
)
def test_released_cache_cannot_reuse_across_frozen_identity_changes(
    tmp_path: Path,
    changed: str,
) -> None:
    compiled = [_compiled()]
    provider = Mock(side_effect=["first-value", "second-value"])
    spec = _spec(
        tmp_path,
        build_context=lambda _: ContextBuildResult(compiled=compiled[0]),
        call_provider=provider,
        request_hash=lambda request: request,
        deployment_fingerprint="model-v1",
        prompt_version="v1",
        validator_version="validator-v1",
        serialize_response=str,
        deserialize_response=str,
    )
    runner = _runner(tmp_path)

    first = runner.run(spec, "same")
    if changed == "prompt":
        replacement_prompt = "fresh frozen prompt"
        compiled[0] = replace(
            compiled[0],
            prompt_text=replacement_prompt,
            manifest=replace(
                compiled[0].manifest,
                prompt_version="v2",
                context_hash=hashlib.sha256(
                    replacement_prompt.encode("utf-8")
                ).hexdigest(),
            ),
        )
        changed_spec = replace(spec, prompt_version="v2")
    elif changed == "deployment":
        changed_spec = replace(spec, deployment_fingerprint="model-v2")
    else:
        changed_spec = replace(spec, validator_version="validator-v2")

    second = runner.run(changed_spec, "same")

    assert (first.tier, first.value) == (Tier.FRONTIER, "first-value")
    assert (second.tier, second.value) == (Tier.FRONTIER, "second-value")
    assert provider.call_count == 2


@pytest.mark.parametrize("selection", [UNVERIFIED, "", "sha256:not-a-digest"])
def test_unverified_or_malformed_selection_is_not_cached(
    tmp_path: Path, selection: str,
) -> None:
    compiled = replace(
        _compiled(),
        manifest=replace(_compiled().manifest, selection_identity=selection),
        selection_identity=selection,
    )
    provider = Mock(side_effect=["first-value", "second-value"])
    cache = ReleasedResultCache(
        tmp_path / "released_cache.sqlite3",
        record_release=lambda *_args: "origin-run",
        verify_release=lambda *_args: True,
    )
    runner = StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=True,
        ceiling_ledger=Mock(),
        released_cache=cache,
        deterministic_cache=Mock(),
    )
    spec = _spec(
        tmp_path,
        build_context=lambda _: ContextBuildResult(compiled=compiled),
        call_provider=provider,
        request_hash=lambda value: value,
        deployment_fingerprint="test",
        validator_version="v1",
        serialize_response=str,
        deserialize_response=str,
    )

    assert runner.run(spec, "same").value == "first-value"
    assert runner.run(spec, "same").value == "second-value"
    assert provider.call_count == 2
    assert not cache._db_path.exists()


def test_changed_verified_selection_does_not_reuse_released_value(tmp_path: Path) -> None:
    active = [_compiled()]
    provider = Mock(side_effect=["first-selection", "second-selection"])
    cache = ReleasedResultCache(
        tmp_path / "released_cache.sqlite3",
        record_release=lambda *_args: "origin-run",
        verify_release=lambda *_args: True,
    )
    runner = StructuredFeatureRunner(
        context=_context(tmp_path),
        frontier_eligible=True,
        ceiling_ledger=Mock(),
        released_cache=cache,
        deterministic_cache=Mock(),
    )
    spec = _spec(
        tmp_path,
        build_context=lambda _: ContextBuildResult(compiled=active[0]),
        call_provider=provider,
        request_hash=lambda value: value,
        deployment_fingerprint="test",
        validator_version="v1",
        serialize_response=str,
        deserialize_response=str,
    )
    assert runner.run(spec, "same").value == "first-selection"

    selection = "sha256:" + "b" * 64
    active[0] = replace(
        active[0],
        manifest=replace(active[0].manifest, selection_identity=selection),
        selection_identity=selection,
    )
    assert runner.run(spec, "same").value == "second-selection"
    assert provider.call_count == 2


@pytest.mark.parametrize("changed", ["prompt", "deployment", "validator"])
def test_released_cache_cannot_reuse_across_frozen_identity_changes(
    tmp_path: Path, changed: str,
) -> None:
    compiled = [_compiled()]
    provider = Mock(side_effect=["first-value", "second-value"])
    spec = _spec(
        tmp_path,
        build_context=lambda _: ContextBuildResult(compiled=compiled[0]),
        call_provider=provider,
        request_hash=lambda request: request,
        deployment_fingerprint="model-v1",
        prompt_version="v1",
        validator_version="validator-v1",
        serialize_response=str,
        deserialize_response=str,
    )
    runner = _runner(tmp_path)

    first = runner.run(spec, "same")
    if changed == "prompt":
        replacement_prompt = "fresh frozen prompt"
        compiled[0] = replace(
            compiled[0],
            prompt_text=replacement_prompt,
            manifest=replace(
                compiled[0].manifest,
                prompt_version="v2",
                context_hash=hashlib.sha256(replacement_prompt.encode("utf-8")).hexdigest(),
            ),
        )
        changed_spec = replace(spec, prompt_version="v2")
    elif changed == "deployment":
        changed_spec = replace(spec, deployment_fingerprint="model-v2")
    else:
        changed_spec = replace(spec, validator_version="validator-v2")

    second = runner.run(changed_spec, "same")
    assert (first.tier, first.value) == (Tier.FRONTIER, "first-value")
    assert (second.tier, second.value) == (Tier.FRONTIER, "second-value")
    assert provider.call_count == 2


@pytest.mark.parametrize("missing", ["prompt_version", "deployment_fingerprint", "validator_version"])
def test_released_cache_refuses_incomplete_frozen_identity(
    tmp_path: Path, missing: str,
) -> None:
    compiled = _compiled()
    options = {
        "prompt_version": compiled.manifest.prompt_version,
        "deployment_fingerprint": "model-v1",
        "validator_version": "validator-v1",
    }
    options[missing] = " \t"
    if missing == "prompt_version":
        compiled = replace(
            compiled,
            manifest=replace(compiled.manifest, prompt_version=" \t"),
        )
    provider = Mock(side_effect=["first", "second"])
    spec = _spec(
        tmp_path,
        build_context=lambda _: ContextBuildResult(compiled=compiled),
        call_provider=provider,
        request_hash=lambda request: request,
        serialize_response=str,
        deserialize_response=str,
        **options,
    )
    runner = _runner(tmp_path)

    first = runner.run(spec, "same")
    second = runner.run(spec, "same")

    assert (first.tier, first.value) == (Tier.FRONTIER, "first")
    assert (second.tier, second.value) == (Tier.FRONTIER, "second")
    assert provider.call_count == 2


def test_model_cache_identity_changes_with_compiler_cache_key(tmp_path: Path) -> None:
    compiled = _compiled()
    runner = _runner(tmp_path)
    spec = _spec(
        tmp_path, request_hash=lambda request: request,
        deployment_fingerprint="test", validator_version="v1",
        serialize_response=str, deserialize_response=str,
    )
    first = runner._cache_identity(spec, "same", ContextBuildResult(compiled=compiled))
    changed = replace(compiled, manifest=replace(compiled.manifest, cache_key="another-compiler-key"))
    second = runner._cache_identity(spec, "same", ContextBuildResult(compiled=changed))
    assert first is not None and second is not None
    assert first.cache_key() != second.cache_key()


@pytest.mark.parametrize("compiler_key", ["", " \t", None, 123])
def test_model_cache_rejects_missing_compiler_key_before_dispatch(tmp_path: Path, compiler_key: object) -> None:
    compiled = _compiled()
    invalid = replace(compiled, manifest=replace(compiled.manifest, cache_key=cast(str, compiler_key)))
    provider = Mock(return_value="unexpected")
    cache = Mock(spec=ReleasedResultCache)
    ledger = Mock()
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger,
        released_cache=cache, deterministic_cache=Mock(spec=DeterministicResultCache),
    )
    spec = _spec(
        tmp_path, build_context=lambda _: ContextBuildResult(compiled=invalid),
        call_provider=provider, request_hash=lambda value: value,
        deployment_fingerprint="test", validator_version="v1",
        serialize_response=str, deserialize_response=str,
    )

    with pytest.raises(StateError, match="Compiled context cache identity is invalid"):
        runner.run(spec, "same")
    cache.get.assert_not_called()
    cache.put.assert_not_called()
    ledger.reserve_frontier_call.assert_not_called()
    provider.assert_not_called()


def test_uncached_runner_does_not_require_compiler_key(tmp_path: Path) -> None:
    compiled = _compiled()
    invalid = replace(compiled, manifest=replace(compiled.manifest, cache_key=""))
    provider = Mock(return_value="uncached-value")
    cache = Mock(spec=ReleasedResultCache)
    ledger = Mock()
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger,
        released_cache=cache, deterministic_cache=Mock(spec=DeterministicResultCache),
    )
    spec = _spec(
        tmp_path, build_context=lambda _: ContextBuildResult(compiled=invalid), call_provider=provider,
    )

    outcome = runner.run(spec, "same")
    assert (outcome.tier, outcome.value) == (Tier.FRONTIER, "uncached-value")
    provider.assert_called_once()
    ledger.reserve_frontier_call.assert_called_once()
    cache.get.assert_not_called()
    cache.put.assert_not_called()


def test_model_cache_partitions_compiler_schema_boundaries(tmp_path: Path) -> None:
    request = ContextCompileRequest(
        program_id="acme", edition_id=None, feature="test_feature", prompt_version="v1",
        system_instructions="system\n\nschema-part", output_schema_text="schema-end",
        required_evidence=(), optional_evidence=(), max_input_tokens=1000, reserved_output_tokens=0,
    )
    validation = ValidationContext(
        program_id="acme", run_id="run-1", execution_mode="observe", classification="internal",
    )
    compiler = DeterministicContextCompiler(
        token_estimator=SimpleNamespace(tokenizer_id="synthetic", estimate=len),
        programs_root=tmp_path / "programs",
    )
    first = compiler.compile(request, validation_context=validation)
    other_request = replace(request, system_instructions="system", output_schema_text="schema-part\n\nschema-end")
    second = compiler.compile(other_request, validation_context=validation)
    equivalent = compiler.compile(
        replace(other_request, output_schema_text="  schema-part\n\nschema-end\n"), validation_context=validation,
    )
    assert first.prompt_text == second.prompt_text == equivalent.prompt_text
    assert first.manifest.context_hash == second.manifest.context_hash == equivalent.manifest.context_hash
    assert first.manifest.cache_key != second.manifest.cache_key == equivalent.manifest.cache_key

    active = [first]
    provider = Mock(side_effect=["first-value", "second-value"])
    spec = _spec(
        tmp_path, build_context=lambda _: ContextBuildResult(compiled=active[0]),
        call_provider=provider, request_hash=lambda value: value,
        deployment_fingerprint="test", validator_version="v1",
        serialize_response=str, deserialize_response=str,
    )
    ledger = Mock()
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger,
        released_cache=ReleasedResultCache(
            tmp_path / "released_cache.sqlite3",
            record_release=lambda *_args: "origin-run",
            verify_release=lambda *_args: True,
        ),
        deterministic_cache=DeterministicResultCache(tmp_path / "deterministic_cache.sqlite3"),
    )

    first_result = runner.run(spec, "same")
    assert (first_result.tier, first_result.value) == (Tier.FRONTIER, "first-value")
    first_hit = runner.run(spec, "same")
    assert (first_hit.tier, first_hit.value) == (Tier.CACHE, "first-value")
    active[0] = second
    second_result = runner.run(spec, "same")
    assert (second_result.tier, second_result.value) == (Tier.FRONTIER, "second-value")
    active[0] = equivalent
    second_hit = runner.run(spec, "same")
    assert (second_hit.tier, second_hit.value) == (Tier.CACHE, "second-value")
    assert provider.call_count == ledger.reserve_frontier_call.call_count == 2


def test_old_released_cache_key_remains_but_misses(tmp_path: Path) -> None:
    db_path = tmp_path / "released_cache.sqlite3"
    cache = ReleasedResultCache(
        db_path,
        record_release=lambda *_args: "origin-run",
        verify_release=lambda *_args: True,
    )
    ledger = Mock()
    runner = StructuredFeatureRunner(
        context=_context(tmp_path), frontier_eligible=True, ceiling_ledger=ledger,
        released_cache=cache,
        deterministic_cache=DeterministicResultCache(tmp_path / "deterministic_cache.sqlite3"),
    )
    compiled = ContextBuildResult(compiled=_compiled())
    provider = Mock(return_value="new-value")
    spec = _spec(
        tmp_path, build_context=lambda _: compiled, call_provider=provider,
        request_hash=lambda value: value, deployment_fingerprint="test", validator_version="v1",
        serialize_response=str, deserialize_response=str,
    )
    identity = runner._cache_identity(spec, "same", compiled)
    assert identity is not None
    old_material = (
        identity.program_id, identity.feature, identity.request_hash, identity.context_hash,
        identity.prompt_version, identity.policy_version, identity.compiler_policy_version,
        identity.deployment_fingerprint, identity.tokenizer_id, identity.response_schema_version,
        identity.validator_version, identity.classification, identity.execution_classification,
        identity.principal, identity.snapshot_id,
    )
    old_key = hashlib.sha256(json.dumps(old_material, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert cache.get(identity, deserialize=str) is None
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            "INSERT INTO released_result_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (old_key, identity.feature, identity.principal, identity.classification, "old-value",
             hashlib.sha256(b"old-value").hexdigest(), "old-run", "2026-01-01T00:00:00Z",
             json.dumps(old_material, separators=(",", ":"))),
        )
        old_row = connection.execute(
            "SELECT * FROM released_result_cache WHERE cache_key = ?", (old_key,),
        ).fetchone()

    first = runner.run(spec, "same")
    second = runner.run(spec, "same")
    assert (first.tier, first.value) == (Tier.FRONTIER, "new-value")
    assert (second.tier, second.value) == (Tier.CACHE, "new-value")
    assert provider.call_count == ledger.reserve_frontier_call.call_count == 1
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM released_result_cache").fetchone()[0] == 1
        assert connection.execute(
            "SELECT * FROM released_result_cache WHERE cache_key = ?", (old_key,),
        ).fetchone() is None


def test_model_cache_identity_changes_with_execution_classification(tmp_path) -> None:
    compiled = ContextBuildResult(compiled=_compiled())
    spec = _spec(
        tmp_path, request_hash=lambda request: request,
        deployment_fingerprint="test", validator_version="v1",
        serialize_response=str, deserialize_response=str,
    )
    identities = []
    for classification in ("internal", "restricted"):
        runner = StructuredFeatureRunner(
            context=_context(tmp_path, classification=classification), frontier_eligible=True,
            released_cache=Mock(), ceiling_ledger=Mock(), deterministic_cache=Mock(),
        )
        identity = runner._cache_identity(spec, "same", compiled)
        assert identity is not None
        identities.append(identity)
    assert identities[0].cache_key() != identities[1].cache_key()
    assert [identity.classification for identity in identities] == ["internal", "internal"]
    assert [identity.execution_classification for identity in identities] == ["internal", "restricted"]


@pytest.mark.parametrize(
    ("first_classification", "second_classification"),
    [("internal", "restricted"), ("restricted", "internal")],
)
def test_model_cache_cannot_reuse_across_execution_classifications(
    tmp_path, first_classification, second_classification,
) -> None:
    cache = ReleasedResultCache(
        tmp_path / "shared-model.sqlite3",
        record_release=lambda *_args: "origin-run",
        verify_release=lambda *_args: True,
    )
    provider = Mock(side_effect=["first-profile", "second-profile"])
    spec = _spec(
        tmp_path, build_context=lambda _: ContextBuildResult(compiled=_compiled()),
        call_provider=provider, request_hash=lambda request: request,
        deployment_fingerprint="test", validator_version="v1",
        serialize_response=str, deserialize_response=str,
    )
    outcomes = []
    for classification in (first_classification, second_classification):
        runner = StructuredFeatureRunner(
            context=_context(tmp_path, classification=classification), frontier_eligible=True,
            released_cache=cache, ceiling_ledger=Mock(), deterministic_cache=Mock(),
        )
        first = runner.run(spec, "same")
        repeated = runner.run(spec, "same")
        assert first.released and repeated.released
        assert repeated.tier == Tier.CACHE
        assert repeated.value == first.value
        outcomes.append(first)
    assert [outcome.tier for outcome in outcomes] == [Tier.FRONTIER, Tier.FRONTIER]
    assert [outcome.value for outcome in outcomes] == ["first-profile", "second-profile"]
    assert provider.call_count == 2


def test_model_cache_changes_with_compiler_policy_independently_of_routing_policy(tmp_path) -> None:
    compiled = [_compiled()]
    provider = Mock(return_value="answer")
    spec = _spec(
        tmp_path, build_context=lambda _: ContextBuildResult(compiled=compiled[0]),
        call_provider=provider, request_hash=lambda request: request,
        deployment_fingerprint="test", validator_version="v1",
        serialize_response=str, deserialize_response=str,
    )
    runner = _runner(tmp_path)
    assert runner.run(spec, "same").tier == Tier.FRONTIER
    compiled[0] = replace(compiled[0], manifest=replace(compiled[0].manifest, policy_version="new-compiler-policy"))
    assert runner.run(spec, "same").tier == Tier.FRONTIER
    assert runner.run(spec, "same").tier == Tier.CACHE
    assert provider.call_count == 2


def test_shared_model_cache_cannot_cross_programs(tmp_path) -> None:
    cache = ReleasedResultCache(
        tmp_path / "shared-model.sqlite3",
        record_release=lambda *_args: "origin-run",
        verify_release=lambda *_args: True,
    )
    values = []
    for program in ("acme", "beta"):
        compiled = replace(_compiled(), manifest=replace(_compiled().manifest, program_id=program))
        runner = StructuredFeatureRunner(
            context=_context(tmp_path, program_id=program), frontier_eligible=True,
            released_cache=cache, ceiling_ledger=Mock(), deterministic_cache=Mock(),
        )
        spec = _spec(
            tmp_path, build_context=lambda _: ContextBuildResult(compiled=compiled),
            call_provider=lambda *_: program, request_hash=lambda request: request,
            deployment_fingerprint="test", validator_version="v1",
            serialize_response=str, deserialize_response=str,
        )
        values.append(runner.run(spec, "same").value)
    assert values == ["acme", "beta"]


def test_shared_deterministic_cache_cannot_cross_programs(tmp_path) -> None:
    cache = DeterministicResultCache(tmp_path / "shared-deterministic.sqlite3")
    values = []
    for program in ("acme", "beta"):
        runner = StructuredFeatureRunner(
            context=_context(tmp_path, program_id=program), frontier_eligible=False,
            released_cache=Mock(), ceiling_ledger=Mock(), deterministic_cache=cache,
        )
        spec = _spec(
            tmp_path, deterministic_handler=lambda _: TierResult(program, 1.0),
            deterministic_engine_version="v1", request_hash=lambda request: request,
            serialize_response=str, deserialize_response=str,
        )
        values.append(runner.run(spec, "same").value)
    assert values == ["acme", "beta"]


def test_cache_key_dimensions_cannot_collide_on_literal_delimiters() -> None:
    identities = (
        _identity(),
        DeterministicCacheIdentity(
            program_id="acme", feature="test", request_hash="request",
            snapshot_id="snapshot", deterministic_engine_version="v1",
            principal="operator", classification="internal",
        ),
    )
    for identity in identities:
        assert replace(identity, feature="alpha|beta", request_hash="gamma").cache_key() != replace(
            identity, feature="alpha", request_hash="beta|gamma",
        ).cache_key()
