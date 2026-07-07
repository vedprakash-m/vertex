"""WI-2.2 / WI-2.3: Tests for signal normalizer and entity.alias emission."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest

from src.core.signal_normalizer import (
    backfill_entity_refs,
    normalize_signal,
    normalize_signals,
)
from src.core.models_v2 import Signal


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_signal(
    sig_id: str = "s1",
    source: str = "ado",
    timestamp: datetime | None = None,
    entity_refs: tuple[str, ...] = (),
    raw_ref: str = "",
    metadata: dict | None = None,
) -> Signal:
    return Signal(
        id=sig_id,
        timestamp=timestamp or datetime(2024, 1, 1, 12, 0, 0),
        source=source,
        program_id="testprog",
        workstream_id=None,
        entity_refs=entity_refs,
        text="some signal text",
        raw_ref=raw_ref,
        confidence=0.9,
        metadata=metadata or {},
        thread_id=None,
        review_policy=None,
    )


# ---------------------------------------------------------------------------
# UTC normalization
# ---------------------------------------------------------------------------

class TestUTCNormalization:
    def test_naive_datetime_gets_utc(self):
        sig = _make_signal(timestamp=datetime(2024, 6, 1, 10, 0, 0))  # naive
        normalized = normalize_signal(sig)
        assert normalized.timestamp.tzinfo is not None
        assert normalized.timestamp.tzinfo == timezone.utc

    def test_utc_datetime_preserved(self):
        utc_ts = datetime(2024, 6, 1, 10, 0, 0, tzinfo=timezone.utc)
        sig = _make_signal(timestamp=utc_ts)
        normalized = normalize_signal(sig)
        assert normalized.timestamp == utc_ts

    def test_aware_datetime_converted_to_utc(self):
        # UTC+5 timestamp should be converted to UTC
        pst_offset = timezone(timedelta(hours=-8))
        ts = datetime(2024, 6, 1, 10, 0, 0, tzinfo=pst_offset)
        sig = _make_signal(timestamp=ts)
        normalized = normalize_signal(sig)
        assert normalized.timestamp.tzinfo == timezone.utc
        assert normalized.timestamp.hour == 18  # 10:00 PST = 18:00 UTC


# ---------------------------------------------------------------------------
# Prepend-preserve (metadata normalization flag)
# ---------------------------------------------------------------------------

class TestPrependPreserve:
    def test_normalized_flag_added_to_metadata(self):
        sig = _make_signal(metadata={"custom_key": "custom_val"})
        normalized = normalize_signal(sig)
        assert normalized.metadata["normalized"] is True

    def test_existing_metadata_preserved(self):
        sig = _make_signal(metadata={"my_key": "my_value"})
        normalized = normalize_signal(sig)
        assert normalized.metadata["my_key"] == "my_value"
        assert normalized.metadata["normalized"] is True

    def test_text_content_preserved(self):
        sig = _make_signal()
        normalized = normalize_signal(sig)
        assert normalized.text == sig.text


# ---------------------------------------------------------------------------
# Idempotence
# ---------------------------------------------------------------------------

class TestIdempotence:
    def test_normalize_twice_same_result(self):
        sig = _make_signal()
        once = normalize_signal(sig)
        twice = normalize_signal(once)
        assert once == twice

    def test_already_normalized_returns_same_object(self):
        sig = _make_signal(metadata={"normalized": True})
        result = normalize_signal(sig)
        assert result is sig  # same object, no copy


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_dedup_by_source_and_id(self):
        s1 = _make_signal("s1", "ado")
        s2 = _make_signal("s1", "ado")  # duplicate
        s3 = _make_signal("s2", "ado")
        result = normalize_signals([s1, s2, s3], deduplicate=True)
        assert len(result) == 2
        ids = [s.id for s in result]
        assert ids == ["s1", "s2"]

    def test_dedup_first_seen_wins(self):
        ts1 = datetime(2024, 1, 1, tzinfo=timezone.utc)
        ts2 = datetime(2024, 1, 2, tzinfo=timezone.utc)
        s1 = _make_signal("s1", "ado", timestamp=ts1)
        s2 = _make_signal("s1", "ado", timestamp=ts2)  # same id, later timestamp
        result = normalize_signals([s1, s2])
        assert len(result) == 1
        assert result[0].timestamp.replace(tzinfo=timezone.utc) == ts1  # first-seen

    def test_no_dedup_when_disabled(self):
        s1 = _make_signal("s1", "ado")
        s2 = _make_signal("s1", "ado")
        result = normalize_signals([s1, s2], deduplicate=False)
        assert len(result) == 2

    def test_different_sources_not_deduped(self):
        s1 = _make_signal("s1", "ado")
        s2 = _make_signal("s1", "teams")  # same id, different source
        result = normalize_signals([s1, s2], deduplicate=True)
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Backfill entity_refs
# ---------------------------------------------------------------------------

class TestBackfillEntityRefs:
    def test_backfill_from_raw_ref(self):
        from src.core.program_reality import CanonicalEntity
        from src.core.entity_registry import EntityRegistry

        entity = CanonicalEntity(
            entity_id="p1",
            entity_type="person",
            canonical_name="Alice Smith",
            aliases=("asmith",),
            scope="program",
        )
        registry = EntityRegistry(program_entities=(entity,), org_entities=())
        sig = _make_signal(raw_ref="Alice Smith", entity_refs=())
        result = backfill_entity_refs(sig, registry)
        assert "p1" in result.entity_refs

    def test_backfill_idempotent(self):
        from src.core.program_reality import CanonicalEntity
        from src.core.entity_registry import EntityRegistry

        entity = CanonicalEntity(
            entity_id="p1",
            entity_type="person",
            canonical_name="Alice Smith",
            aliases=(),
            scope="program",
        )
        registry = EntityRegistry(program_entities=(entity,), org_entities=())
        sig = _make_signal(raw_ref="Alice Smith", entity_refs=())
        once = backfill_entity_refs(sig, registry)
        twice = backfill_entity_refs(once, registry)
        assert once.entity_refs == twice.entity_refs

    def test_backfill_no_match_preserves_original(self):
        from src.core.entity_registry import EntityRegistry

        registry = EntityRegistry(program_entities=(), org_entities=())
        sig = _make_signal(raw_ref="Unknown Person", entity_refs=("existing-ref",))
        result = backfill_entity_refs(sig, registry)
        assert result.entity_refs == ("existing-ref",)

    def test_backfill_no_raw_ref_preserves_entity_refs(self):
        from src.core.entity_registry import EntityRegistry

        registry = EntityRegistry(program_entities=(), org_entities=())
        sig = _make_signal(raw_ref="", entity_refs=("existing-ref",))
        result = backfill_entity_refs(sig, registry)
        assert result.entity_refs == ("existing-ref",)


# ---------------------------------------------------------------------------
# Batch normalization
# ---------------------------------------------------------------------------

def test_normalize_signals_all_utc():
    signals = [
        _make_signal("s1", timestamp=datetime(2024, 1, 1, 10, 0, 0)),
        _make_signal("s2", timestamp=datetime(2024, 1, 2, 11, 0, 0)),
    ]
    result = normalize_signals(signals)
    for s in result:
        assert s.timestamp.tzinfo is not None
        assert s.metadata["normalized"] is True


def test_normalize_signals_empty():
    result = normalize_signals([])
    assert result == ()
