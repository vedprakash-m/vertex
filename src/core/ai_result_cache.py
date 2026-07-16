"""ADF-W5.2 (specs/arch-data-fix.md Section 8.8.3): shared AI-result cache.

Generalizes `src/core/rev/rev_cache_store.py`'s content-addressed/TTL/LRU
primitives (already Zone-A-pure, already the "REV content-hash cache" the
spec names) into a cross-feature cache for any tiered AI call, rather than
reimplementing atomic-write/eviction logic a second time.

Cache key per Section 8.8.3, verbatim: program/tenant, feature, canonical
input hash, prompt version, policy version, model/deployment, context
manifest hash, output schema version. "No cross-program cache reuse" is
structurally guaranteed -- `program_id` is both part of the key AND part of
the on-disk path (`rev_cache_store._cache_dir` is per-program already).

A cache entry retains its originating model/deployment, prompt, policy,
schema version, and validation/release metadata (Section 8.8.3: "A cache
hit never masquerades as a new provider call") -- `CachedAIResult.was_cached`
is always `True` on a read, so a caller can tell the two apart.

Zone A -- no AI or M365 imports.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.core.rev.rev_cache_store import get_cached, put_cached

AI_RESULT_CACHE_KIND = "ai_result"

# Default TTL for a cached AI result. Shorter than REV's 90-day extraction
# cache (that cache targets stable historical email content; AI results here
# may back program-state-sensitive features where staleness matters more).
AI_RESULT_TTL_DAYS = 7
AI_RESULT_MAXSIZE = 1000

_KEY_LABELS = (
    "program_id",
    "feature",
    "canonical_input_hash",
    "prompt_version",
    "policy_version",
    "model_deployment",
    "context_manifest_hash",
    "output_schema_version",
)


@dataclass(frozen=True, slots=True)
class AIResultCacheKey:
    program_id: str
    feature: str
    canonical_input_hash: str
    prompt_version: str
    policy_version: str
    model_deployment: str
    context_manifest_hash: str
    output_schema_version: str

    def to_key_values(self) -> tuple[str, ...]:
        return (
            self.program_id,
            self.feature,
            self.canonical_input_hash,
            self.prompt_version,
            self.policy_version,
            self.model_deployment,
            self.context_manifest_hash,
            self.output_schema_version,
        )


@dataclass(frozen=True, slots=True)
class CachedAIResult:
    value: Any
    model_deployment: str
    prompt_version: str
    policy_version: str
    output_schema_version: str
    was_cached: bool = True


def canonical_input_hash(canonical_input: str) -> str:
    """Sha256 of the caller's own canonical (already-normalized) input text.
    Callers own canonicalization -- this module only hashes."""
    return hashlib.sha256(canonical_input.encode("utf-8")).hexdigest()


def get_ai_result(
    key: AIResultCacheKey,
    *,
    programs_root: Path,
    now_epoch: float | None = None,
) -> CachedAIResult | None:
    """Return the cached result, or None on miss/expiry/corrupt/schema-mismatch."""
    payload = get_cached(
        program_id=key.program_id,
        kind=AI_RESULT_CACHE_KIND,
        key_values=key.to_key_values(),
        programs_root=programs_root,
        ttl_days=AI_RESULT_TTL_DAYS,
        now_epoch=now_epoch,
    )
    if not isinstance(payload, dict):
        return None
    # A hit must carry a value under every key this cache always writes;
    # any missing field means the entry is not one this module wrote (or is
    # from an incompatible earlier version) -- treat as a miss, never guess.
    if "value" not in payload:
        return None
    if (
        payload.get("prompt_version") != key.prompt_version
        or payload.get("policy_version") != key.policy_version
        or payload.get("output_schema_version") != key.output_schema_version
    ):
        return None
    return CachedAIResult(
        value=payload["value"],
        model_deployment=str(payload.get("model_deployment", key.model_deployment)),
        prompt_version=key.prompt_version,
        policy_version=key.policy_version,
        output_schema_version=key.output_schema_version,
    )


def put_ai_result(
    key: AIResultCacheKey,
    value: Any,
    *,
    programs_root: Path,
    set_at_epoch: float | None = None,
    now_epoch: float | None = None,
) -> Path:
    """Persist a fresh (non-cached) provider result, retaining the metadata
    that produced it so a later hit can be distinguished from a fresh call."""
    payload = {
        "value": value,
        "model_deployment": key.model_deployment,
        "prompt_version": key.prompt_version,
        "policy_version": key.policy_version,
        "output_schema_version": key.output_schema_version,
    }
    return put_cached(
        program_id=key.program_id,
        kind=AI_RESULT_CACHE_KIND,
        key_values=key.to_key_values(),
        key_labels=_KEY_LABELS,
        payload=payload,
        programs_root=programs_root,
        set_at_epoch=set_at_epoch,
        now_epoch=now_epoch,
    )


__all__ = [
    "AI_RESULT_CACHE_KIND",
    "AI_RESULT_MAXSIZE",
    "AI_RESULT_TTL_DAYS",
    "AIResultCacheKey",
    "CachedAIResult",
    "canonical_input_hash",
    "get_ai_result",
    "put_ai_result",
]
