"""P2-8 / P2-12 — REV result-cache unit tests.

Covers the shared Zone-A cache store (``src.core.rev.rev_cache_store``): hit /
miss, prompt-version mismatch, TTL expiry + prune, LRU eviction at maxsize,
corrupt-file resilience, accessed_at touch, stats, clear, byte ceiling — plus
the wiring into ``LLMRevExtractor`` (extraction cache) and ``judge_extractions``
(judge cache).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, TypeVar

import pytest

from src.core.rev.rev_cache_store import (
    CACHE_SCHEMA_VERSION,
    EXTRACTION_MAXSIZE,
    EXTRACTION_TTL_DAYS,
    JUDGE_TTL_DAYS,
    MAX_ENTRY_BYTES,
    cache_stats,
    clear_cache,
    evict_lru,
    evict_stale,
    get_extraction_result,
    get_judge_result,
    hash_ground_truth,
    put_extraction_result,
    put_judge_result,
)

_DAY = 86_400.0
T0 = 1_700_000_000.0  # deterministic epoch baseline


# ---------------------------------------------------------------------------
# Extraction-result cache (P2-12) — store mechanics
# ---------------------------------------------------------------------------


class TestExtractionCacheStore:
    def test_put_then_get_is_a_hit(self, tmp_path: Path) -> None:
        claims = [{"event_type": "deployment.completed", "payload": {"date": "2026-06-20"}}]
        put_extraction_result(
            program_id="p1", source_hash="abc", prompt_version="rev_extractor.v1",
            claims=claims, programs_root=tmp_path, set_at_epoch=T0, now_epoch=T0,
        )
        got = get_extraction_result(
            program_id="p1", source_hash="abc", prompt_version="rev_extractor.v1",
            programs_root=tmp_path, now_epoch=T0,
        )
        assert got == claims

    def test_different_source_hash_is_a_miss(self, tmp_path: Path) -> None:
        put_extraction_result(
            program_id="p1", source_hash="abc", prompt_version="rev_extractor.v1",
            claims=[], programs_root=tmp_path, set_at_epoch=T0, now_epoch=T0,
        )
        assert get_extraction_result(
            program_id="p1", source_hash="zzz", prompt_version="rev_extractor.v1",
            programs_root=tmp_path, now_epoch=T0,
        ) is None

    def test_prompt_version_mismatch_is_a_miss(self, tmp_path: Path) -> None:
        put_extraction_result(
            program_id="p1", source_hash="abc", prompt_version="rev_extractor.v1",
            claims=[], programs_root=tmp_path, set_at_epoch=T0, now_epoch=T0,
        )
        # Different prompt version → different key fingerprint → miss.
        assert get_extraction_result(
            program_id="p1", source_hash="abc", prompt_version="rev_extractor.v2",
            programs_root=tmp_path, now_epoch=T0,
        ) is None

    def test_ttl_expiry_is_a_miss_and_prunes(self, tmp_path: Path) -> None:
        put_extraction_result(
            program_id="p1", source_hash="abc", prompt_version="rev_extractor.v1",
            claims=[], programs_root=tmp_path,
            set_at_epoch=T0 - (EXTRACTION_TTL_DAYS + 1) * _DAY, now_epoch=T0,
        )
        assert get_extraction_result(
            program_id="p1", source_hash="abc", prompt_version="rev_extractor.v1",
            programs_root=tmp_path, now_epoch=T0,
        ) is None
        # Expired entry was pruned on read.
        stats = cache_stats("p1", "extraction", tmp_path)
        assert stats.count == 0

    def test_corrupt_file_returns_none_never_raises(self, tmp_path: Path) -> None:
        put_extraction_result(
            program_id="p1", source_hash="abc", prompt_version="rev_extractor.v1",
            claims=[], programs_root=tmp_path, set_at_epoch=T0, now_epoch=T0,
        )
        # Corrupt the entry file.
        d = tmp_path / "p1" / "rev_cache" / "extraction"
        target = list(d.glob("*.json"))[0]
        target.write_text("{not valid json", encoding="utf-8")
        assert get_extraction_result(
            program_id="p1", source_hash="abc", prompt_version="rev_extractor.v1",
            programs_root=tmp_path, now_epoch=T0,
        ) is None  # no exception

    def test_stale_schema_entry_is_a_miss(self, tmp_path: Path) -> None:
        d = tmp_path / "p1" / "rev_cache" / "extraction"
        d.mkdir(parents=True)
        # Hand-write an entry with a wrong schema_version.
        from src.core.rev.rev_cache_store import _key_fingerprint, _entry_path
        path = _entry_path("p1", "extraction", ("abc", "rev_extractor.v1"), tmp_path)
        path.write_text(json.dumps({
            "schema_version": "rev_cache.v0",  # stale
            "kind": "extraction",
            "key": {}, "captured_at_epoch": T0, "accessed_at_epoch": T0,
            "payload": {"prompt_version": "rev_extractor.v1", "claims": []},
        }), encoding="utf-8")
        assert get_extraction_result(
            program_id="p1", source_hash="abc", prompt_version="rev_extractor.v1",
            programs_root=tmp_path, now_epoch=T0,
        ) is None

    def test_evict_stale_removes_expired_entries(self, tmp_path: Path) -> None:
        # Two entries: one fresh, one expired.
        put_extraction_result(
            program_id="p1", source_hash="fresh", prompt_version="rev_extractor.v1",
            claims=[], programs_root=tmp_path, set_at_epoch=T0, now_epoch=T0,
        )
        put_extraction_result(
            program_id="p1", source_hash="old", prompt_version="rev_extractor.v1",
            claims=[], programs_root=tmp_path,
            set_at_epoch=T0 - (EXTRACTION_TTL_DAYS + 5) * _DAY, now_epoch=T0 - (EXTRACTION_TTL_DAYS + 5) * _DAY,
        )
        removed = evict_stale("p1", "extraction", tmp_path, ttl_days=EXTRACTION_TTL_DAYS, now_epoch=T0)
        assert removed == 1
        assert get_extraction_result(
            program_id="p1", source_hash="fresh", prompt_version="rev_extractor.v1",
            programs_root=tmp_path, now_epoch=T0,
        ) is not None

    def test_lru_eviction_at_maxsize(self, tmp_path: Path) -> None:
        # Insert MAXSIZE+1 entries with strictly increasing accessed_at.
        # After the (MAXSIZE+1)-th put, put_cached auto-evicts the oldest-accessed.
        for i in range(EXTRACTION_MAXSIZE + 1):
            put_extraction_result(
                program_id="p1", source_hash=f"h{i:04d}", prompt_version="rev_extractor.v1",
                claims=[], programs_root=tmp_path, set_at_epoch=T0 + i, now_epoch=T0 + i,
            )
        stats = cache_stats("p1", "extraction", tmp_path)
        assert stats.count == EXTRACTION_MAXSIZE
        # The oldest-accessed entry (h0000) was evicted; the newest (last) survives.
        assert get_extraction_result(
            program_id="p1", source_hash="h0000", prompt_version="rev_extractor.v1",
            programs_root=tmp_path, now_epoch=T0 + EXTRACTION_MAXSIZE,
        ) is None
        assert get_extraction_result(
            program_id="p1", source_hash=f"h{EXTRACTION_MAXSIZE:04d}",
            prompt_version="rev_extractor.v1",
            programs_root=tmp_path, now_epoch=T0 + EXTRACTION_MAXSIZE,
        ) is not None

    def test_read_touches_accessed_at_saving_it_from_lru(self, tmp_path: Path) -> None:
        # Insert two old entries, then read the first so it becomes freshest.
        put_extraction_result(
            program_id="p1", source_hash="h0", prompt_version="rev_extractor.v1",
            claims=[], programs_root=tmp_path, set_at_epoch=T0, now_epoch=T0,
        )
        put_extraction_result(
            program_id="p1", source_hash="h1", prompt_version="rev_extractor.v1",
            claims=[], programs_root=tmp_path, set_at_epoch=T0 + 1, now_epoch=T0 + 1,
        )
        # Touch h0 at a later time → its accessed_at becomes the newest.
        get_extraction_result(
            program_id="p1", source_hash="h0", prompt_version="rev_extractor.v1",
            programs_root=tmp_path, now_epoch=T0 + 100,
        )
        # Force eviction of 1 (maxsize=1) → h1 (oldest accessed) is evicted, h0 kept.
        removed = evict_lru("p1", "extraction", tmp_path, maxsize=1, now_epoch=T0 + 100)
        assert removed == 1
        assert get_extraction_result(
            program_id="p1", source_hash="h0", prompt_version="rev_extractor.v1",
            programs_root=tmp_path, now_epoch=T0 + 100,
        ) is not None
        assert get_extraction_result(
            program_id="p1", source_hash="h1", prompt_version="rev_extractor.v1",
            programs_root=tmp_path, now_epoch=T0 + 100,
        ) is None

    def test_cache_stats_reports_count_and_bytes(self, tmp_path: Path) -> None:
        put_extraction_result(
            program_id="p1", source_hash="abc", prompt_version="rev_extractor.v1",
            claims=[{"event_type": "x"}], programs_root=tmp_path, set_at_epoch=T0, now_epoch=T0,
        )
        stats = cache_stats("p1", "extraction", tmp_path)
        assert stats.count == 1
        assert stats.total_bytes > 0
        assert stats.oldest_accessed_epoch is not None

    def test_clear_cache_removes_all(self, tmp_path: Path) -> None:
        for i in range(3):
            put_extraction_result(
                program_id="p1", source_hash=f"h{i}", prompt_version="rev_extractor.v1",
                claims=[], programs_root=tmp_path, set_at_epoch=T0, now_epoch=T0,
            )
        assert clear_cache("p1", "extraction", tmp_path) == 3
        assert cache_stats("p1", "extraction", tmp_path).count == 0

    def test_missing_dir_stats_is_empty(self, tmp_path: Path) -> None:
        stats = cache_stats("nope", "extraction", tmp_path)
        assert stats.count == 0 and stats.total_bytes == 0
        assert get_extraction_result(
            program_id="nope", source_hash="x", prompt_version="rev_extractor.v1",
            programs_root=tmp_path, now_epoch=T0,
        ) is None

    def test_oversize_payload_raises(self, tmp_path: Path) -> None:
        big = [{"event_type": "x", "payload": {"k": "v" * (MAX_ENTRY_BYTES + 1)}}]
        with pytest.raises(ValueError, match="exceeds"):
            put_extraction_result(
                program_id="p1", source_hash="abc", prompt_version="rev_extractor.v1",
                claims=big, programs_root=tmp_path, set_at_epoch=T0, now_epoch=T0,
            )


# ---------------------------------------------------------------------------
# Judge cache (P2-8) — store mechanics
# ---------------------------------------------------------------------------


class TestJudgeCacheStore:
    def test_put_then_get_is_a_hit(self, tmp_path: Path) -> None:
        verdict = {"extractor_a": {"precision": 0.9, "recall": 0.8, "scores": []}}
        gt_hash = hash_ground_truth(["fact1", "fact2"])
        put_judge_result(
            program_id="p1", source_document_key="msg-1", prompt_version="rev_judge.v1",
            ground_truth_hash=gt_hash, verdict=verdict, programs_root=tmp_path,
            set_at_epoch=T0, now_epoch=T0,
        )
        got = get_judge_result(
            program_id="p1", source_document_key="msg-1", prompt_version="rev_judge.v1",
            ground_truth_hash=gt_hash, programs_root=tmp_path, now_epoch=T0,
        )
        assert got == verdict

    def test_different_ground_truth_is_a_miss(self, tmp_path: Path) -> None:
        gt1 = hash_ground_truth(["fact1"])
        gt2 = hash_ground_truth(["fact2"])
        put_judge_result(
            program_id="p1", source_document_key="msg-1", prompt_version="rev_judge.v1",
            ground_truth_hash=gt1, verdict={"x": 1}, programs_root=tmp_path,
            set_at_epoch=T0, now_epoch=T0,
        )
        assert get_judge_result(
            program_id="p1", source_document_key="msg-1", prompt_version="rev_judge.v1",
            ground_truth_hash=gt2, programs_root=tmp_path, now_epoch=T0,
        ) is None

    def test_ground_truth_hash_is_order_invariant(self, tmp_path: Path) -> None:
        # Sorted inside hash_ground_truth → same set of facts ⇒ same hash.
        assert hash_ground_truth(["a", "b"]) == hash_ground_truth(["b", "a"])

    def test_ttl_expiry_is_a_miss(self, tmp_path: Path) -> None:
        gt_hash = hash_ground_truth(["fact1"])
        put_judge_result(
            program_id="p1", source_document_key="msg-1", prompt_version="rev_judge.v1",
            ground_truth_hash=gt_hash, verdict={"x": 1}, programs_root=tmp_path,
            set_at_epoch=T0 - (JUDGE_TTL_DAYS + 1) * _DAY, now_epoch=T0,
        )
        assert get_judge_result(
            program_id="p1", source_document_key="msg-1", prompt_version="rev_judge.v1",
            ground_truth_hash=gt_hash, programs_root=tmp_path, now_epoch=T0,
        ) is None


# ---------------------------------------------------------------------------
# Extraction cache wiring into LLMRevExtractor (P2-12)
# ---------------------------------------------------------------------------

StructuredResponse = TypeVar("StructuredResponse")


def _hydrated(text: str, *, subject: str = "s", program_id: str = "nova"):
    from src.core.rev.identity import CanonicalItemIdentity
    from src.core.rev.entity_types import EntityType
    from src.core.rev.normalizer import chunk_canonical
    from src.core.rev.ports import HydratedContent
    identity = CanonicalItemIdentity(
        source_type=EntityType.MESSAGE, tenant_id="t", principal_mailbox="u@x.com",
        container="inbox", resource_id="msg-1",
    )
    return HydratedContent(
        identity=identity, canonical_text=text,
        normalized_source_hash="sha256:" + text.encode("utf-8").hex()[:64],
        chunks=tuple(chunk_canonical(text)), route_metadata={"subject": subject, "program_id": program_id},
        metadata_only=False,
    )


class _CountingClient:
    """FakeClient that records calls and returns a canned grounded payload."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls = 0

    def chat(self, system: str, user: str, **kwargs: Any) -> str:
        return ""

    def structured(
        self, system: str, user: str, *,
        parser: Callable[[dict[str, Any]], StructuredResponse],
        max_tokens: int = 800, prompt_version: str | None = None,
    ) -> StructuredResponse:
        self.calls += 1
        return parser(self._payload)


class TestExtractionCacheWiring:
    def _payload(self, canonical: str) -> dict[str, Any]:
        excerpt = "deployment completed"
        return {"events": [{
            "event_type": "deployment.completed",
            "payload": {"date": "2026-06-20"},
            "excerpt": excerpt,
            "extraction_confidence": 0.9,
        }]}

    def test_second_call_is_a_cache_hit_no_llm_call(self, tmp_path: Path) -> None:
        from src.ai.rev.extractor import LLMRevExtractor
        canonical = "The deployment completed on 2026-06-20."
        client = _CountingClient(self._payload(canonical))
        ext = LLMRevExtractor(
            client=client, cache_program_id="p1", cache_programs_root=tmp_path, use_cache=True,
        )
        h = _hydrated(canonical)
        ext.extract(h, correlation_id="c1")
        assert client.calls == 1
        assert ext.cache_misses == 1 and ext.cache_hits == 0
        ext.extract(h, correlation_id="c2")
        assert client.calls == 1  # no second LLM call
        assert ext.cache_hits == 1

    def test_cache_disabled_calls_llm_every_time(self, tmp_path: Path) -> None:
        from src.ai.rev.extractor import LLMRevExtractor
        canonical = "The deployment completed on 2026-06-20."
        client = _CountingClient(self._payload(canonical))
        ext = LLMRevExtractor(client=client)  # use_cache defaults False
        h = _hydrated(canonical)
        ext.extract(h, correlation_id="c1")
        ext.extract(h, correlation_id="c2")
        assert client.calls == 2
        assert ext.cache_hits == 0

    def test_different_canonical_text_is_a_cache_miss(self, tmp_path: Path) -> None:
        from src.ai.rev.extractor import LLMRevExtractor
        a = "The deployment completed on 2026-06-20."
        b = "The deployment completed on 2026-06-21 cleanly."
        client = _CountingClient(self._payload(a))
        ext = LLMRevExtractor(
            client=client, cache_program_id="p1", cache_programs_root=tmp_path, use_cache=True,
        )
        ext.extract(_hydrated(a), correlation_id="c1")
        # Same client returns a (grounded-in-a) payload; for b the excerpt must
        # still be a substring of b for grounding to pass — it is.
        ext.extract(_hydrated(b), correlation_id="c2")
        assert client.calls == 2  # different source_hash → miss → LLM called
        assert ext.cache_misses == 2

    def test_cached_claims_roundtrip_to_extractedclaim(self, tmp_path: Path) -> None:
        from src.ai.rev.extractor import LLMRevExtractor
        canonical = "The deployment completed on 2026-06-20."
        client = _CountingClient(self._payload(canonical))
        ext = LLMRevExtractor(
            client=client, cache_program_id="p1", cache_programs_root=tmp_path, use_cache=True,
        )
        first = ext.extract(_hydrated(canonical), correlation_id="c1").value
        second = ext.extract(_hydrated(canonical), correlation_id="c2").value
        # Same event types present on both the LLM and the cached path.
        assert {c.event_type for c in first} == {c.event_type for c in second}
        assert any(c.event_type == "deployment.completed" for c in second)


# ---------------------------------------------------------------------------
# Judge cache wiring into judge_extractions (P2-8)
# ---------------------------------------------------------------------------


class _JudgeCountingClient:
    def __init__(self, verdict: dict[str, Any]) -> None:
        self._verdict = verdict
        self.calls = 0

    def chat(self, system: str, user: str, **kwargs: Any) -> str:
        return ""

    def structured(
        self, system: str, user: str, *,
        parser: Callable[[dict[str, Any]], StructuredResponse],
        max_tokens: int = 800, prompt_version: str | None = None,
    ) -> StructuredResponse:
        self.calls += 1
        return parser(self._verdict)


class TestJudgeCacheWiring:
    def _verdict(self) -> dict[str, Any]:
        return {
            "extractor_a": {"precision": 0.9, "recall": 0.8, "scores": [
                {"event_type": "deployment.completed", "verdict": "CORRECT", "reason": "ok"}]},
            "extractor_b": {"precision": 0.7, "recall": 0.6, "scores": []},
            "ground_truth_coverage": [{"fact": "deploy done", "captured_by": "A"}],
            "summary": "A wins",
        }

    def _args(self, tmp_path: Path, gt: dict[str, list[str]]):
        from src.ai.rev.extractor import EvidenceSpan, ExtractedClaim
        claim = ExtractedClaim(
            event_type="deployment.completed", payload={"date": "2026-06-20"},
            evidence_spans=(EvidenceSpan("c0", 0, 21, "deployment completed"),),
            extraction_confidence=0.9, extraction_model="rev.llm.frontier.v1",
        )
        return dict(
            messages=[{"message_id": "m1", "subject": "s"}],
            extractor_a_name="det", extractor_a_claims={"m1": (claim,)},
            extractor_b_name="llm", extractor_b_claims={"m1": ()},
            canonical_texts={"m1": "The deployment completed on 2026-06-20."},
            ground_truth=gt,
        )

    def test_second_run_is_all_cache_hits(self, tmp_path: Path) -> None:
        from src.ai.rev.judge import judge_extractions
        client = _JudgeCountingClient(self._verdict())
        args = self._args(tmp_path, {"m1": ["deploy done"]})
        r1 = judge_extractions(client=client, use_cache=True,
                               cache_program_id="p1", cache_programs_root=tmp_path, **args)
        assert client.calls == 1
        assert r1.cache_hits == 0
        r2 = judge_extractions(client=client, use_cache=True,
                               cache_program_id="p1", cache_programs_root=tmp_path, **args)
        assert client.calls == 1  # no new LLM call
        assert r2.cache_hits == 1

    def test_changed_ground_truth_invalidates_cache(self, tmp_path: Path) -> None:
        from src.ai.rev.judge import judge_extractions
        client = _JudgeCountingClient(self._verdict())
        args = self._args(tmp_path, {"m1": ["deploy done"]})
        judge_extractions(client=client, use_cache=True,
                          cache_program_id="p1", cache_programs_root=tmp_path, **args)
        # Different ground-truth facts → different gt_hash → miss.
        args2 = self._args(tmp_path, {"m1": ["deploy done", "extra fact"]})
        r2 = judge_extractions(client=client, use_cache=True,
                               cache_program_id="p1", cache_programs_root=tmp_path, **args2)
        assert client.calls == 2
        assert r2.cache_hits == 0

    def test_cache_disabled_calls_llm_every_run(self, tmp_path: Path) -> None:
        from src.ai.rev.judge import judge_extractions
        client = _JudgeCountingClient(self._verdict())
        args = self._args(tmp_path, {"m1": ["deploy done"]})
        judge_extractions(client=client, **args)  # use_cache defaults False
        judge_extractions(client=client, **args)
        assert client.calls == 2