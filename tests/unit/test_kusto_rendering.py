from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from src.commands.chart_gather import _scrub_pii_rows
from src.core.config_loader import KustoQuerySettings, KustoSettings
from src.core.exceptions import AuthError, QueryError
from src.core.kusto_rendering import build_kusto_sections
from src.core.kusto_templates import KustoTemplateContext


@pytest.mark.parametrize("failure_type", [AuthError, QueryError])
@pytest.mark.parametrize("reference", [None, "https://example.invalid/dashboard"])
@pytest.mark.parametrize("render_as,required", [
    ("chart_image", False), ("chart_image", True),
    ("table", False), ("metric_highlight", False),
])
@pytest.mark.parametrize("fallback", [False, True])
def test_report_kusto_failure_omits_provider_text(
    failure_type, reference, render_as, required, fallback, caplog,
):
    from src.core.quality_gates import evaluate_chart_gates

    sample = "sample@example.invalid"
    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as=render_as, confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=reference,
        chart_config={"type": "line"} if render_as == "chart_image" else None,
        chart_blocks_publish=required,
        fallback_kql="fallback" if fallback else None,
    )
    calls = []

    def fail(current):
        calls.append(current.kql)
        if fallback and current.kql == "synthetic":
            return []
        raise failure_type(sample)

    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), fail,
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )

    assert sample not in str((sections, observations, warnings))
    assert sample not in caplog.text
    assert calls == (["synthetic", "fallback"] if fallback else ["synthetic"])
    assert observations[0].query_id == query.id
    assert observations[0].cluster == query.cluster
    assert observations[0].database == query.database
    assert observations[0].execution_state == "degraded"
    assert observations[0].last_successful_fetch_at is None
    assert observations[0].message and warnings
    assert query.id in warnings[0]
    assert bool(sections) is (reference is not None or required)
    if sections:
        assert sections[0].is_degraded
        assert sections[0].chart_blocks_publish is required
    if render_as == "chart_image":
        gate = next(result for result in evaluate_chart_gates(sections).results if result.gate_id == "QG-22")
        assert gate.passed is not required
        if required:
            assert not gate.forceable


@pytest.mark.parametrize("failure_type", [AuthError, QueryError])
def test_report_kusto_failure_does_not_stop_later_query(failure_type, caplog):
    sample = "sample@example.invalid"
    bad = KustoQuerySettings(
        id="bad", cluster="synthetic", database="synthetic", kql="bad",
        section="Synthetic", render_as="table", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
    )
    good = replace(bad, id="good", kql="good")

    def execute(query):
        if query.id == "bad":
            raise failure_type(sample)
        return [{"value": 1}]

    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(bad, good)), execute,
    )

    assert sample not in str((sections, observations, warnings))
    assert sample not in caplog.text
    assert [observation.execution_state for observation in observations] == ["degraded", "success"]
    assert observations[1].query_id == "good"
    assert sections[0].rows


@pytest.mark.parametrize("renderer_succeeds", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
def test_chart_renderer_table_fallback_and_cache_receive_scrubbed_rows(
    renderer_succeeds, fallback, tmp_path, monkeypatch,
):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    from src.core.chart_cache_store import load_chart_cache
    import src.core.kusto_rendering as rendering

    sample = "sample@example.invalid"
    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="primary",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_config={"type": "line"}, chart_renderer_id="synthetic::line",
        fallback_kql="fallback" if fallback else None,
    )
    registry = MagicMock()
    builder = registry.get_renderer.return_value
    builder.build.return_value = (b"synthetic-image", {}) if renderer_succeeds else None
    monkeypatch.setattr(rendering, "_VERTEX_CHARTS_ENABLED", True)

    def execute(current):
        return [] if fallback and current.kql == "primary" else [
            {"label": sample, "details": {"contact": sample}, "value": 2},
        ]

    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), execute,
        chart_registry=registry, chart_cache_loader=load_chart_cache, programs_root=tmp_path,
        program_id="synthetic", observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        chart_rows_scrubber=_scrub_pii_rows,
    )
    assert sample not in str(builder.build.call_args.args[2])
    assert sample not in str(sections)
    assert observations[0].execution_state == "success"
    if renderer_succeeds:
        cache_id = "synthetic__fallback" if fallback else "synthetic"
        cached = load_chart_cache("synthetic", cache_id, programs_root=tmp_path)
        assert sample not in str(cached.rows)
        assert sections[0].render_mode == "chart"
        assert not warnings
    else:
        assert sections[0].render_mode == "table"
        assert "[PII-FILTERED-EMAIL]" in str(sections[0].rows)
        assert warnings


@pytest.mark.parametrize("renderer_id", ["vertex::declarative", "core::deployment_velocity"])
@pytest.mark.parametrize("blocks_publish", [False, True])
def test_report_renderer_failure_keeps_table_gate_and_cache_without_exception_text(
    renderer_id, blocks_publish, tmp_path, monkeypatch, caplog,
):
    pytest.importorskip("matplotlib")
    from src.core.chart_cache_store import load_chart_cache, write_chart_cache
    from src.core.chart_renderer_registry import build_default_registry
    from src.core.quality_gates import evaluate_chart_gates
    import src.core.kusto_rendering as rendering

    sample = "renderer-sample@example.invalid"
    row_sample = "row-sample@example.invalid"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_config={"type": "line", "x_axis": "day", "y_axes": ["value"], "palette": [sample]},
        chart_renderer_id=renderer_id, chart_blocks_publish=blocks_publish,
    )
    existing = write_chart_cache(
        "synthetic", query.id, [{"value": 1}], now, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
    )
    before = existing.read_bytes()
    monkeypatch.setattr(rendering, "_VERTEX_CHARTS_ENABLED", True)
    caplog.set_level("WARNING")

    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)),
        lambda _: [{"day": "first", "value": 1, "label": row_sample}, {"day": "last", "value": 2}],
        chart_registry=build_default_registry(), chart_cache_loader=load_chart_cache,
        programs_root=tmp_path, program_id="synthetic", observed_at=now,
        chart_rows_scrubber=_scrub_pii_rows,
    )

    assert sections[0].render_mode == "table"
    assert sections[0].is_degraded is blocks_publish
    assert "[PII-FILTERED-EMAIL]" in str(sections[0].rows)
    assert row_sample not in str(sections)
    assert observations[0].execution_state == "success"
    assert warnings and "synthetic" in warnings[0]
    assert existing.read_bytes() == before
    gate = next(result for result in evaluate_chart_gates(sections, current_time=now).results
                if result.gate_id == "QG-22")
    assert gate.passed is not blocks_publish
    if blocks_publish:
        assert not gate.forceable
    assert sample not in str((sections, observations, warnings))
    assert sample not in caplog.text


@pytest.mark.parametrize("failure", ["missing", "exception", "sensitive_column"])
def test_chart_scrub_failure_degrades_without_rendering_or_replacing_cache(
    failure, tmp_path, monkeypatch, caplog,
):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from src.core.chart_cache_store import load_chart_cache, write_chart_cache
    import src.core.kusto_rendering as rendering

    sample = "sample@example.invalid"
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url="https://example.test/chart",
        chart_config={"type": "line"}, chart_renderer_id="synthetic::line",
    )
    existing = write_chart_cache(
        "synthetic", query.id, [{"value": 1}], now, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
    )
    before = existing.read_bytes()
    registry = MagicMock()
    builder = registry.get_renderer.return_value
    builder.build.return_value = (b"synthetic-image", {})
    monkeypatch.setattr(rendering, "_VERTEX_CHARTS_ENABLED", True)

    def fail(_rows):
        raise RuntimeError(sample)

    scrubber = None if failure == "missing" else fail if failure == "exception" else _scrub_pii_rows
    rows = [{sample: {"nested": "value"}}] if failure == "sensitive_column" else [{"label": sample}]
    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), lambda _: rows,
        chart_registry=registry, chart_cache_loader=load_chart_cache, programs_root=tmp_path,
        program_id="synthetic", observed_at=now, chart_rows_scrubber=scrubber,
    )
    builder.build.assert_not_called()
    assert sections[0].is_degraded
    assert not sections[0].rows
    assert observations[0].execution_state == "degraded"
    assert observations[0].last_successful_fetch_at is None
    assert warnings
    assert sample not in str((sections, observations, warnings))
    assert sample not in caplog.text
    assert existing.read_bytes() == before


@pytest.mark.parametrize("renderer_succeeds", [False, True])
@pytest.mark.parametrize("sample", [15555550199, "123456789"])
def test_nested_numeric_identifier_is_not_rendered_or_cached(
    sample, renderer_succeeds, monkeypatch, tmp_path, caplog,
):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from src.core.chart_cache_store import load_chart_cache, write_chart_cache
    from src.core.quality_gates import evaluate_chart_gates
    import src.core.kusto_rendering as rendering

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_config={"type": "line"}, chart_renderer_id="synthetic::line",
        chart_blocks_publish=True,
    )
    existing = write_chart_cache(
        "synthetic", query.id, [{"value": 1}], now, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
    )
    before = existing.read_bytes()
    registry = MagicMock()
    renderer = registry.get_renderer.return_value
    renderer.build.return_value = (b"synthetic-image", {}) if renderer_succeeds else None
    monkeypatch.setattr(rendering, "_VERTEX_CHARTS_ENABLED", True)

    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)),
        lambda _: [{"phone": {"value": sample}, "value": 1}],
        chart_registry=registry, chart_cache_loader=load_chart_cache, programs_root=tmp_path,
        program_id="synthetic", observed_at=now, chart_rows_scrubber=_scrub_pii_rows,
    )

    renderer.build.assert_not_called()
    assert observations[0].execution_state == "degraded"
    assert observations[0].last_successful_fetch_at is None
    assert sections[0].is_degraded and not sections[0].rows
    assert str(sample) not in str((sections, observations, warnings))
    assert str(sample) not in caplog.text
    assert existing.read_bytes() == before
    gate = next(result for result in evaluate_chart_gates(sections, current_time=now).results
                if result.gate_id == "QG-22")
    assert not gate.passed and not gate.forceable


@pytest.mark.parametrize("failure", [
    OSError(13, "Access denied", "sample@example.invalid"),
    ValueError("sample@example.invalid"),
])
def test_report_cache_failure_omits_exception_text(failure, tmp_path, monkeypatch, caplog):
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    import src.core.chart_cache_store as store
    import src.core.kusto_rendering as rendering

    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_config={"type": "line"}, chart_renderer_id="synthetic::line",
    )
    registry = MagicMock()
    registry.get_renderer.return_value.build.return_value = (b"synthetic-image", {})
    monkeypatch.setattr(rendering, "_VERTEX_CHARTS_ENABLED", True)
    monkeypatch.setattr(store, "write_chart_cache", MagicMock(side_effect=failure))

    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), lambda _: [{"value": 1}],
        chart_registry=registry, chart_cache_loader=lambda *_args, **_kwargs: None,
        programs_root=tmp_path, program_id="synthetic",
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc), chart_rows_scrubber=_scrub_pii_rows,
    )
    assert observations[0].execution_state == "success"
    assert sections[0].render_mode == "chart"
    assert not warnings
    assert "failed to write cache" in caplog.text
    assert "sample@example.invalid" not in caplog.text


@pytest.mark.parametrize("blocks_publish", [False, True])
def test_chart_scrub_failure_respects_publish_gate(blocks_publish):
    from datetime import datetime, timezone
    from src.core.quality_gates import evaluate_chart_gates

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_config={"type": "line"}, chart_blocks_publish=blocks_publish,
    )
    sections, observations, _ = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), lambda _: [{"value": 2}],
        observed_at=now,
    )
    gate = next(result for result in evaluate_chart_gates(sections, current_time=now).results
                if result.gate_id == "QG-22")
    assert observations[0].execution_state == "degraded"
    assert gate.passed is not blocks_publish
    if blocks_publish:
        assert not gate.forceable


@pytest.mark.parametrize("disabled_by", ["environment", "edition"])
@pytest.mark.parametrize("rows", [[], [{"label": "sample@example.invalid", "value": 2}]])
def test_disabled_chart_pipeline_renders_scrubbed_table(disabled_by, rows, monkeypatch):
    from unittest.mock import Mock
    import src.core.kusto_rendering as rendering

    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_config={"type": "line"}, chart_blocks_publish=True,
    )
    builder = Mock()
    monkeypatch.setattr(rendering, "_VERTEX_CHARTS_ENABLED", disabled_by != "environment")

    sections, observations, _ = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), lambda _: rows,
        chart_builder=builder, chart_rows_scrubber=_scrub_pii_rows,
        charts_enabled=disabled_by != "edition",
    )

    assert sections[0].render_mode == "table"
    assert "sample@example.invalid" not in str(sections)
    assert observations[0].execution_state == "success"
    builder.assert_not_called()


@pytest.mark.parametrize("blocks_publish", [False, True])
def test_chart_query_failure_without_reference_respects_publish_gate(blocks_publish):
    from src.core.exceptions import QueryError
    from src.core.quality_gates import evaluate_chart_gates

    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_blocks_publish=blocks_publish,
    )

    def fail(_query):
        raise QueryError("synthetic query failure")

    sections, observations, _ = build_kusto_sections(KustoSettings(enabled=True, queries=(query,)), fail)
    gate = next(result for result in evaluate_chart_gates(sections).results if result.gate_id == "QG-22")

    assert observations[0].execution_state == "degraded"
    assert gate.passed is not blocks_publish
    if blocks_publish:
        assert not gate.forceable


@pytest.mark.parametrize("blocks_publish", [False, True])
def test_chart_renderer_failure_respects_publish_gate(blocks_publish):
    from src.core.quality_gates import evaluate_chart_gates

    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_blocks_publish=blocks_publish,
    )
    sections, _, _ = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), lambda _: [{"value": 2}],
        chart_builder=lambda *_: None, chart_rows_scrubber=_scrub_pii_rows,
    )

    gate = next(result for result in evaluate_chart_gates(sections).results if result.gate_id == "QG-22")
    assert gate.passed is not blocks_publish
    if blocks_publish:
        assert not gate.forceable


def test_legacy_chart_builder_receives_scrubbed_rows():
    sample = "sample@example.invalid"
    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
    )
    seen = []

    def build(_query, rows):
        seen.extend(rows)
        return "data:image/png;base64,ZmFrZQ=="

    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), lambda _: [{"label": sample, "value": 2}],
        chart_builder=build, chart_rows_scrubber=_scrub_pii_rows,
    )
    assert sample not in str(seen)
    assert sample not in str(sections)
    assert sections[0].render_mode == "chart_image"
    assert observations[0].execution_state == "success"
    assert not warnings


@pytest.mark.parametrize(("size", "gate_passes"), [(102_400, True), (102_401, False)])
def test_legacy_chart_png_size_reaches_hard_gate(size: int, gate_passes: bool) -> None:
    import base64
    from src.core.quality_gates.chart import evaluate_chart_gates

    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
    )
    image = "data:image/png;base64," + base64.b64encode(b"x" * size).decode("ascii")
    sections, _, _ = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), lambda _: [{"label": "x", "value": 2}],
        chart_builder=lambda *_: image, chart_rows_scrubber=_scrub_pii_rows,
    )

    assert sections[0].chart_png_size_bytes == size
    gate = next(result for result in evaluate_chart_gates(sections).results if result.gate_id == "QG-21")
    assert gate.passed is gate_passes


@pytest.mark.parametrize("image", ["data:image/png;base64,%%", "https://example.test/chart.png"])
def test_invalid_legacy_chart_image_falls_back_to_blocking_table(image: str) -> None:
    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_blocks_publish=True,
    )
    sections, _, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), lambda _: [{"label": "x", "value": 2}],
        chart_builder=lambda *_: image, chart_rows_scrubber=_scrub_pii_rows,
    )

    assert sections[0].render_mode == "table"
    assert sections[0].is_degraded
    assert sections[0].chart_blocks_publish
    assert warnings


@pytest.mark.parametrize("renderer_id", ["vertex::declarative", "core::deployment_velocity"])
def test_actual_registry_chart_logs_nonfinite_auxiliary_cache_refusal(renderer_id, tmp_path, monkeypatch, caplog):
    from datetime import datetime, timezone
    from src.core.chart_cache_store import load_chart_cache, write_chart_cache
    from src.core.chart_renderer_registry import build_default_registry
    import src.core.kusto_rendering as rendering

    pytest.importorskip("matplotlib")
    monkeypatch.setattr(rendering, "_VERTEX_CHARTS_ENABLED", True)
    now = datetime.now(timezone.utc)
    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="primary",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_config={"type": "line", "x_axis": "day", "y_axes": ["p50"]},
        chart_renderer_id=renderer_id,
    )
    path = write_chart_cache(
        "synthetic", "synthetic", [{"day": "first", "p50": 7.0}], now, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
    )
    before = path.read_bytes()
    rows = [
        {"day": "first", "p50": 1.0, "aux": 1.0},
        {"day": "last", "p50": 2.0, "aux": float("inf")},
    ]

    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), lambda _: rows,
        chart_registry=build_default_registry(), observed_at=now,
        chart_cache_loader=load_chart_cache, programs_root=tmp_path, program_id="synthetic",
        chart_rows_scrubber=_scrub_pii_rows,
    )

    assert sections[0].render_mode == "chart"
    assert observations[0].execution_state == "success"
    assert warnings == ()
    assert "non-finite" in caplog.text
    assert path.read_bytes() == before


@pytest.mark.parametrize("fallback", [False, True])
def test_actual_registry_chart_uses_surviving_series_in_report(fallback, monkeypatch):
    from dataclasses import replace
    from datetime import datetime, timezone
    from src.core.chart_renderer_registry import ChartRendererRegistry
    import src.core.kusto_rendering as rendering

    pytest.importorskip("matplotlib")
    monkeypatch.setattr(rendering, "_VERTEX_CHARTS_ENABLED", True)
    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="primary",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_config={"type": "line", "x_axis": "day", "y_axes": ["missing", "p50", "p75"]},
        fallback_kql="fallback" if fallback else None,
    )
    rows = [{"day": f"d{i}", "p50": float(i + 1), "p75": float(i + 2)} for i in range(6)]

    def execute(current):
        return [] if fallback and current.kql == "primary" else rows

    def render(current):
        return build_kusto_sections(
            KustoSettings(enabled=True, queries=(current,)), execute,
            chart_registry=ChartRendererRegistry(),
            observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            chart_rows_scrubber=_scrub_pii_rows,
        )

    sections, observations, warnings = render(query)
    expected, _, expected_warnings = render(replace(
        query, chart_config={"type": "line", "x_axis": "day", "y_axes": ["p50", "p75"]},
    ))

    assert warnings == expected_warnings == ()
    assert observations[0].execution_state == "success"
    assert sections[0].render_mode == "chart"
    assert sections[0].message.startswith("p50 trending ")
    assert sections[0].message == expected[0].message
    assert sections[0].chart_png_base64 == expected[0].chart_png_base64
    assert sections[0].chart_png_base64


@pytest.mark.parametrize("renderer_id", ["vertex::declarative", "core::deployment_velocity"])
@pytest.mark.parametrize("value", [float("nan"), Decimal("NaN"), Decimal("sNaN"), Decimal("1e10000"), Decimal("1e-10000")])
def test_actual_registry_chart_falls_back_to_table_for_nonfinite_value(renderer_id, value, monkeypatch):
    from datetime import datetime, timezone
    from unittest.mock import Mock
    from src.core.chart_renderer_registry import build_default_registry
    import src.core.kusto_rendering as rendering

    pytest.importorskip("matplotlib")
    monkeypatch.setattr(rendering, "_VERTEX_CHARTS_ENABLED", True)
    write_cache = Mock()
    monkeypatch.setattr(rendering, "_write_back_chart_cache", write_cache)
    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="primary",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_config={"type": "line", "x_axis": "day", "y_axes": ["p50"]},
        chart_renderer_id=renderer_id,
    )
    rows = [{"day": "first", "p50": 1.0}, {"day": "last", "p50": value}]

    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), lambda _: rows,
        chart_registry=build_default_registry(),
        observed_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        chart_rows_scrubber=_scrub_pii_rows,
    )

    assert len(sections) == 1
    assert sections[0].render_mode == "table"
    assert sections[0].image_data_url is None
    assert observations[0].execution_state == "success"
    assert warnings == ("Kusto chart rendering fell back to table for synthetic.",)
    write_cache.assert_not_called()


@pytest.mark.parametrize("renderer_id", ["vertex::declarative", "core::deployment_velocity"])
def test_actual_registry_chart_plots_decimals_without_overwriting_cache(renderer_id, tmp_path, monkeypatch, caplog):
    from datetime import datetime, timezone
    from src.core.chart_cache_store import write_chart_cache
    from src.core.chart_renderer_registry import build_default_registry
    from src.core.kusto_result_limits import KustoResultLimits
    import src.core.kusto_rendering as rendering

    pytest.importorskip("matplotlib")
    monkeypatch.setattr(rendering, "_VERTEX_CHARTS_ENABLED", True)
    now = datetime.now(timezone.utc)
    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="primary",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_config={"type": "line", "x_axis": "day", "y_axes": ["p50"]},
        chart_renderer_id=renderer_id, result_limits=KustoResultLimits(("day", "p50"), 2, 1000),
    )
    rows = [{"day": "first", "p50": Decimal("1.25")}, {"day": "last", "p50": Decimal("2.5")}]
    float_rows = [{"day": row["day"], "p50": float(row["p50"])} for row in rows]
    registry = build_default_registry()
    settings = KustoSettings(enabled=True, queries=(query,))
    expected, _, _ = build_kusto_sections(
        settings, lambda _: float_rows, chart_registry=registry, observed_at=now, chart_rows_scrubber=_scrub_pii_rows,
    )
    cache_path = write_chart_cache(
        "synthetic", "synthetic", float_rows, now, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
    )
    before = cache_path.read_bytes()

    sections, observations, warnings = build_kusto_sections(
        settings, lambda _: rows, chart_registry=registry, observed_at=now,
        chart_cache_loader=lambda *_args, **_kwargs: None, programs_root=tmp_path, program_id="synthetic",
        chart_rows_scrubber=_scrub_pii_rows,
    )

    assert sections[0].render_mode == "chart"
    assert sections[0].chart_png_base64 == expected[0].chart_png_base64
    assert sections[0].message == expected[0].message
    assert observations[0].execution_state == "success"
    assert warnings == ()
    assert "Decimal values cannot be stored losslessly" in caplog.text
    assert cache_path.read_bytes() == before


@pytest.mark.parametrize("fallback", [False, True])
def test_actual_report_cache_write_binds_executed_definition(fallback, tmp_path, monkeypatch):
    from dataclasses import replace
    from datetime import datetime, timezone
    from unittest.mock import MagicMock
    import src.core.kusto_rendering as rendering
    from src.core.chart_cache_store import chart_cache_contract_identity, load_chart_cache
    from src.core.models_v2 import KustoQuery

    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="primary",
        section="Synthetic", render_as="chart_image", confidence="high",
        kusto_section_validates_slice=False, caveats=(), reference_url=None,
        chart_config={"type": "line"}, chart_renderer_id="synthetic::line",
        fallback_kql="fallback" if fallback else None,
    )
    registry = MagicMock()
    registry.get_renderer.return_value.build.return_value = (b"synthetic-image", {})
    monkeypatch.setattr(rendering, "_VERTEX_CHARTS_ENABLED", True)

    def execute(current):
        return [] if fallback and current.kql == "primary" else [{"Value": 0}]

    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), execute,
        chart_registry=registry, chart_cache_loader=load_chart_cache, programs_root=tmp_path,
        program_id="synthetic", observed_at=datetime.now(timezone.utc),
        chart_rows_scrubber=_scrub_pii_rows,
    )
    actual = replace(query, id="synthetic__fallback", kql="fallback") if fallback else query
    entry = load_chart_cache("synthetic", actual.id, programs_root=tmp_path)
    assert entry.query_contract_hash == chart_cache_contract_identity(actual)
    equivalent = KustoQuery(
        id=actual.id, cluster=actual.cluster, database=actual.database, kql=actual.kql,
        section=actual.section, render_as=actual.render_as, confidence=actual.confidence,
        chart_config=actual.chart_config, chart_renderer_id=actual.chart_renderer_id,
    )
    assert entry.query_contract_hash == chart_cache_contract_identity(equivalent)
    from src.commands.chart_gather import _gather_single_chart

    def refuse_live(_query):
        pytest.fail("A matching report-produced cache should be reusable without another execution")

    result = _gather_single_chart(
        equivalent, "synthetic", refuse_live, datetime.now(timezone.utc), load_chart_cache, tmp_path,
    )
    assert result.cache_hit
    assert result.error is None
    if fallback:
        assert entry.query_contract_hash != chart_cache_contract_identity(query)
    assert sections
    assert observations[0].execution_state == "success"
    assert not warnings


@pytest.mark.parametrize("failure", ["rows", "bytes", "schema", "scalar", "query", "auth"])
def test_fallback_refusal_is_degraded_before_any_chart_or_output(failure: str) -> None:
    from src.core.exceptions import QueryError
    from src.core.kusto_result_limits import KustoResultLimits

    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="primary", section="Synthetic",
        render_as="chart_image", confidence="high", kusto_section_validates_slice=False,
        caveats=(), reference_url="https://example.test/telemetry", fallback_kql="fallback",
        result_limits=KustoResultLimits(("Value",), 2 if failure == "scalar" else 1, 2 if failure == "bytes" else 100),
        expected_cardinality="scalar_required" if failure == "scalar" else "zero_ok",
    )
    calls: list[str] = []

    def execute(current):
        calls.append(current.kql)
        if current.kql == "primary":
            # A scalar-required primary is invalid even before fallback.
            return [{"Value": 1}, {"Value": 2}] if failure == "scalar" else []
        if failure == "query":
            raise QueryError("synthetic refused fallback")
        if failure == "auth":
            raise AuthError("synthetic unavailable identity")
        return [{"Other" if failure == "schema" else "Value": value} for value in ([1, 2] if failure == "rows" else [1])]

    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)), execute,
        chart_builder=lambda *_args: pytest.fail("Refused rows must never reach chart rendering"),
    )
    assert calls == (["primary"] if failure == "scalar" else ["primary", "fallback"])
    assert sections[0].is_degraded
    assert observations[0].execution_state == "degraded"
    assert observations[0].last_successful_fetch_at is None
    assert warnings


@pytest.mark.parametrize("fallback", ["valid", "empty", "legacy_error"])
def test_declared_fallback_preserves_valid_empty_and_legacy_failure_behavior(fallback: str) -> None:
    from src.core.kusto_result_limits import KustoResultLimits

    query = KustoQuerySettings(
        id="synthetic", cluster="synthetic", database="synthetic", kql="primary", section="Synthetic",
        render_as="table", confidence="high", kusto_section_validates_slice=False,
        caveats=(), reference_url=None, fallback_kql="fallback", result_limits=KustoResultLimits(("Value",), 1, 100),
    )
    calls: list[str] = []

    def execute(current):
        calls.append(current.kql)
        if current.kql == "primary":
            return []
        if fallback == "legacy_error":
            raise RuntimeError("Pre-existing noncontract failure")
        return [{"Value": 0}] if fallback == "valid" else []

    sections, observations, warnings = build_kusto_sections(KustoSettings(enabled=True, queries=(query,)), execute)
    assert calls == ["primary", "fallback"]
    assert not warnings
    if fallback == "valid":
        assert observations[0].query_id == "synthetic__fallback"
        assert observations[0].execution_state == "success"
        assert sections[0].rows
    else:
        assert observations[0].execution_state == "empty"


def test_build_kusto_sections_supports_all_render_modes() -> None:
    settings = KustoSettings(
        enabled=True,
        queries=(
            KustoQuerySettings(
                id="table-query",
                cluster="https://cluster.kusto.windows.net",
                database="telemetry",
                kql="table query",
                section="Incident Summary",
                render_as="table",
                confidence="medium",
                kusto_section_validates_slice=False,
                caveats=("Synthetic data",),
                reference_url="https://dash.example/incidents",
            ),
            KustoQuerySettings(
                id="metric-query",
                cluster="https://cluster.kusto.windows.net",
                database="telemetry",
                kql="metric query",
                section="Latency Snapshot",
                render_as="metric_highlight",
                confidence="high",
                kusto_section_validates_slice=True,
                caveats=(),
                reference_url=None,
            ),
            KustoQuerySettings(
                id="chart-query",
                cluster="https://cluster.kusto.windows.net",
                database="telemetry",
                kql="chart query",
                section="Fleet Health",
                render_as="chart_image",
                confidence="high",
                kusto_section_validates_slice=False,
                caveats=(),
                reference_url="https://dash.example/fleet",
            ),
        ),
    )

    sections, observations, warnings = build_kusto_sections(
        settings,
        _query_results,
        chart_builder=lambda query, rows: "data:image/png;base64,ZmFrZQ==" if query.id == "chart-query" else None,
        observed_at=__import__("datetime", fromlist=["datetime", "timezone"]).datetime(2026, 5, 7, 18, 0, tzinfo=__import__("datetime", fromlist=["timezone"]).timezone.utc),
        chart_rows_scrubber=_scrub_pii_rows,
    )

    assert warnings == ()
    assert [section.render_mode for section in sections] == ["table", "metric_highlight", "chart_image"]
    assert [observation.execution_state for observation in observations] == ["success", "success", "success"]
    assert observations[1].kusto_section_validates_slice is True
    assert sections[0].rows[0][-1].href == "https://portal.microsofticm.com/imp/v3/incidents/details/12345"
    assert sections[1].metrics[0].label == "P50 Hours"
    assert sections[1].metrics[0].value == "4.2"
    assert sections[2].image_data_url == "data:image/png;base64,ZmFrZQ=="


def test_build_kusto_sections_degrades_auth_failures_to_reference_links() -> None:
    settings = KustoSettings(
        enabled=True,
        queries=(
            KustoQuerySettings(
                id="mttr-query",
                cluster="https://cluster.kusto.windows.net",
                database="telemetry",
                kql="mttr query",
                section="Incident MTTR",
                render_as="metric_highlight",
                confidence="medium",
                kusto_section_validates_slice=False,
                caveats=("Auth required",),
                reference_url="https://dash.example/mttr",
            ),
            KustoQuerySettings(
                id="auth-no-link",
                cluster="https://cluster.kusto.windows.net",
                database="telemetry",
                kql="auth no link",
                section="Hidden Section",
                render_as="table",
                confidence="low",
                kusto_section_validates_slice=False,
                caveats=(),
                reference_url=None,
            ),
        ),
    )

    sections, observations, warnings = build_kusto_sections(settings, _auth_fail_results)

    assert len(sections) == 1
    assert [observation.execution_state for observation in observations] == ["degraded", "degraded"]
    assert sections[0].is_degraded is True
    assert sections[0].reference_url == "https://dash.example/mttr"
    assert "Run vertex admin auth setup" in sections[0].message
    assert any("mttr-query" in warning for warning in warnings)
    assert any("auth-no-link" in warning for warning in warnings)


def test_build_kusto_sections_renders_query_templates_before_execution() -> None:
    settings = KustoSettings(
        enabled=True,
        queries=(
            KustoQuerySettings(
                id="templated-query",
                cluster="https://cluster.kusto.windows.net",
                database="telemetry",
                kql='Telemetry | where Program == "{program_id}" | where AreaPath == "{area_path}" | where Timestamp > ago({date_range}) | take 1',
                section="Telemetry",
                render_as="table",
                confidence="medium",
                kusto_section_validates_slice=False,
                caveats=(),
                reference_url=None,
            ),
        ),
    )
    rendered_kql: list[str] = []

    def _capture_query(query: KustoQuerySettings) -> list[dict[str, object]]:
        rendered_kql.append(query.kql)
        return [{"Count": 1}]

    build_kusto_sections(
        settings,
        _capture_query,
        template_context=KustoTemplateContext(
            program_id="acme",
            area_paths=("One\\Adventure\\Acme",),
            date_window_days=14,
        ),
    )

    assert rendered_kql == [
        'Telemetry | where Program == "acme" | where AreaPath == "One\\Adventure\\Acme" | where Timestamp > ago(14d) | take 1'
    ]


def _query_results(query: KustoQuerySettings) -> list[dict[str, object]]:
    if query.id == "table-query":
        return [
            {
                "IncidentId": "ICM-12345",
                "Severity": "3",
                "Title": "Fleet capacity alert",
                "IncidentUrl": "https://portal.microsofticm.com/imp/v3/incidents/details/12345",
            }
        ]
    if query.id == "metric-query":
        return [{"P50Hours": 4.2, "P90Hours": 7.8}]
    if query.id == "chart-query":
        return [
            {"Date": "2026-05-01", "HealthyPct": 98.2},
            {"Date": "2026-05-02", "HealthyPct": 98.9},
        ]
    raise AssertionError(f"Unexpected query id: {query.id}")


def _auth_fail_results(query: KustoQuerySettings) -> list[dict[str, object]]:
    raise AuthError(f"credential unavailable for {query.id}")
