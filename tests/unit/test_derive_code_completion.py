"""GAP-35: ``scripts/derive_code_completion.py`` snapshot-derived code-completion tooling.

Verifies the tool mechanically derives a per-capability code-completion ratio
from the live tree (replacing the frozen editorial estimates in
``specs/gaps.md``'s Completion Snapshot), is read-only, and emits stable
human + JSON output.
"""
from __future__ import annotations

import json

import pytest

from scripts.derive_code_completion import CAPABILITIES, derive, main


def test_derive_returns_one_entry_per_declared_capability() -> None:
    derived = derive()
    assert set(derived.keys()) == {cap.key for cap in CAPABILITIES}
    # Every entry carries the contract fields.
    for cap in CAPABILITIES:
        entry = derived[cap.key]
        assert entry["label"] == cap.label
        assert isinstance(entry["present"], int)
        assert isinstance(entry["total"], int)
        assert entry["total"] == len(cap.signals)
        assert 0.0 <= entry["ratio"] <= 1.0
        assert isinstance(entry["modules"], int)
        assert isinstance(entry["tests"], int)
        # present never exceeds total.
        assert entry["present"] <= entry["total"]


def test_ratio_equals_present_over_total() -> None:
    derived = derive()
    for entry in derived.values():
        if entry["total"] == 0:
            assert entry["ratio"] == 0.0
        else:
            assert entry["ratio"] == round(entry["present"] / entry["total"], 4)


def test_known_load_bearing_signals_are_present() -> None:
    """The curated signal set must reflect the real tree — if a load-bearing
    path disappears, the ratio drops and the spec author is alerted (rather
    than the count silently staying 100%).  Sanity-check a few anchors."""
    derived = derive()
    # report_pipeline anchors all exist on a healthy tree.
    assert derived["report_pipeline"]["present"] == derived["report_pipeline"]["total"]
    assert derived["report_pipeline"]["ratio"] == 1.0
    # reality_substrate anchors (resolver, fact store, truth_model) exist.
    assert derived["reality_substrate"]["ratio"] == 1.0
    # contract/invariant tests are tracked.
    assert derived["contract_invariant"]["present"] == derived["contract_invariant"]["total"]


def test_main_json_emits_valid_json_and_exits_zero(capsys: pytest.CaptureFixture) -> None:
    rc = main(["--format", "json"])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert set(payload.keys()) == {cap.key for cap in CAPABILITIES}
    for cap in CAPABILITIES:
        assert "ratio" in payload[cap.key]
        assert "present" in payload[cap.key]
        assert "total" in payload[cap.key]


def test_main_human_emits_table_and_exits_zero(capsys: pytest.CaptureFixture) -> None:
    rc = main(["--format", "human"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "capability" in out
    assert "ratio" in out
    assert "overall" in out
    # Every declared capability label-key appears in the table.
    for cap in CAPABILITIES:
        assert cap.key in out


def test_main_default_format_is_human(capsys: pytest.CaptureFixture) -> None:
    rc = main([])
    assert rc == 0
    out = capsys.readouterr().out
    assert "overall" in out  # human table footer


def test_signals_reference_real_paths_only() -> None:
    """Every signal must be a concrete repo-relative path (no ``<…>``
    placeholders) so a probe never masks a missing capability behind a
    skipped placeholder."""
    for cap in CAPABILITIES:
        for signal in cap.signals:
            assert "<" not in signal and ">" not in signal, (
                f"{cap.key} signal {signal!r} must not be a placeholder"
            )
            # No signal may be empty.
            assert signal.strip() == signal and signal, f"{cap.key} has empty signal"