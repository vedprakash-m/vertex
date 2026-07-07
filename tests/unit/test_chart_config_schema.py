"""Unit tests for chart config JSON Schema — spec §11."""
from __future__ import annotations

import pytest

from src.core.charts.chart_config_schema import CHART_CONFIG_SCHEMA, FORBIDDEN_CHART_CONFIG_KEYS


def _validate(config: dict) -> list[str]:
    """Return list of validation error messages (empty = valid)."""
    try:
        import jsonschema
        validator = jsonschema.Draft202012Validator(CHART_CONFIG_SCHEMA)
        return [e.message for e in validator.iter_errors(config)]
    except ImportError:
        pytest.skip("jsonschema not available")


# ---------------------------------------------------------------------------
# Valid configs
# ---------------------------------------------------------------------------

def test_valid_line_config():
    config = {"type": "line", "x_axis": "CompletionDay", "y_axes": ["P50_hrs", "P75_hrs"]}
    assert _validate(config) == []


def test_valid_bar_config():
    config = {"type": "bar", "x_axis": "Week", "y_axes": ["Count"]}
    assert _validate(config) == []


def test_valid_stacked_bar_config():
    config = {"type": "stacked_bar", "x_axis": "Sprint", "y_axes": ["Done", "In Progress"]}
    assert _validate(config) == []


def test_valid_scatter_config():
    config = {"type": "scatter", "x_axis": "Date", "y_axes": ["Value"]}
    assert _validate(config) == []


def test_valid_combined_config():
    config = {
        "type": "combined",
        "x_axis": "Day",
        "y_axes": ["P50", "Count"],
        "primary": {"type": "line", "x_axis": "Day", "y_axes": ["P50"]},
        "secondary": {"type": "bar", "x_axis": "Day", "y_axes": ["Count"]},
    }
    assert _validate(config) == []


def test_valid_config_with_optional_fields():
    config = {
        "type": "line",
        "x_axis": "Day",
        "y_axes": ["P50"],
        "goal_lines": [{"label": "Target", "value": 24.0}],
        "annotations": [{"x": "2026-05-01", "label": "Post-fix"}],
        "palette": ["#2563EB"],
        "summary_hint": "trend_down",
    }
    assert _validate(config) == []


# ---------------------------------------------------------------------------
# Invalid configs
# ---------------------------------------------------------------------------

def test_missing_type_fails():
    errors = _validate({"x_axis": "Day", "y_axes": ["P50"]})
    assert any("type" in e.lower() or "required" in e.lower() for e in errors)


def test_missing_x_axis_fails():
    errors = _validate({"type": "line", "y_axes": ["P50"]})
    assert len(errors) > 0


def test_missing_y_axes_fails():
    errors = _validate({"type": "line", "x_axis": "Day"})
    assert len(errors) > 0


def test_invalid_type_value_fails():
    errors = _validate({"type": "pie", "x_axis": "Day", "y_axes": ["P50"]})
    assert len(errors) > 0


def test_empty_y_axes_fails():
    errors = _validate({"type": "line", "x_axis": "Day", "y_axes": []})
    assert len(errors) > 0


def test_goal_line_missing_value_fails():
    errors = _validate({
        "type": "line", "x_axis": "Day", "y_axes": ["P50"],
        "goal_lines": [{"label": "Target"}],  # missing value
    })
    assert len(errors) > 0


def test_additional_properties_rejected():
    errors = _validate({
        "type": "line", "x_axis": "Day", "y_axes": ["P50"],
        "unknown_field": "oops",
    })
    assert len(errors) > 0


# ---------------------------------------------------------------------------
# Forbidden keys
# ---------------------------------------------------------------------------

def test_forbidden_keys_exist_in_set():
    assert "show_previous_issue" in FORBIDDEN_CHART_CONFIG_KEYS
    assert "comparison_rows" in FORBIDDEN_CHART_CONFIG_KEYS
    assert "vega_lite_config" in FORBIDDEN_CHART_CONFIG_KEYS


def test_forbidden_keys_not_in_schema_properties():
    props = CHART_CONFIG_SCHEMA.get("properties", {})
    for key in FORBIDDEN_CHART_CONFIG_KEYS:
        assert key not in props, f"Forbidden key '{key}' must not be in schema properties"
