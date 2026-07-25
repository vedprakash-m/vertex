"""BL-F2 (specs/backlog.md, D-19): tests for Signal.workstream_ids, the
plural companion to workstream_id added additively so a signal can visibly
belong to more than one workstream.

Covers this row's own migration/replay and legacy-serialization acceptance
criteria: an old record (no `ws_ids` key) must round-trip identically to
before this field existed, and a genuinely plural signal must round-trip
its full workstream_ids set.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from src.core.journal import _signal_from_record, _signal_to_record, append_signal, read_signals
from src.core.models import Confidence
from src.core.models_v2 import Signal

_NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


def _make_signal(**overrides: object) -> Signal:
    defaults: dict[str, object] = dict(
        id="sig-1",
        timestamp=_NOW,
        source="ado/odata",
        program_id="acme",
        workstream_id="alpha",
        entity_refs=(),
        text="",
        raw_ref=None,
        confidence=Confidence.HIGH,
    )
    defaults.update(overrides)
    return Signal(**defaults)  # type: ignore[arg-type]


def test_post_init_defaults_workstream_ids_from_scalar() -> None:
    signal = _make_signal()
    assert signal.workstream_ids == ("alpha",)


def test_post_init_leaves_no_workstream_untouched() -> None:
    signal = _make_signal(workstream_id=None)
    assert signal.workstream_ids == ()


def test_explicit_plural_construction_is_not_overridden() -> None:
    signal = _make_signal(workstream_id="alpha", workstream_ids=("alpha", "beta"))
    assert signal.workstream_ids == ("alpha", "beta")


def test_replace_without_workstream_ids_preserves_existing_plural_set() -> None:
    signal = _make_signal(workstream_ids=("alpha", "beta"))
    moved = replace(signal, thread_id="t-1")
    assert moved.workstream_ids == ("alpha", "beta")


def test_replace_can_explicitly_override_plural_set() -> None:
    """The gather.py re-routing site's own pattern: passing workstream_ids
    explicitly alongside workstream_id replaces the set wholesale, per the
    BL-F2 "rerouting replaces, does not add" decision."""
    signal = _make_signal(workstream_ids=("alpha", "beta"))
    rerouted = replace(signal, workstream_id="gamma", workstream_ids=("gamma",))
    assert rerouted.workstream_ids == ("gamma",)


def test_serialization_round_trip_omits_ws_ids_for_single_workstream() -> None:
    """Space-saving choice: a non-plural signal's record looks exactly like
    it did before this field existed (ws_ids is null, not a redundant
    single-element list)."""
    signal = _make_signal()
    record = _signal_to_record(signal)
    assert record["ws_ids"] is None
    restored = _signal_from_record(record)
    assert restored.workstream_ids == ("alpha",)


def test_serialization_round_trip_preserves_genuine_plural_set() -> None:
    signal = _make_signal(workstream_ids=("alpha", "beta"))
    record = _signal_to_record(signal)
    assert record["ws_ids"] == ["alpha", "beta"]
    restored = _signal_from_record(record)
    assert restored.workstream_ids == ("alpha", "beta")


def test_legacy_record_missing_ws_ids_key_round_trips_via_post_init() -> None:
    """Migration/replay acceptance criterion: a record written before this
    field existed (no `ws_ids` key at all, not even null) must produce the
    exact same effective workstream_ids as a freshly-written single-workstream
    record -- proving old journal data doesn't need a backfill migration."""
    signal = _make_signal()
    legacy_record = _signal_to_record(signal)
    del legacy_record["ws_ids"]
    restored = _signal_from_record(legacy_record)
    assert restored.workstream_ids == ("alpha",)


def test_read_signals_filters_on_any_associated_workstream(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    shared = _make_signal(id="shared-1", workstream_id="alpha", workstream_ids=("alpha", "beta"))
    solo = _make_signal(id="solo-1", workstream_id="gamma")
    append_signal(shared, programs_root, partition_at=_NOW)
    append_signal(solo, programs_root, partition_at=_NOW)

    alpha_signals = read_signals("acme", workstream_id="alpha", programs_root=programs_root)
    beta_signals = read_signals("acme", workstream_id="beta", programs_root=programs_root)
    gamma_signals = read_signals("acme", workstream_id="gamma", programs_root=programs_root)

    assert {s.id for s in alpha_signals} == {"shared-1"}
    assert {s.id for s in beta_signals} == {"shared-1"}
    assert {s.id for s in gamma_signals} == {"solo-1"}
