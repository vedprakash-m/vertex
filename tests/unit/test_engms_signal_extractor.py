from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from src.core.engms_signal_extractor import EngMsSignalExtractor, hashes_from_artifacts
from src.core.models import Confidence, RiskLevel, WorkItem


def _item(
    item_id: int = 1,
    description: str = "",
    field_key: str = "System.Description",
) -> WorkItem:
    return WorkItem(
        id=item_id,
        type="Feature",
        title="Test Item",
        state="Active",
        assigned_to=None,
        assigned_to_email=None,
        area_path="One\\Demo",
        iteration_path="One\\Demo\\Sprint",
        target_date=None,
        risk_level=RiskLevel.UNKNOWN,
        tags=[],
        custom_fields={field_key: description} if description else {},
        fetched_at=datetime(2026, 5, 29, tzinfo=timezone.utc),
    )


# ---------------------------------------------------------------------------
# No eng.ms URLs
# ---------------------------------------------------------------------------

def test_no_signals_when_no_description() -> None:
    result = EngMsSignalExtractor().extract([_item()], "prog-1")
    assert result.signals == ()
    assert result.side_artifacts == {}


def test_no_signals_when_description_has_no_engms_url() -> None:
    item = _item(description="See https://example.com/doc for details.")
    result = EngMsSignalExtractor().extract([item], "prog-1")
    assert result.signals == ()


# ---------------------------------------------------------------------------
# URL extraction
# ---------------------------------------------------------------------------

def test_signal_emitted_for_new_engms_url() -> None:
    url = "https://eng.ms/docs/acme/spec"
    item = _item(description=f"Refer to {url} for the spec.")
    with patch("src.core.engms_signal_extractor.fetch_engms_page_summary", return_value="Page summary text") as mock_fetch:
        result = EngMsSignalExtractor().extract([item], "prog-1")

    mock_fetch.assert_called_once_with(url)
    assert len(result.signals) == 1
    sig = result.signals[0]
    assert sig.source == "engms"
    assert sig.confidence == Confidence.LOW
    assert sig.program_id == "prog-1"
    assert "new reference" in sig.text
    assert sig.raw_ref == url
    assert "ado:1" in sig.entity_refs
    assert "WI:1" in sig.entity_refs


def test_signal_emitted_for_changed_page() -> None:
    url = "https://eng.ms/docs/acme/design"
    item = _item(description=f"Design: {url}")
    old_hash = "aaaa1111bbbb2222"  # different from what fetch returns
    with patch("src.core.engms_signal_extractor.fetch_engms_page_summary", return_value="Updated content"):
        result = EngMsSignalExtractor().extract([item], "prog-1", previous_hashes={url: old_hash})

    assert len(result.signals) == 1
    sig = result.signals[0]
    assert "updated" in sig.text
    assert sig.metadata is not None
    assert sig.metadata["changed"] is True


def test_no_signal_when_page_unchanged() -> None:
    url = "https://eng.ms/docs/acme/stable"
    item = _item(description=f"See {url}")
    content = "Stable page content"
    with patch("src.core.engms_signal_extractor.fetch_engms_page_summary", return_value=content) as mock_fetch:
        # First run — capture hash
        first = EngMsSignalExtractor().extract([item], "prog-1")
        prev = hashes_from_artifacts(first.side_artifacts)
        # Second run — same content
        second = EngMsSignalExtractor().extract([item], "prog-1", previous_hashes=prev)

    assert len(first.signals) == 1   # new reference
    assert len(second.signals) == 0  # unchanged


def test_no_signal_when_fetch_returns_none() -> None:
    url = "https://eng.ms/docs/private"
    item = _item(description=f"Internal: {url}")
    with patch("src.core.engms_signal_extractor.fetch_engms_page_summary", return_value=None):
        result = EngMsSignalExtractor().extract([item], "prog-1")
    assert result.signals == ()
    assert result.side_artifacts == {}


# ---------------------------------------------------------------------------
# URL deduplication across items
# ---------------------------------------------------------------------------

def test_multiple_items_same_url_deduplicated() -> None:
    url = "https://eng.ms/docs/shared"
    items = [
        _item(item_id=10, description=f"Ref: {url}"),
        _item(item_id=20, description=f"Also see: {url}"),
    ]
    with patch("src.core.engms_signal_extractor.fetch_engms_page_summary", return_value="Shared page"):
        result = EngMsSignalExtractor().extract(items, "prog-1")

    # URL is unique → one signal
    assert len(result.signals) == 1
    sig = result.signals[0]
    assert "ado:10" in sig.entity_refs
    assert "ado:20" in sig.entity_refs
    assert "WI:10" in sig.entity_refs
    assert "WI:20" in sig.entity_refs


def test_two_distinct_urls_produce_two_signals() -> None:
    url_a = "https://eng.ms/docs/a"
    url_b = "https://eng.ms/docs/b"
    items = [
        _item(item_id=1, description=f"A: {url_a}"),
        _item(item_id=2, description=f"B: {url_b}"),
    ]
    summaries = {url_a: "Content A", url_b: "Content B"}
    with patch("src.core.engms_signal_extractor.fetch_engms_page_summary", side_effect=lambda u: summaries.get(u)):
        result = EngMsSignalExtractor().extract(items, "prog-1")
    assert len(result.signals) == 2


# ---------------------------------------------------------------------------
# Trailing punctuation stripped from URLs
# ---------------------------------------------------------------------------

def test_trailing_punctuation_stripped() -> None:
    url = "https://eng.ms/docs/spec"
    item = _item(description=f"See {url}.")
    with patch("src.core.engms_signal_extractor.fetch_engms_page_summary", return_value="Content") as mock_fetch:
        EngMsSignalExtractor().extract([item], "prog-1")
    mock_fetch.assert_called_once_with(url)  # no trailing dot


# ---------------------------------------------------------------------------
# alternate description field key
# ---------------------------------------------------------------------------

def test_description_field_alias_used() -> None:
    url = "https://eng.ms/docs/alt"
    item = _item(description=f"Alt: {url}", field_key="description")
    with patch("src.core.engms_signal_extractor.fetch_engms_page_summary", return_value="Alt content") as mock_fetch:
        result = EngMsSignalExtractor().extract([item], "prog-1")
    mock_fetch.assert_called_once_with(url)
    assert len(result.signals) == 1


# ---------------------------------------------------------------------------
# side_artifacts / hashes_from_artifacts round-trip
# ---------------------------------------------------------------------------

def test_side_artifacts_contain_current_hash() -> None:
    url = "https://eng.ms/docs/hash-check"
    item = _item(description=f"See {url}")
    with patch("src.core.engms_signal_extractor.fetch_engms_page_summary", return_value="Content"):
        result = EngMsSignalExtractor().extract([item], "prog-1")

    assert any("engms_hash:" in k for k in result.side_artifacts)


def test_hashes_from_artifacts_round_trip() -> None:
    url = "https://eng.ms/docs/roundtrip"
    item = _item(description=f"See {url}")
    with patch("src.core.engms_signal_extractor.fetch_engms_page_summary", return_value="Round-trip content"):
        result = EngMsSignalExtractor().extract([item], "prog-1")

    recovered = hashes_from_artifacts(result.side_artifacts)
    assert url in recovered
    assert len(recovered[url]) == 16  # short hash length


def test_hashes_from_artifacts_ignores_non_hash_keys() -> None:
    artifacts: dict[str, str | int | float | bool | None] = {
        "engms_hash:https://eng.ms/a": "abc123",
        "other_key": "value",
        "engms_hash:https://eng.ms/b": None,  # None value ignored
    }
    result = hashes_from_artifacts(artifacts)
    assert "https://eng.ms/a" in result
    assert "other_key" not in result
    assert "https://eng.ms/b" not in result  # None filtered


# ---------------------------------------------------------------------------
# channel and metadata
# ---------------------------------------------------------------------------

def test_extraction_result_channel() -> None:
    result = EngMsSignalExtractor().extract([], "prog-1")
    assert result.channel == "engms"
    assert result.trajectory_points == ()
    assert result.errors == ()


def test_signal_metadata_fields() -> None:
    url = "https://eng.ms/docs/meta"
    item = _item(description=f"See {url}")
    with patch("src.core.engms_signal_extractor.fetch_engms_page_summary", return_value="Meta content"):
        result = EngMsSignalExtractor().extract([item], "prog-1")

    sig = result.signals[0]
    assert sig.metadata is not None
    assert sig.metadata["url"] == url
    assert "hash" in sig.metadata
    assert sig.metadata["changed"] is False  # new reference, not changed
