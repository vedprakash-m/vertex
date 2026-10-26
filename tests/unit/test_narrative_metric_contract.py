from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.core import metric_registry, narrative_store, reality_store
from src.core.exceptions import ConfigError
from src.core.metric_models import MetricAggregation, MetricDefinition, MetricObservation, MetricQualityState


AS_OF = datetime(2026, 10, 24, tzinfo=timezone.utc)


def _prepare(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, unit: str | None,
    numeric: float | None = 47.3, text: str | None = None, observed: bool = True,
) -> Path:
    monkeypatch.setattr(
        "src.core.edition_resolver.resolve_edition_paths",
        Mock(return_value=SimpleNamespace(program_id="fixture")),
    )
    monkeypatch.setattr("src.core.overrides_store.load_overrides", Mock(return_value=None))
    monkeypatch.setattr("src.core.section_proposal_store.load_hint_proposals", Mock(return_value=()))
    definitions = {} if unit is None else {
        "fixture.metric": MetricDefinition(
            id="fixture.metric", title="Synthetic metric", unit=unit,
            aggregation=MetricAggregation.LAST, valid_from=AS_OF,
        ),
    }
    monkeypatch.setattr(metric_registry, "load_metric_definition_map", Mock(return_value=definitions))
    store = Mock()
    store.list_metric_observations.return_value = (
        MetricObservation(
            observation_id="fixture-observation", program_id="fixture", metric_id="fixture.metric",
            dimensions_json="{}", measurement_period_start=AS_OF, measurement_period_end=AS_OF,
            observed_at=AS_OF, value_num=numeric, value_text=text, sample_count=1,
            quality_state=MetricQualityState.OK,
        ),
    ) if observed else ()
    monkeypatch.setattr(reality_store, "RealityStore", Mock(return_value=store))
    return tmp_path / "reports"


@pytest.mark.parametrize("unit", [None, "", " \t "])
@pytest.mark.parametrize("numeric,text", [(47.3, None), (None, "measured")])
def test_material_metric_without_defined_unit_never_becomes_an_implicit_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    unit: str | None, numeric: float | None, text: str | None,
) -> None:
    reports_root = _prepare(tmp_path, monkeypatch, unit=unit, numeric=numeric, text=text)
    with pytest.raises(ConfigError, match="fixture.metric.*explicit unit"):
        narrative_store.inject_narrative_placeholders(
            "fixture", 7, {"summary.md": "<!-- vertex:metric: fixture.metric -->"},
            reports_root=reports_root,
        )
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("unit,expected", [
    ("count", "47.3"), ("mins", "47.3 min"), ("pct", "47.3%"),
    ("bytes", "47.3 bytes"), (" Count ", "47.3"),
])
def test_explicit_metric_units_preserve_supported_rendering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unit: str, expected: str,
) -> None:
    reports_root = _prepare(tmp_path, monkeypatch, unit=unit)
    assert narrative_store.inject_narrative_placeholders(
        "fixture", 7, {"summary.md": "<!-- vertex:metric: fixture.metric -->"},
        reports_root=reports_root,
    ) == {"summary.md": expected}
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("observed", [False, True])
def test_missing_metric_value_stays_pending_without_inventing_units(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, observed: bool,
) -> None:
    reports_root = _prepare(tmp_path, monkeypatch, unit=None, numeric=None, observed=observed)
    assert narrative_store.inject_narrative_placeholders(
        "fixture", 7, {"summary.md": "<!-- vertex:metric: fixture.metric -->"},
        reports_root=reports_root,
    ) == {"summary.md": "[value pending]"}
    assert not list(tmp_path.iterdir())


def test_explicit_zero_is_not_missing_metric_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reports_root = _prepare(tmp_path, monkeypatch, unit="pct", numeric=0.0)
    assert narrative_store.inject_narrative_placeholders(
        "fixture", 7, {"summary.md": "<!-- vertex:metric: fixture.metric -->"},
        reports_root=reports_root,
    ) == {"summary.md": "0%"}
    assert not list(tmp_path.iterdir())
