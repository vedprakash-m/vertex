"""Precision monitoring with deliberately synthetic operator policy."""

import json
from pathlib import Path

import pytest

from src.commands.doctor_checks.storage_checks import _rev_extraction_precision_regression_check


@pytest.fixture(autouse=True)
def _clear_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VERTEX_REV_PRECISION_BASELINE", raising=False)
    monkeypatch.delenv("VERTEX_REV_PRECISION_REGRESSION_DELTA", raising=False)


def _metrics(root: Path, current: float) -> None:
    quality = root / "demo" / "_quality"
    quality.mkdir(parents=True)
    (quality / "rev_quality_metrics.json").write_text(
        json.dumps({"g_xtract_prec": current}), encoding="utf-8",
    )


def test_missing_baseline_is_unknown_not_a_deployment_default(tmp_path: Path) -> None:
    _metrics(tmp_path, 0.72)
    result = _rev_extraction_precision_regression_check("demo", programs_root=tmp_path)
    assert result.status == "warn"
    assert result.metadata["baseline_configured"] is False
    assert "VERTEX_REV_PRECISION_BASELINE" in result.detail
    assert "VERTEX_REV_PRECISION_REGRESSION_DELTA" in result.detail
    assert "floor" not in result.metadata


@pytest.mark.parametrize(
    ("baseline", "delta"),
    [("", "0.08"), ("0.72", ""), ("invalid", "0.08"), ("nan", "0.08"),
     ("inf", "0.08"), ("0.72", "nan"), ("0.72", "-0.1"), ("1.2", "0.08")],
)
def test_invalid_policy_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, baseline: str, delta: str,
) -> None:
    _metrics(tmp_path, 0.72)
    monkeypatch.setenv("VERTEX_REV_PRECISION_BASELINE", baseline)
    monkeypatch.setenv("VERTEX_REV_PRECISION_REGRESSION_DELTA", delta)
    result = _rev_extraction_precision_regression_check("demo", programs_root=tmp_path)
    assert result.status == "warn"
    assert result.metadata["baseline_configured"] is False


@pytest.mark.parametrize(("current", "status"), [(0.72, "ok"), (0.64, "ok"), (0.6399, "warn")])
def test_operator_values_preserve_strict_comparison_and_rounding(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, current: float, status: str,
) -> None:
    _metrics(tmp_path, current)
    monkeypatch.setenv("VERTEX_REV_PRECISION_BASELINE", "0.72")
    monkeypatch.setenv("VERTEX_REV_PRECISION_REGRESSION_DELTA", "0.08")
    result = _rev_extraction_precision_regression_check("demo", programs_root=tmp_path)
    assert result.status == status
    assert result.metadata["baseline"] == 0.72
    assert result.metadata["floor"] == 0.64
    assert "configured" in result.detail


def test_different_operator_values_are_not_replaced_by_public_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _metrics(tmp_path, 0.8889)
    monkeypatch.setenv("VERTEX_REV_PRECISION_BASELINE", "0.9123")
    monkeypatch.setenv("VERTEX_REV_PRECISION_REGRESSION_DELTA", "0.0234")
    result = _rev_extraction_precision_regression_check("demo", programs_root=tmp_path)
    assert result.status == "ok"
    assert result.metadata["baseline"] == 0.9123
    assert result.metadata["floor"] == 0.8889
