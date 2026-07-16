"""ADF-W5.8 (Section 8.2.5): ``lineage_regression`` detection logic.

Tests the pure comparison detector; the cockpit wiring (best-effort alert
emission against retained history) is covered by
``test_cockpit_lineage_regression_alert.py``.
"""
from __future__ import annotations

import pytest

from src.core.lineage_regression_detector import (
    DEFAULT_MAX_DROP,
    build_lineage_regression_alert_message,
    detect_lineage_regression,
)


def test_no_prior_coverage_is_not_regressing() -> None:
    finding = detect_lineage_regression(previous_coverage=None, current_coverage=0.5)
    assert not finding.is_regressing
    assert finding.drop is None


def test_no_current_coverage_is_not_regressing() -> None:
    finding = detect_lineage_regression(previous_coverage=0.8, current_coverage=None)
    assert not finding.is_regressing
    assert finding.drop is None


def test_both_none_is_not_regressing() -> None:
    finding = detect_lineage_regression(previous_coverage=None, current_coverage=None)
    assert not finding.is_regressing


def test_improved_coverage_is_not_regressing() -> None:
    finding = detect_lineage_regression(previous_coverage=0.5, current_coverage=0.7)
    assert not finding.is_regressing
    assert finding.drop == pytest.approx(-0.2)


def test_small_drop_within_budget_is_not_regressing() -> None:
    # Default budget is 5 percentage points; a 2-point drop is normal drift.
    finding = detect_lineage_regression(previous_coverage=0.80, current_coverage=0.78)
    assert not finding.is_regressing
    assert finding.drop == pytest.approx(0.02)


def test_drop_over_budget_is_regressing() -> None:
    finding = detect_lineage_regression(previous_coverage=0.80, current_coverage=0.60)
    assert finding.is_regressing
    assert finding.drop == pytest.approx(0.20)
    assert finding.previous_coverage == 0.80
    assert finding.current_coverage == 0.60


def test_drop_exactly_at_budget_is_not_regressing() -> None:
    finding = detect_lineage_regression(
        previous_coverage=0.80, current_coverage=0.75, max_drop=0.05,
    )
    assert not finding.is_regressing


def test_custom_max_drop_respected() -> None:
    # A 3-point drop is over a tightened 1-point budget.
    finding = detect_lineage_regression(
        previous_coverage=0.90, current_coverage=0.87, max_drop=0.01,
    )
    assert finding.is_regressing


def test_alert_message_includes_percentages() -> None:
    finding = detect_lineage_regression(previous_coverage=0.80, current_coverage=0.60)
    assert finding.is_regressing
    message, next_command = build_lineage_regression_alert_message(finding)
    assert "80.0%" in message
    assert "60.0%" in message
    assert "doctor" in next_command.lower()


def test_alert_message_raises_on_non_regressing_finding() -> None:
    finding = detect_lineage_regression(previous_coverage=0.80, current_coverage=0.79)
    assert not finding.is_regressing
    with pytest.raises(AssertionError):
        build_lineage_regression_alert_message(finding)


def test_default_max_drop_is_five_percent() -> None:
    assert DEFAULT_MAX_DROP == 0.05
