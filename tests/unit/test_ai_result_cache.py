"""ADF-W5.2: src/core/ai_result_cache.py."""
from __future__ import annotations

from pathlib import Path

from src.core.ai_result_cache import (
    AIResultCacheKey,
    canonical_input_hash,
    get_ai_result,
    put_ai_result,
)


def _key(**overrides: str) -> AIResultCacheKey:
    defaults = dict(
        program_id="xpf",
        feature="risk_proposal_generator",
        canonical_input_hash=canonical_input_hash("some canonical input text"),
        prompt_version="risk_proposal.v1",
        policy_version="policy-2026-07-13",
        model_deployment="gpt-5.4-mini",
        context_manifest_hash="manifest-abc123",
        output_schema_version="1",
    )
    defaults.update(overrides)
    return AIResultCacheKey(**defaults)  # type: ignore[arg-type]


def test_miss_when_nothing_cached(tmp_path: Path) -> None:
    assert get_ai_result(_key(), programs_root=tmp_path) is None


def test_put_then_get_round_trips(tmp_path: Path) -> None:
    key = _key()
    put_ai_result(key, {"causal_title": "Vendor delay"}, programs_root=tmp_path)
    hit = get_ai_result(key, programs_root=tmp_path)
    assert hit is not None
    assert hit.value == {"causal_title": "Vendor delay"}
    assert hit.was_cached is True
    assert hit.model_deployment == "gpt-5.4-mini"


def test_different_program_id_is_a_miss_no_cross_program_reuse(tmp_path: Path) -> None:
    put_ai_result(_key(program_id="xpf"), {"x": 1}, programs_root=tmp_path)
    assert get_ai_result(_key(program_id="armada"), programs_root=tmp_path) is None


def test_different_canonical_input_hash_is_a_miss(tmp_path: Path) -> None:
    put_ai_result(_key(canonical_input_hash="hash-a"), {"x": 1}, programs_root=tmp_path)
    assert get_ai_result(_key(canonical_input_hash="hash-b"), programs_root=tmp_path) is None


def test_prompt_version_bump_invalidates_cache(tmp_path: Path) -> None:
    put_ai_result(_key(prompt_version="v1"), {"x": 1}, programs_root=tmp_path)
    assert get_ai_result(_key(prompt_version="v2"), programs_root=tmp_path) is None


def test_policy_version_bump_invalidates_cache(tmp_path: Path) -> None:
    put_ai_result(_key(policy_version="p1"), {"x": 1}, programs_root=tmp_path)
    assert get_ai_result(_key(policy_version="p2"), programs_root=tmp_path) is None


def test_output_schema_version_bump_invalidates_cache(tmp_path: Path) -> None:
    put_ai_result(_key(output_schema_version="1"), {"x": 1}, programs_root=tmp_path)
    assert get_ai_result(_key(output_schema_version="2"), programs_root=tmp_path) is None


def test_canonical_input_hash_is_stable_sha256() -> None:
    a = canonical_input_hash("same text")
    b = canonical_input_hash("same text")
    c = canonical_input_hash("different text")
    assert a == b
    assert a != c
    assert len(a) == 64  # sha256 hex digest
