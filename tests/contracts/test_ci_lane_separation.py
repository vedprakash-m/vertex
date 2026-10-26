"""RELO-W0.7a: the bounded performance lane is opt-in."""

from __future__ import annotations

import pytest


pytestmark = pytest.mark.perf


def test_perf_lane_marker_is_registered() -> None:
    """A deliberately tiny sentinel keeps explicit ``-m perf`` collection non-empty."""
    assert True
