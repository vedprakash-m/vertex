"""Golden file tests for the setup preview HTML renderer.

Freezes the clock and uses a fixed seed so that the output is byte-identical
across runs. Use ``pytest --update-golden`` to regenerate the golden file.
"""
from __future__ import annotations

import difflib
from datetime import datetime, timezone
from pathlib import Path

from src.commands.setup_preview import generate_preview_data, render_preview_html


GOLDEN_DIR = Path(__file__).resolve().parent / "public_snapshots"
FROZEN_NOW = datetime(2026, 5, 26, 12, 0, tzinfo=timezone.utc)

_WS_NAMES = ["Infra Health", "Feature Delivery", "Risk & Compliance"]
_SC_NAMES = [
    ("Infra Health", ["Infra Health Health"]),
    ("Feature Delivery", ["Feature Delivery Health"]),
    ("Risk & Compliance", ["Risk & Compliance Health"]),
]
_PROGRAM_NAME = "Acme Platform Reliability"
_EDITION_SLUG = "acme_platform_reliability_weekly"
_SYNTHETIC_OWNERS = (
    "Synthetic owner A",
    "Synthetic owner B",
    "Synthetic owner C",
    "Synthetic owner D",
    "Synthetic owner E",
)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.fromtimestamp(FROZEN_NOW.timestamp())
        return cls(
            FROZEN_NOW.year, FROZEN_NOW.month, FROZEN_NOW.day,
            FROZEN_NOW.hour, FROZEN_NOW.minute, FROZEN_NOW.second,
            FROZEN_NOW.microsecond, tzinfo=tz,
        )


def _compare_with_golden(name: str, actual: str, update: bool) -> None:
    golden_path = GOLDEN_DIR / f"{name}.golden"
    if update:
        GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(actual, encoding="utf-8")
        return

    assert golden_path.exists(), f"Missing public golden baseline: {name}.golden; regenerate explicitly with --update-golden."
    golden = golden_path.read_text(encoding="utf-8")
    if actual != golden:
        diff = "".join(
            difflib.unified_diff(
                golden.splitlines(keepends=True),
                actual.splitlines(keepends=True),
                fromfile=f"{name}.golden",
                tofile="actual",
            )
        )
        raise AssertionError(
            f"Preview HTML does not match golden file: {name}.golden\n\nDiff:\n{diff}"
        )


def _synthetic_preview_data(monkeypatch):
    monkeypatch.setattr("src.commands.setup_preview.datetime", _FrozenDatetime)
    monkeypatch.setattr("src.commands.setup_preview._STUB_OWNERS", _SYNTHETIC_OWNERS)
    return generate_preview_data(_WS_NAMES, _SC_NAMES, seed=42)


def test_setup_preview_html_golden(update_golden: bool, monkeypatch) -> None:
    """Golden: setup preview HTML is byte-identical across runs with frozen clock."""
    ws_data, sc_data = _synthetic_preview_data(monkeypatch)
    html = render_preview_html(_PROGRAM_NAME, _EDITION_SLUG, ws_data, sc_data)

    _compare_with_golden("setup_preview", html, update_golden)


def test_setup_preview_demo_html_golden(update_golden: bool, monkeypatch) -> None:
    """Golden: demo=True variant adds data-demo attribute and is stable."""
    ws_data, sc_data = _synthetic_preview_data(monkeypatch)
    html = render_preview_html(_PROGRAM_NAME, _EDITION_SLUG, ws_data, sc_data, demo=True)

    _compare_with_golden("setup_preview_demo", html, update_golden)
