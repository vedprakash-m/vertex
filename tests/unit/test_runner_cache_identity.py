from __future__ import annotations

from dataclasses import replace
from unittest.mock import Mock

from src.ai.structured_feature_runner import (
    ContextBuildResult, DeterministicCacheIdentity, DeterministicResultCache, ReleasedResultCache, StructuredFeatureRunner,
)
from src.ai.tiered_router import Tier, TierResult
from tests.unit.test_structured_feature_runner import _compiled, _context, _identity, _runner, _spec


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
    cache = ReleasedResultCache(tmp_path / "shared-model.sqlite3")
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
