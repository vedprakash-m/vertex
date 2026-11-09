"""ADF-W1.5/ADF-W1.10 remainder: src/commands/gather_pipeline/workiq_prefetch_stage.py."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import sys
from pathlib import Path

import pytest

from src.commands.gather_pipeline.workiq_prefetch_stage import (
    WORKIQ_PREFETCH_CHANNEL,
    resolve_workiq_signals,
    workiq_signals_from_payload,
    workiq_signals_to_payload,
)
from src.core.models import Confidence
from src.core.models_v2 import Signal
from src.core.journal import signal_to_record
from src.core.m365_payload_support import has_provisional_workiq_identity
from src.core.prefetch_store import (
    compute_snapshot_id,
    read_snapshot_payload,
    read_unexpired_committed_snapshot,
    write_prefetch_snapshot,
)

_NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=timezone.utc)


def _signal(signal_id: str = "sig-1") -> Signal:
    return Signal(
        id=signal_id,
        timestamp=_NOW,
        source="workiq",
        program_id="samplealpha",
        workstream_id="deployment",
        entity_refs=("WI:123",),
        text="A stakeholder mentioned a delay.",
        raw_ref=None,
        confidence=Confidence.MEDIUM,
    )


def _write_raw_committed_snapshot(programs_root: Path, payload_bytes: bytes, *, now: datetime = _NOW) -> None:
    manifest = write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload(()), watermark=None, completeness="complete",
        latency_ms=0, ttl_seconds=3600, programs_root=programs_root, now=now,
    )
    channel_dir = programs_root / "samplealpha" / "runtime" / "prefetch" / WORKIQ_PREFETCH_CHANNEL
    snapshot_id = compute_snapshot_id(payload_bytes)
    snapshot_dir = channel_dir / snapshot_id
    (channel_dir / manifest.snapshot_id).rename(snapshot_dir)
    (snapshot_dir / manifest.payload_path).write_bytes(payload_bytes)
    (snapshot_dir / "manifest.json").write_text(
        json.dumps(replace(manifest, snapshot_id=snapshot_id).to_dict()), encoding="utf-8",
    )
    (channel_dir / "latest.json").write_text(json.dumps({"snapshot_id": snapshot_id}), encoding="utf-8")


def test_payload_round_trip() -> None:
    signals = (_signal("s1"), _signal("s2"))
    payload = workiq_signals_to_payload(signals)
    restored = workiq_signals_from_payload(payload)
    assert [s.id for s in restored] == ["s1", "s2"]
    assert restored[0].text == signals[0].text


def test_preview_only_identity_cannot_be_written_or_loaded_from_prefetch() -> None:
    preview = replace(
        _signal("preview"),
        source="workiq/email",
        metadata={
            "message_id": "semantic:preview",
            "native_message_id": "semantic:preview",
            "source_identity_kind": "semantic_preview",
        },
    )
    with pytest.raises(ValueError, match="provisional identity"):
        workiq_signals_to_payload((preview,))
    with pytest.raises(ValueError, match="provisional identity"):
        workiq_signals_from_payload({"signals": [signal_to_record(preview)]})
    legacy_thread_fallback = replace(
        preview,
        metadata={
            "message_id": "native-thread:seg:0",
            "parent_message_id": "native-thread",
            "native_message_id": "native-thread",
            "thread_id": "native-thread",
            "message_id_kind": "derived_fragment",
        },
    )
    assert has_provisional_workiq_identity(legacy_thread_fallback.metadata)


def test_preview_only_cached_snapshot_falls_back_to_live(
    tmp_path: Path,
) -> None:
    preview = replace(
        _signal("preview"),
        source="workiq/email",
        metadata={
            "message_id": "semantic:preview",
            "native_message_id": "semantic:preview",
            "source_identity_kind": "semantic_preview",
        },
    )
    _write_raw_committed_snapshot(
        tmp_path,
        json.dumps({"signals": [signal_to_record(preview)]}).encode("utf-8"),
    )
    live = _signal("live")
    assert resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path,
        live_fetch_fn=lambda: (live,), now=_NOW,
    ) == (live,)


def test_payload_from_payload_handles_missing_signals_key() -> None:
    assert workiq_signals_from_payload({}) == ()


def test_resolve_falls_back_to_live_when_no_snapshot(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    live_called = []

    def live_fetch() -> tuple[Signal, ...]:
        live_called.append(True)
        return (_signal("live-1"),)

    result = resolve_workiq_signals(program_id="samplealpha", programs_root=programs_root, live_fetch_fn=live_fetch)
    assert live_called == [True]
    assert [s.id for s in result] == ["live-1"]


def test_resolve_prefers_unexpired_committed_snapshot(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    write_prefetch_snapshot(
        program_id="samplealpha",
        channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((_signal("cached-1"),)),
        watermark=None,
        completeness="complete",
        latency_ms=1234.0,
        ttl_seconds=3600,
        programs_root=programs_root,
        now=_NOW,
    )
    live_called = []

    def live_fetch() -> tuple[Signal, ...]:
        live_called.append(True)
        return (_signal("live-1"),)

    result = resolve_workiq_signals(
        program_id="samplealpha", programs_root=programs_root, live_fetch_fn=live_fetch, now=_NOW
    )
    assert live_called == []  # never called -- snapshot served instead
    assert [s.id for s in result] == ["cached-1"]


@pytest.mark.parametrize(
    "payload",
    [{}, {"signals": None}, {"signals": "Private malformed payload"}, {"signals": 0}, {"signals": {}}],
    ids=["missing", "null", "string", "number", "object"],
)
def test_resolve_falls_back_for_missing_or_non_list_cached_signals(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, payload: dict[str, object],
) -> None:
    write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL, payload=payload,
        watermark=None, completeness="complete", latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=_NOW,
    )
    live_calls: list[bool] = []

    def live_fetch() -> tuple[Signal, ...]:
        live_calls.append(True)
        return (_signal("live"),)

    result = resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=live_fetch, now=_NOW,
    )
    assert [signal.id for signal in result] == ["live"]
    assert live_calls == [True]
    assert "WorkIQ cached snapshot has an invalid signals payload" in caplog.text
    assert "Private malformed payload" not in caplog.text


@pytest.mark.parametrize("record", [None, "Private malformed record", 42], ids=["null", "string", "number"])
@pytest.mark.parametrize("include_valid", [False, True])
def test_resolve_falls_back_for_non_object_cached_signal_records(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, record: object, include_valid: bool,
) -> None:
    payload = workiq_signals_to_payload((_signal("cached"),) if include_valid else ())
    payload["signals"].append(record)
    write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL, payload=payload,
        watermark=None, completeness="complete", latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=_NOW,
    )
    live_calls: list[bool] = []

    def live_fetch() -> tuple[Signal, ...]:
        live_calls.append(True)
        return (_signal("live"),)

    result = resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=live_fetch, now=_NOW,
    )
    assert [signal.id for signal in result] == ["live"]
    assert live_calls == [True]
    assert "WorkIQ cached snapshot has an invalid signals payload" in caplog.text
    assert "Private malformed record" not in caplog.text


@pytest.mark.parametrize("payload", [None, [], "Private malformed payload", 42], ids=["null", "list", "string", "number"])
def test_resolve_falls_back_for_non_object_cached_payload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, payload: object,
) -> None:
    _write_raw_committed_snapshot(tmp_path, json.dumps(payload).encode("utf-8"))
    live_calls: list[bool] = []

    def live_fetch() -> tuple[Signal, ...]:
        live_calls.append(True)
        return (_signal("live"),)

    result = resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=live_fetch, now=_NOW,
    )
    assert [signal.id for signal in result] == ["live"]
    assert live_calls == [True]
    assert "WorkIQ cached snapshot has an invalid signals payload" in caplog.text
    assert "Private malformed payload" not in caplog.text


@pytest.mark.parametrize("payload_bytes", [b"{", b"\xff"], ids=["invalid-json", "invalid-utf8"])
def test_resolve_falls_back_for_invalid_content_addressed_bytes(tmp_path: Path, payload_bytes: bytes) -> None:
    _write_raw_committed_snapshot(tmp_path, payload_bytes)
    live = _signal("live")
    assert resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=lambda: (live,), now=_NOW,
    ) == (live,)


@pytest.mark.parametrize("nesting", ["array", "object"])
def test_resolve_falls_back_for_deeply_nested_content_addressed_payload(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, nesting: str,
) -> None:
    depth = sys.getrecursionlimit() + 100
    nested = (
        b"[" * depth + b'"Synthetic hidden payload"' + b"]" * depth
        if nesting == "array"
        else b'{"value":' * depth + b'"Synthetic hidden payload"' + b"}" * depth
    )
    payload_bytes = b'{"signals":' + nested + b"}"
    _write_raw_committed_snapshot(tmp_path, payload_bytes)
    channel_dir = tmp_path / "samplealpha" / "runtime" / "prefetch" / WORKIQ_PREFETCH_CHANNEL
    snapshot_dir = channel_dir / compute_snapshot_id(payload_bytes)
    manifest_before = (snapshot_dir / "manifest.json").read_bytes()
    pointer_before = (channel_dir / "latest.json").read_bytes()
    manifest = read_unexpired_committed_snapshot("samplealpha", WORKIQ_PREFETCH_CHANNEL, programs_root=tmp_path, now=_NOW)
    assert manifest is not None
    payload = read_snapshot_payload("samplealpha", WORKIQ_PREFETCH_CHANNEL, manifest, programs_root=tmp_path)
    assert isinstance(payload, dict)
    live = _signal("live")
    live_calls: list[bool] = []

    def live_fetch() -> tuple[Signal, ...]:
        live_calls.append(True)
        return (live,)

    assert resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=live_fetch, now=_NOW,
    ) == (live,)
    assert live_calls == [True]
    assert "WorkIQ cached snapshot is too deeply nested; falling back to live." in caplog.text
    assert "Synthetic hidden payload" not in caplog.text
    assert str(tmp_path) not in caplog.text
    assert manifest.snapshot_id not in caplog.text
    assert (snapshot_dir / "payload.json").read_bytes() == payload_bytes
    assert (snapshot_dir / "manifest.json").read_bytes() == manifest_before
    assert (channel_dir / "latest.json").read_bytes() == pointer_before


def test_resolve_propagates_live_failure_after_deeply_nested_cached_payload(tmp_path: Path) -> None:
    depth = sys.getrecursionlimit() + 100
    payload_bytes = b'{"signals":' + b"[" * depth + b"]" * depth + b"}"
    _write_raw_committed_snapshot(tmp_path, payload_bytes)

    def live_fetch() -> tuple[Signal, ...]:
        raise TimeoutError("Synthetic live failure")

    with pytest.raises(TimeoutError, match="Synthetic live failure"):
        resolve_workiq_signals(
            program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=live_fetch, now=_NOW,
        )
    payload_path = tmp_path / "samplealpha" / "runtime" / "prefetch" / WORKIQ_PREFETCH_CHANNEL / compute_snapshot_id(payload_bytes) / "payload.json"
    assert payload_path.read_bytes() == payload_bytes


def test_resolve_hides_cached_payload_recursion_error_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    import src.commands.gather_pipeline.workiq_prefetch_stage as prefetch_stage

    write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload(()), watermark=None, completeness="complete",
        latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )

    def fail_read(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise RecursionError("Synthetic hidden exception details")

    monkeypatch.setattr(prefetch_stage, "read_snapshot_payload", fail_read)
    live = _signal("live")
    assert resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=lambda: (live,), now=_NOW,
    ) == (live,)
    assert "WorkIQ cached snapshot is too deeply nested; falling back to live." in caplog.text
    assert "Synthetic hidden exception details" not in caplog.text


@pytest.mark.parametrize(
    ("target", "error"),
    [("read_snapshot_payload", RuntimeError), ("workiq_signals_from_payload", RecursionError)],
)
def test_resolve_propagates_unexpected_cached_payload_processing_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, error: type[Exception],
) -> None:
    import src.commands.gather_pipeline.workiq_prefetch_stage as prefetch_stage

    write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload(()), watermark=None, completeness="complete",
        latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )

    def fail_processing(*_args: object, **_kwargs: object) -> dict[str, object]:
        raise error("Unexpected synthetic failure")

    monkeypatch.setattr(prefetch_stage, target, fail_processing)
    with pytest.raises(error, match="Unexpected synthetic failure"):
        resolve_workiq_signals(
            program_id="samplealpha", programs_root=tmp_path,
            live_fetch_fn=lambda: pytest.fail("unexpected live call"), now=_NOW,
        )


@pytest.mark.parametrize("kind", ["record", "empty", "whitespace", "invalid-json"])
def test_resolve_falls_back_for_mismatched_snapshot_bytes(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, kind: str,
) -> None:
    manifest = write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((_signal("cached"),)), watermark=None, completeness="complete",
        latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    snapshot_dir = tmp_path / "samplealpha" / "runtime" / "prefetch" / WORKIQ_PREFETCH_CHANNEL / manifest.snapshot_id
    payload_path = snapshot_dir / manifest.payload_path
    if kind == "record":
        replacement = json.dumps(workiq_signals_to_payload((_signal("Private changed signal"),))).encode("utf-8")
    elif kind == "empty":
        replacement = b'{"signals":[]}'
    elif kind == "whitespace":
        replacement = payload_path.read_bytes() + b" "
    else:
        replacement = b"{"
    payload_path.write_bytes(replacement)
    manifest_bytes = (snapshot_dir / "manifest.json").read_bytes()
    live_calls: list[bool] = []

    def live_fetch() -> tuple[Signal, ...]:
        live_calls.append(True)
        return (_signal("live"),)

    result = resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=live_fetch, now=_NOW,
    )
    assert [signal.id for signal in result] == ["live"]
    assert live_calls == [True]
    assert "Prefetch snapshot payload does not match its content address" in caplog.text
    assert "Private changed signal" not in caplog.text
    assert manifest.snapshot_id not in caplog.text
    assert payload_path.read_bytes() == replacement
    assert (snapshot_dir / "manifest.json").read_bytes() == manifest_bytes


def test_resolve_falls_back_for_manifest_pointing_to_an_uncommitted_snapshot(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    manifest = write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((_signal("cached"),)), watermark=None, completeness="complete",
        latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    channel_dir = tmp_path / "samplealpha" / "runtime" / "prefetch" / WORKIQ_PREFETCH_CHANNEL
    orphan_bytes = json.dumps(workiq_signals_to_payload((_signal("orphan"),))).encode("utf-8")
    orphan_id = compute_snapshot_id(orphan_bytes)
    orphan_dir = channel_dir / orphan_id
    orphan_dir.mkdir()
    (orphan_dir / "payload.json").write_bytes(orphan_bytes)
    manifest_path = channel_dir / manifest.snapshot_id / "manifest.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_data["snapshot_id"] = orphan_id
    manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")
    manifest_bytes = manifest_path.read_bytes()
    live = _signal("live")

    assert resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=lambda: (live,), now=_NOW,
    ) == (live,)
    assert "Prefetch snapshot manifest does not match the selected snapshot" in caplog.text
    assert orphan_id not in caplog.text
    assert manifest_path.read_bytes() == manifest_bytes
    assert not (orphan_dir / "manifest.json").exists()


def test_resolve_propagates_live_failure_after_snapshot_digest_mismatch(tmp_path: Path) -> None:
    manifest = write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((_signal("cached"),)), watermark=None, completeness="complete",
        latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    payload_path = tmp_path / "samplealpha" / "runtime" / "prefetch" / WORKIQ_PREFETCH_CHANNEL / manifest.snapshot_id / manifest.payload_path
    payload_path.write_bytes(b'{"signals":[]}')

    def live_fetch() -> tuple[Signal, ...]:
        raise TimeoutError("Synthetic live failure")

    with pytest.raises(TimeoutError, match="Synthetic live failure"):
        resolve_workiq_signals(
            program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=live_fetch, now=_NOW,
        )
    assert payload_path.read_bytes() == b'{"signals":[]}'


@pytest.mark.parametrize("target", ["pointer", "manifest"])
@pytest.mark.parametrize(
    "metadata_bytes",
    [
        b'{"snapshot_id":"Synthetic hidden value"\xff}',
        b'{"snapshot_id":' + b"[" * (sys.getrecursionlimit() + 100) + b"]" * (sys.getrecursionlimit() + 100) + b"}",
    ],
    ids=["invalid-utf8", "deep-nesting"],
)
def test_resolve_falls_back_for_unreadable_selection_metadata(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, target: str, metadata_bytes: bytes,
) -> None:
    manifest = write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((_signal("cached"),)), watermark=None, completeness="complete",
        latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    channel_dir = tmp_path / "samplealpha" / "runtime" / "prefetch" / WORKIQ_PREFETCH_CHANNEL
    selected_path = channel_dir / "latest.json" if target == "pointer" else channel_dir / manifest.snapshot_id / "manifest.json"
    selected_path.write_bytes(metadata_bytes)
    live = _signal("live")
    live_calls: list[bool] = []

    def live_fetch() -> tuple[Signal, ...]:
        live_calls.append(True)
        return (live,)

    assert resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=live_fetch, now=_NOW,
    ) == (live,)
    assert live_calls == [True]
    assert f"Prefetch snapshot {target} is unreadable or invalid." in caplog.text
    assert "Synthetic hidden value" not in caplog.text
    assert selected_path.read_bytes() == metadata_bytes


@pytest.mark.parametrize("target", ["pointer", "manifest"])
def test_resolve_propagates_live_failure_after_unreadable_selection_metadata(tmp_path: Path, target: str) -> None:
    manifest = write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((_signal("cached"),)), watermark=None, completeness="complete",
        latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    channel_dir = tmp_path / "samplealpha" / "runtime" / "prefetch" / WORKIQ_PREFETCH_CHANNEL
    selected_path = channel_dir / "latest.json" if target == "pointer" else channel_dir / manifest.snapshot_id / "manifest.json"
    selected_path.write_bytes(b"\xff")

    def live_fetch() -> tuple[Signal, ...]:
        raise TimeoutError("Synthetic live failure")

    with pytest.raises(TimeoutError, match="Synthetic live failure"):
        resolve_workiq_signals(
            program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=live_fetch, now=_NOW,
        )
    assert selected_path.read_bytes() == b"\xff"


@pytest.mark.parametrize("target", ["pointer", "manifest", "payload"])
def test_gather_uses_live_workiq_after_unreadable_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str,
) -> None:
    from src.commands import gather
    from src.core.models_v2 import ADOConfig, M365Config, Program

    now = datetime.now(timezone.utc)
    programs_root = tmp_path / "programs"
    monkeypatch.setenv("VERTEX_DB_PATH", str(tmp_path / "db"))
    program = Program(
        schema_version="2.0", id="samplealpha", name="Synthetic fixture",
        ado=ADOConfig(
            organization="fixture", project="fixture", area_paths=(), work_item_types=("Feature",),
            excluded_states=(), date_window_days=14,
        ),
        m365=M365Config(enabled=True, prefer_agency=True, workiq_queries={"feedback_search": "Synthetic query"}),
    )
    monkeypatch.setattr(gather, "_load_program_context", lambda *_args, **_kwargs: (program, ()))
    monkeypatch.setattr(gather, "_load_freshness_thresholds", lambda *_args, **_kwargs: (14, 30))
    live_calls: list[bool] = []

    def live_fetch(**_kwargs: object) -> tuple[Signal, ...]:
        live_calls.append(True)
        return (_signal("live"),)

    monkeypatch.setattr(gather, "_build_workiq_signals", live_fetch)
    channel_dir = programs_root / "samplealpha" / "runtime" / "prefetch" / WORKIQ_PREFETCH_CHANNEL
    if target == "payload":
        depth = sys.getrecursionlimit() + 100
        selected_bytes = b'{"signals":' + b"[" * depth + b"]" * depth + b"}"
        _write_raw_committed_snapshot(programs_root, selected_bytes, now=now)
        selected_path = channel_dir / compute_snapshot_id(selected_bytes) / "payload.json"
    else:
        manifest = write_prefetch_snapshot(
            program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
            payload=workiq_signals_to_payload((_signal("cached"),)), watermark=None, completeness="complete",
            latency_ms=0, ttl_seconds=3600, programs_root=programs_root, now=now,
        )
        selected_path = channel_dir / "latest.json" if target == "pointer" else channel_dir / manifest.snapshot_id / "manifest.json"
        selected_bytes = b"\xff"
        selected_path.write_bytes(selected_bytes)

    artifacts = gather.gather_program(
        "samplealpha", as_of=now, programs_root=programs_root, include_workiq=True,
        loader=lambda *_args, **_kwargs: ((), 0),
        freshness_loader=lambda *_args, **_kwargs: ((), 0),
    )
    assert live_calls == [True]
    assert artifacts.discovered_signals == 1
    assert artifacts.integration_errors == ()
    assert selected_path.read_bytes() == selected_bytes


@pytest.mark.parametrize("completeness", ["complete", "partial"])
def test_resolve_preserves_valid_empty_cached_signals(tmp_path: Path, completeness: str) -> None:
    write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL, payload=workiq_signals_to_payload(()),
        watermark=None, completeness=completeness, latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=_NOW,
    )
    assert resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path,
        live_fetch_fn=lambda: pytest.fail("The valid empty snapshot should be used"), now=_NOW,
    ) == ()


def test_resolve_propagates_live_failure_after_malformed_cached_signals(tmp_path: Path) -> None:
    write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL, payload={},
        watermark=None, completeness="complete", latency_ms=0, ttl_seconds=3600,
        programs_root=tmp_path, now=_NOW,
    )

    def live_fetch() -> tuple[Signal, ...]:
        raise TimeoutError("Synthetic live failure")

    with pytest.raises(TimeoutError, match="Synthetic live failure"):
        resolve_workiq_signals(
            program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=live_fetch, now=_NOW,
        )


def test_resolve_falls_back_when_snapshot_expired(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    write_prefetch_snapshot(
        program_id="samplealpha",
        channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((_signal("cached-1"),)),
        watermark=None,
        completeness="complete",
        latency_ms=1234.0,
        ttl_seconds=1,
        programs_root=programs_root,
        now=_NOW,
    )
    live_called = []

    def live_fetch() -> tuple[Signal, ...]:
        live_called.append(True)
        return (_signal("live-1"),)

    from datetime import timedelta

    # 2 hours after the 1-second TTL snapshot -- definitely expired.
    result = resolve_workiq_signals(
        program_id="samplealpha", programs_root=programs_root, live_fetch_fn=live_fetch,
        now=_NOW + timedelta(hours=2),
    )
    assert live_called == [True]
    assert [s.id for s in result] == ["live-1"]


def test_resolve_with_none_programs_root_always_falls_back_to_live() -> None:
    live_called = []

    def live_fetch() -> tuple[Signal, ...]:
        live_called.append(True)
        return ()

    resolve_workiq_signals(program_id="samplealpha", programs_root=None, live_fetch_fn=live_fetch)
    assert live_called == [True]


@pytest.mark.parametrize("error", [None, "", 12])
def test_cached_unusable_response_diagnostic_without_error_falls_back_to_live(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, error: object,
) -> None:
    diagnostic = replace(
        _signal("bad-diagnostic"), source="system",
        metadata={"integration_source": "workiq", "workiq_structured_response_unusable": True, "error": error},
    )
    write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((diagnostic,)), watermark=None, completeness="degraded",
        latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    live = _signal("live")
    result = resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=lambda: (live,), now=_NOW,
    )
    assert result == (live,)
    assert "WorkIQ discovery diagnostic" in caplog.text


@pytest.mark.parametrize("include_valid", [False, True])
def test_cached_workiq_budget_diagnostic_replays_optional_error(tmp_path: Path, include_valid: bool) -> None:
    from src.core.models_v2 import IntegrationError

    message = "WorkIQ query-plan budget exhausted; at least one plan was not attempted."
    diagnostic = replace(
        _signal("budget"), source="system", workstream_id=None, entity_refs=(), confidence=Confidence.NONE,
        metadata={"integration_source": "workiq", "workiq_plan_budget_exhausted": True, "error": message},
    )
    signals = (_signal("valid"), diagnostic) if include_valid else (diagnostic,)
    write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload(signals), watermark=None, completeness="degraded",
        latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    errors: list[IntegrationError] = []
    restored = resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path,
        live_fetch_fn=lambda: pytest.fail("The committed snapshot should be used"),
        now=_NOW, integration_error_sink=errors,
    )

    assert len(restored) == len(signals)
    assert len(errors) == 1
    assert errors[0].source == "workiq"
    assert errors[0].stage == "budget"
    assert errors[0].message == message
    assert errors[0].retryable is True
    assert errors[0].operator_action == "Review the WorkIQ query-plan time budget and retry."


@pytest.mark.parametrize("error", [None, "", 12, "Private untrusted cached error"])
def test_cached_workiq_budget_diagnostic_with_invalid_error_falls_back_to_live(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, error: object,
) -> None:
    diagnostic = replace(
        _signal("bad-budget"), source="system", metadata={
            "integration_source": "workiq", "workiq_plan_budget_exhausted": True, "error": error,
        },
    )
    write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((diagnostic,)), watermark=None, completeness="degraded",
        latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    live = _signal("live")
    result = resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=lambda: (live,), now=_NOW,
    )
    assert result == (live,)
    assert "WorkIQ query-plan budget diagnostic" in caplog.text
    assert "Private untrusted cached error" not in caplog.text


@pytest.mark.parametrize(
    ("marker", "integration_source"),
    [(True, None), (True, "private source"), (False, "workiq"), (None, "workiq"),
     (1, "workiq"), ("true", "workiq"), ([], "workiq")],
)
def test_cached_workiq_budget_diagnostic_with_invalid_marker_falls_back_to_live(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, marker: object, integration_source: str | None,
) -> None:
    from src.core.models_v2 import IntegrationError

    diagnostic = replace(
        _signal("bad-budget"), source="system", metadata={
            "integration_source": integration_source, "workiq_plan_budget_exhausted": marker,
            "error": "WorkIQ query-plan budget exhausted; at least one plan was not attempted.",
        },
    )
    write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((diagnostic,)), watermark=None, completeness="degraded",
        latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    live = _signal("live")
    errors: list[IntegrationError] = []
    result = resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=lambda: (live,),
        now=_NOW, integration_error_sink=errors,
    )
    assert result == (live,)
    assert errors == []
    assert "WorkIQ query-plan budget diagnostic" in caplog.text
    assert "private source" not in caplog.text


def test_cached_unrelated_system_signal_with_inactive_budget_marker_remains_ordinary(tmp_path: Path) -> None:
    ordinary = replace(
        _signal("ordinary-system"), source="system",
        metadata={"integration_source": "kusto", "workiq_plan_budget_exhausted": False},
    )
    write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((ordinary,)), watermark=None, completeness="complete",
        latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    restored = resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path,
        live_fetch_fn=lambda: pytest.fail("The unrelated cached signal should remain usable"), now=_NOW,
    )
    assert len(restored) == 1 and restored[0].id == "ordinary-system"


@pytest.mark.parametrize("other_marker", ["quarantined", "workiq_structured_response_unusable"])
def test_cached_workiq_budget_diagnostic_rejects_conflicting_kinds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, other_marker: str,
) -> None:
    diagnostic = replace(
        _signal("bad-budget"), source="system", metadata={
            "integration_source": "workiq", "workiq_plan_budget_exhausted": True,
            other_marker: True, "error": "WorkIQ query-plan budget exhausted; at least one plan was not attempted.",
        },
    )
    write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((diagnostic,)), watermark=None, completeness="degraded",
        latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    live = _signal("live")
    result = resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path, live_fetch_fn=lambda: (live,), now=_NOW,
    )
    assert result == (live,)
    assert "WorkIQ query-plan budget diagnostic" in caplog.text


def test_cached_non_system_signal_with_budget_marker_remains_ordinary(tmp_path: Path) -> None:
    from src.core.models_v2 import IntegrationError

    ordinary = replace(
        _signal("ordinary"), source="workiq/email",
        metadata={"integration_source": "workiq", "workiq_plan_budget_exhausted": True},
    )
    write_prefetch_snapshot(
        program_id="samplealpha", channel=WORKIQ_PREFETCH_CHANNEL,
        payload=workiq_signals_to_payload((ordinary,)), watermark=None, completeness="complete",
        latency_ms=0, ttl_seconds=3600, programs_root=tmp_path, now=_NOW,
    )
    errors: list[IntegrationError] = []
    restored = resolve_workiq_signals(
        program_id="samplealpha", programs_root=tmp_path,
        live_fetch_fn=lambda: pytest.fail("The ordinary cached signal should remain usable"),
        now=_NOW, integration_error_sink=errors,
    )
    assert len(restored) == 1 and restored[0].id == "ordinary"
    assert errors == []
