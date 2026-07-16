"""ADF-W1.5 (Appendix A.7): prefetch snapshot store.

Covers the two properties the brief names explicitly: a partial (in-flight
or interrupted) snapshot is invisible to readers, and expiry is honored --
plus the payload-then-manifest commit ordering and the "latest" pointer.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.core.prefetch_store import (
    PrefetchSnapshotManifest,
    compute_snapshot_id,
    read_latest_committed_snapshot,
    read_snapshot_payload,
    read_unexpired_committed_snapshot,
    write_prefetch_snapshot,
)

_NOW = datetime(2026, 7, 12, 12, 0, tzinfo=timezone.utc)


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    manifest = write_prefetch_snapshot(
        program_id="fixture_prog",
        channel="workiq",
        payload={"threads": ["a", "b"]},
        watermark="2026-07-01T00:00:00+00:00",
        completeness="complete",
        latency_ms=1234.5,
        ttl_seconds=3600,
        source_identities=("thread:1", "thread:2"),
        programs_root=programs_root,
        now=_NOW,
    )

    read_back = read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=programs_root)
    assert read_back is not None
    assert read_back.snapshot_id == manifest.snapshot_id
    assert read_back.completeness == "complete"
    assert read_back.watermark == "2026-07-01T00:00:00+00:00"

    payload = read_snapshot_payload("fixture_prog", "workiq", read_back, programs_root=programs_root)
    assert payload == {"threads": ["a", "b"]}


def test_snapshot_id_is_content_addressed(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    payload = {"a": 1}
    manifest = write_prefetch_snapshot(
        program_id="fixture_prog",
        channel="workiq",
        payload=payload,
        watermark=None,
        completeness="complete",
        latency_ms=1.0,
        ttl_seconds=3600,
        programs_root=programs_root,
        now=_NOW,
    )
    expected_id = compute_snapshot_id(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    assert manifest.snapshot_id == expected_id


def test_directory_without_manifest_is_invisible_to_readers(tmp_path: Path) -> None:
    """A snapshot whose payload was written but whose manifest.json commit
    marker was never written (interrupted mid-write) must never be visible."""
    programs_root = tmp_path / "programs"
    snapshot_dir = programs_root / "fixture_prog" / "runtime" / "prefetch" / "workiq" / "deadbeef"
    snapshot_dir.mkdir(parents=True)
    (snapshot_dir / "payload.json").write_text('{"partial": true}', encoding="utf-8")
    # No manifest.json written -- simulates an interrupted commit.

    # Even with a (maliciously or accidentally) hand-written pointer at the
    # in-flight snapshot, the absent manifest makes it invisible.
    pointer_path = programs_root / "fixture_prog" / "runtime" / "prefetch" / "workiq" / "latest.json"
    pointer_path.write_text(json.dumps({"snapshot_id": "deadbeef"}), encoding="utf-8")

    assert read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=programs_root) is None
    assert read_unexpired_committed_snapshot("fixture_prog", "workiq", programs_root=programs_root) is None


def test_no_snapshot_at_all_is_none_not_an_error(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    assert read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=programs_root) is None
    assert read_unexpired_committed_snapshot("fixture_prog", "workiq", programs_root=programs_root) is None


def test_expired_snapshot_is_invisible_via_read_unexpired_but_visible_via_read_latest(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    write_prefetch_snapshot(
        program_id="fixture_prog",
        channel="workiq",
        payload={"a": 1},
        watermark=None,
        completeness="complete",
        latency_ms=1.0,
        ttl_seconds=60,  # expires 60s after `now`
        programs_root=programs_root,
        now=_NOW,
    )
    past_expiry = _NOW + timedelta(seconds=120)

    # The bounded, non-blocking read a consumer should use: expired -> None.
    assert read_unexpired_committed_snapshot("fixture_prog", "workiq", programs_root=programs_root, now=past_expiry) is None
    # The raw "what's committed" read still finds it (e.g. for staleness diagnostics).
    raw = read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=programs_root)
    assert raw is not None
    assert raw.is_expired(at=past_expiry) is True
    assert raw.is_expired(at=_NOW) is False


def test_second_write_updates_the_latest_pointer(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    first = write_prefetch_snapshot(
        program_id="fixture_prog",
        channel="workiq",
        payload={"v": 1},
        watermark=None,
        completeness="complete",
        latency_ms=1.0,
        ttl_seconds=3600,
        programs_root=programs_root,
        now=_NOW,
    )
    second = write_prefetch_snapshot(
        program_id="fixture_prog",
        channel="workiq",
        payload={"v": 2},
        watermark=None,
        completeness="complete",
        latency_ms=1.0,
        ttl_seconds=3600,
        programs_root=programs_root,
        now=_NOW + timedelta(minutes=5),
    )
    assert first.snapshot_id != second.snapshot_id

    current = read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=programs_root)
    assert current is not None
    assert current.snapshot_id == second.snapshot_id


def test_channels_and_programs_are_isolated(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    write_prefetch_snapshot(
        program_id="fixture_prog",
        channel="workiq",
        payload={"v": 1},
        watermark=None,
        completeness="complete",
        latency_ms=1.0,
        ttl_seconds=3600,
        programs_root=programs_root,
        now=_NOW,
    )
    assert read_latest_committed_snapshot("fixture_prog", "kusto", programs_root=programs_root) is None
    assert read_latest_committed_snapshot("other_prog", "workiq", programs_root=programs_root) is None


def test_partial_completeness_state_is_recorded_not_rejected(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    manifest = write_prefetch_snapshot(
        program_id="fixture_prog",
        channel="workiq",
        payload={"threads": []},
        watermark=None,
        completeness="partial",
        latency_ms=90000.0,
        ttl_seconds=3600,
        programs_root=programs_root,
        now=_NOW,
    )
    assert manifest.completeness == "partial"
    read_back = read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=programs_root)
    assert read_back is not None
    assert read_back.completeness == "partial"


def test_invalid_completeness_state_raises() -> None:
    with pytest.raises(ValueError, match="completeness"):
        write_prefetch_snapshot(
            program_id="fixture_prog",
            channel="workiq",
            payload={},
            watermark=None,
            completeness="not-a-real-state",
            latency_ms=1.0,
            ttl_seconds=3600,
        )


def test_manifest_serialization_round_trip() -> None:
    manifest = PrefetchSnapshotManifest(
        schema_version="1",
        program_id="fixture_prog",
        channel="workiq",
        snapshot_id="abc123",
        created_at=_NOW,
        expires_at=_NOW + timedelta(hours=1),
        watermark="wm-1",
        completeness="complete",
        latency_ms=42.0,
        source_identities=("thread:1",),
        payload_path="payload.json",
    )
    round_tripped = PrefetchSnapshotManifest.from_dict(manifest.to_dict())
    assert round_tripped == manifest
