"""ADF-W1.5 (Appendix A.7): prefetch snapshot store.

Covers the two properties the brief names explicitly: a partial (in-flight
or interrupted) snapshot is invisible to readers, and expiry is honored --
plus the payload-then-manifest commit ordering and the "latest" pointer.
"""

from __future__ import annotations

import json
import sys
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


@pytest.mark.parametrize("target", ["pointer", "manifest"])
@pytest.mark.parametrize(
    "metadata_bytes",
    [
        b'{"snapshot_id":"Synthetic hidden value"\xff}',
        b'{"snapshot_id":' + b"9" * 5000 + b"}",
        b'{"snapshot_id":' + b"[" * (sys.getrecursionlimit() + 100) + b"]" * (sys.getrecursionlimit() + 100) + b"}",
        b'{"snapshot_id":"../outside"}',
        b'{"snapshot_id":[]}',
        b"{",
        b"{}",
        b"[]",
    ],
    ids=["invalid-utf8", "oversized-integer", "deep-nesting", "path-shaped", "non-string", "invalid-json", "missing-id", "non-object"],
)
def test_unreadable_selection_metadata_is_not_a_usable_snapshot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, target: str, metadata_bytes: bytes,
) -> None:
    manifest = write_prefetch_snapshot(
        program_id="fixture_prog", channel="workiq", payload={"threads": ["a"]},
        watermark=None, completeness="complete", latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=_NOW,
    )
    channel_dir = tmp_path / "fixture_prog" / "runtime" / "prefetch" / "workiq"
    snapshot_dir = channel_dir / manifest.snapshot_id
    pointer_path = channel_dir / "latest.json"
    manifest_path = snapshot_dir / "manifest.json"
    selected_path = pointer_path if target == "pointer" else manifest_path
    untouched_path = manifest_path if target == "pointer" else pointer_path
    untouched_before = untouched_path.read_bytes()
    payload_before = (snapshot_dir / "payload.json").read_bytes()
    selected_path.write_bytes(metadata_bytes)

    original_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        assert read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=tmp_path) is None
        assert read_unexpired_committed_snapshot("fixture_prog", "workiq", programs_root=tmp_path, now=_NOW) is None
    finally:
        sys.set_int_max_str_digits(original_limit)

    assert f"Prefetch snapshot {target} is unreadable or invalid." in caplog.text
    assert "Synthetic hidden value" not in caplog.text
    assert str(tmp_path) not in caplog.text
    assert manifest.snapshot_id not in caplog.text
    assert selected_path.read_bytes() == metadata_bytes
    assert untouched_path.read_bytes() == untouched_before
    assert (snapshot_dir / "payload.json").read_bytes() == payload_before


@pytest.mark.parametrize("target", ["pointer", "manifest"])
def test_selection_metadata_read_error_warns_without_exposing_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, target: str,
) -> None:
    manifest = write_prefetch_snapshot(
        program_id="fixture_prog", channel="workiq", payload={"threads": []},
        watermark=None, completeness="complete", latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=_NOW,
    )
    channel_dir = tmp_path / "fixture_prog" / "runtime" / "prefetch" / "workiq"
    selected_path = channel_dir / "latest.json" if target == "pointer" else channel_dir / manifest.snapshot_id / "manifest.json"
    selected_before = selected_path.read_bytes()
    read_text = Path.read_text

    def fail_selected(path: Path, *args: object, **kwargs: object) -> str:
        if path == selected_path:
            raise PermissionError("Synthetic hidden read detail")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_selected)
    assert read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=tmp_path) is None
    assert f"Prefetch snapshot {target} is unreadable or invalid." in caplog.text
    assert "Synthetic hidden read detail" not in caplog.text
    assert str(tmp_path) not in caplog.text
    assert selected_path.read_bytes() == selected_before


@pytest.mark.parametrize("target", ["pointer", "manifest"])
def test_unexpected_selection_reader_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    manifest = write_prefetch_snapshot(
        program_id="fixture_prog", channel="workiq", payload={"threads": []},
        watermark=None, completeness="complete", latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=_NOW,
    )
    channel_dir = tmp_path / "fixture_prog" / "runtime" / "prefetch" / "workiq"
    selected_path = channel_dir / "latest.json" if target == "pointer" else channel_dir / manifest.snapshot_id / "manifest.json"
    read_text = Path.read_text

    def fail_selected(path: Path, *args: object, **kwargs: object) -> str:
        if path == selected_path:
            raise RuntimeError("Unexpected synthetic failure")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_selected)
    with pytest.raises(RuntimeError, match="Unexpected synthetic failure"):
        read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=tmp_path)


@pytest.mark.parametrize(
    "replacement",
    [b'{"threads":["Private changed payload"]}', b'{"threads":[]}', b'{"threads":["a","b"]} ', b"{", b"\xff"],
    ids=["changed-record", "empty", "whitespace", "invalid-json", "invalid-utf8"],
)
def test_read_snapshot_rejects_mismatched_content_address(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, replacement: bytes,
) -> None:
    manifest = write_prefetch_snapshot(
        program_id="fixture_prog", channel="workiq", payload={"threads": ["a", "b"]},
        watermark=None, completeness="complete", latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=_NOW,
    )
    snapshot_dir = tmp_path / "fixture_prog" / "runtime" / "prefetch" / "workiq" / manifest.snapshot_id
    payload_path = snapshot_dir / manifest.payload_path
    manifest_bytes = (snapshot_dir / "manifest.json").read_bytes()
    payload_path.write_bytes(replacement)

    with pytest.raises(ValueError, match="payload does not match its content address"):
        read_snapshot_payload("fixture_prog", "workiq", manifest, programs_root=tmp_path)

    assert "Prefetch snapshot payload does not match its content address" in caplog.text
    assert "Private changed payload" not in caplog.text
    assert manifest.snapshot_id not in caplog.text
    assert payload_path.read_bytes() == replacement
    assert (snapshot_dir / "manifest.json").read_bytes() == manifest_bytes


@pytest.mark.parametrize("target_committed", [False, True], ids=["uncommitted", "committed"])
def test_latest_rejects_manifest_pointing_to_a_different_snapshot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, target_committed: bool,
) -> None:
    channel_dir = tmp_path / "fixture_prog" / "runtime" / "prefetch" / "workiq"
    if target_committed:
        target = write_prefetch_snapshot(
            program_id="fixture_prog", channel="workiq", payload={"version": "target"},
            watermark=None, completeness="complete", latency_ms=0, ttl_seconds=3600,
            programs_root=tmp_path, now=_NOW,
        )
        target_id = target.snapshot_id
    else:
        target_bytes = b'{"version":"target"}'
        target_id = compute_snapshot_id(target_bytes)
        target_dir = channel_dir / target_id
        target_dir.mkdir(parents=True)
        (target_dir / "payload.json").write_bytes(target_bytes)

    selected = write_prefetch_snapshot(
        program_id="fixture_prog", channel="workiq", payload={"version": "selected"},
        watermark=None, completeness="complete", latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=_NOW,
    )
    manifest_path = channel_dir / selected.snapshot_id / "manifest.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_data["snapshot_id"] = target_id
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
    manifest_bytes = manifest_path.read_bytes()
    pointer_bytes = (channel_dir / "latest.json").read_bytes()

    assert read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=tmp_path) is None
    assert read_unexpired_committed_snapshot("fixture_prog", "workiq", programs_root=tmp_path, now=_NOW) is None
    assert "Prefetch snapshot manifest does not match the selected snapshot" in caplog.text
    assert selected.snapshot_id not in caplog.text
    assert target_id not in caplog.text
    assert manifest_path.read_bytes() == manifest_bytes
    assert (channel_dir / "latest.json").read_bytes() == pointer_bytes
    assert (channel_dir / target_id / "manifest.json").exists() is target_committed


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


def test_no_snapshot_at_all_is_none_not_an_error(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    programs_root = tmp_path / "programs"
    assert read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=programs_root) is None
    assert read_unexpired_committed_snapshot("fixture_prog", "workiq", programs_root=programs_root) is None
    assert not caplog.records


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


@pytest.mark.parametrize("historical", [False, True], ids=["latest", "historical"])
@pytest.mark.parametrize(
    "payload", [{"signals": []}, {"threads": ["synthetic-a", "synthetic-b"]}],
    ids=["empty", "populated"],
)
def test_repeated_committed_snapshot_preserves_payload_during_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, historical: bool, payload: dict[str, object],
) -> None:
    first = write_prefetch_snapshot(
        program_id="fixture_prog", channel="workiq", payload=payload, watermark="old",
        completeness="complete", latency_ms=1.0, ttl_seconds=3600,
        programs_root=tmp_path, now=_NOW,
    )
    channel_dir = tmp_path / "fixture_prog" / "runtime" / "prefetch" / "workiq"
    payload_path = channel_dir / first.snapshot_id / "payload.json"
    payload_before = payload_path.read_bytes()
    if historical:
        other = write_prefetch_snapshot(
            program_id="fixture_prog", channel="workiq", payload={"different": True}, watermark=None,
            completeness="complete", latency_ms=1.0, ttl_seconds=3600,
            programs_root=tmp_path, now=_NOW,
        )
        assert read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=tmp_path) == other

    original_open = Path.open

    def interrupt_in_place_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
        if path == payload_path and mode == "wb":
            with original_open(path, mode, *args, **kwargs) as handle:
                handle.write(b'{"partial":')
            raise OSError("Synthetic interrupted in-place write")
        return original_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", interrupt_in_place_open)
    refreshed = write_prefetch_snapshot(
        program_id="fixture_prog", channel="workiq", payload=payload, watermark="new",
        completeness="complete", latency_ms=2.0, ttl_seconds=1800,
        source_identities=("synthetic",), programs_root=tmp_path, now=_NOW + timedelta(minutes=5),
    )

    assert refreshed.snapshot_id == first.snapshot_id
    assert refreshed.created_at == _NOW + timedelta(minutes=5)
    assert refreshed.expires_at == _NOW + timedelta(minutes=35)
    assert refreshed.watermark == "new"
    assert refreshed.source_identities == ("synthetic",)
    assert payload_path.read_bytes() == payload_before
    assert read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=tmp_path) == refreshed
    assert read_snapshot_payload("fixture_prog", "workiq", first, programs_root=tmp_path) == payload


def test_committed_payload_comparison_failure_preserves_existing_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {"threads": ["synthetic"]}
    first = write_prefetch_snapshot(
        program_id="fixture_prog", channel="workiq", payload=payload, watermark=None,
        completeness="complete", latency_ms=1.0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    channel_dir = tmp_path / "fixture_prog" / "runtime" / "prefetch" / "workiq"
    payload_path = channel_dir / first.snapshot_id / "payload.json"
    manifest_path = payload_path.parent / "manifest.json"
    pointer_path = channel_dir / "latest.json"
    original_read_bytes = Path.read_bytes
    before = tuple(original_read_bytes(path) for path in (payload_path, manifest_path, pointer_path))

    def fail_payload_read(path: Path) -> bytes:
        if path == payload_path:
            raise PermissionError("Synthetic payload comparison failure")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fail_payload_read)
    with pytest.raises(PermissionError, match="Synthetic payload comparison failure"):
        write_prefetch_snapshot(
            program_id="fixture_prog", channel="workiq", payload=payload, watermark="new",
            completeness="complete", latency_ms=2.0, ttl_seconds=3600,
            programs_root=tmp_path, now=_NOW + timedelta(minutes=5),
        )
    assert tuple(original_read_bytes(path) for path in (payload_path, manifest_path, pointer_path)) == before


@pytest.mark.parametrize("replacement", [b"x", b" " * len(b'{"threads":["synthetic"]}')], ids=["different-size", "same-size"])
def test_nonmatching_committed_payload_keeps_existing_writer_behavior(tmp_path: Path, replacement: bytes) -> None:
    payload = {"threads": ["synthetic"]}
    first = write_prefetch_snapshot(
        program_id="fixture_prog", channel="workiq", payload=payload, watermark=None,
        completeness="complete", latency_ms=1.0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    payload_path = tmp_path / "fixture_prog" / "runtime" / "prefetch" / "workiq" / first.snapshot_id / "payload.json"
    payload_path.write_bytes(replacement)

    refreshed = write_prefetch_snapshot(
        program_id="fixture_prog", channel="workiq", payload=payload, watermark="new",
        completeness="complete", latency_ms=2.0, ttl_seconds=3600, programs_root=tmp_path,
        now=_NOW + timedelta(minutes=5),
    )
    assert refreshed.snapshot_id == first.snapshot_id
    assert read_snapshot_payload("fixture_prog", "workiq", refreshed, programs_root=tmp_path) == payload
    assert read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=tmp_path) == refreshed


def test_identical_uncommitted_payload_is_written_and_fsynced_before_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    previous = write_prefetch_snapshot(
        program_id="fixture_prog", channel="workiq", payload={"previous": True}, watermark=None,
        completeness="complete", latency_ms=1.0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    payload = {"threads": ["synthetic"]}
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    channel_dir = tmp_path / "fixture_prog" / "runtime" / "prefetch" / "workiq"
    snapshot_dir = channel_dir / compute_snapshot_id(payload_bytes)
    snapshot_dir.mkdir()
    payload_path = snapshot_dir / "payload.json"
    payload_path.write_bytes(payload_bytes)
    pointer_path = channel_dir / "latest.json"
    pointer_before = pointer_path.read_bytes()
    original_open = Path.open
    opened_payload = False

    def observe_open(path: Path, mode: str = "r", *args: object, **kwargs: object):
        nonlocal opened_payload
        if path == payload_path and mode == "wb":
            opened_payload = True
        return original_open(path, mode, *args, **kwargs)

    def fail_fsync(_: int) -> None:
        assert opened_payload
        raise OSError("Synthetic payload fsync failure")

    monkeypatch.setattr(Path, "open", observe_open)
    monkeypatch.setattr(os, "fsync", fail_fsync)
    with pytest.raises(OSError, match="Synthetic payload fsync failure"):
        write_prefetch_snapshot(
            program_id="fixture_prog", channel="workiq", payload=payload, watermark=None,
            completeness="complete", latency_ms=1.0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
        )
    assert not (snapshot_dir / "manifest.json").exists()
    assert pointer_path.read_bytes() == pointer_before
    assert read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=tmp_path) == previous


@pytest.mark.parametrize("ttl_seconds", [0, -1])
def test_invalid_ttl_fails_before_creating_prefetch_files(tmp_path: Path, ttl_seconds: int) -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        write_prefetch_snapshot(
            program_id="fixture_prog", channel="workiq", payload={"threads": []}, watermark=None,
            completeness="complete", latency_ms=1.0, ttl_seconds=ttl_seconds, programs_root=tmp_path, now=_NOW,
        )
    assert not (tmp_path / "fixture_prog").exists()


def test_missing_committed_payload_keeps_existing_writer_behavior(tmp_path: Path) -> None:
    payload = {"threads": ["synthetic"]}
    first = write_prefetch_snapshot(
        program_id="fixture_prog", channel="workiq", payload=payload, watermark=None,
        completeness="complete", latency_ms=1.0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    payload_path = tmp_path / "fixture_prog" / "runtime" / "prefetch" / "workiq" / first.snapshot_id / "payload.json"
    payload_path.unlink()

    refreshed = write_prefetch_snapshot(
        program_id="fixture_prog", channel="workiq", payload=payload, watermark="new",
        completeness="complete", latency_ms=2.0, ttl_seconds=3600, programs_root=tmp_path,
        now=_NOW + timedelta(minutes=5),
    )
    assert refreshed.snapshot_id == first.snapshot_id
    assert read_snapshot_payload("fixture_prog", "workiq", refreshed, programs_root=tmp_path) == payload


def test_manifest_publication_failure_preserves_previous_committed_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import src.core.prefetch_store as store

    payload = {"threads": ["synthetic"]}
    first = write_prefetch_snapshot(
        program_id="fixture_prog", channel="workiq", payload=payload, watermark="old",
        completeness="complete", latency_ms=1.0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    channel_dir = tmp_path / "fixture_prog" / "runtime" / "prefetch" / "workiq"
    snapshot_dir = channel_dir / first.snapshot_id
    paths = (snapshot_dir / "payload.json", snapshot_dir / "manifest.json", channel_dir / "latest.json")
    before = tuple(path.read_bytes() for path in paths)
    original_write = store._atomic_write_json

    def fail_manifest(path: Path, data: dict[str, object]) -> None:
        if path == paths[1]:
            raise OSError("Synthetic manifest publication failure")
        original_write(path, data)

    monkeypatch.setattr(store, "_atomic_write_json", fail_manifest)
    with pytest.raises(OSError, match="Synthetic manifest publication failure"):
        write_prefetch_snapshot(
            program_id="fixture_prog", channel="workiq", payload=payload, watermark="new",
            completeness="complete", latency_ms=2.0, ttl_seconds=3600,
            programs_root=tmp_path, now=_NOW + timedelta(minutes=5),
        )
    assert tuple(path.read_bytes() for path in paths) == before
    assert read_latest_committed_snapshot("fixture_prog", "workiq", programs_root=tmp_path) == first
    assert read_snapshot_payload("fixture_prog", "workiq", first, programs_root=tmp_path) == payload
