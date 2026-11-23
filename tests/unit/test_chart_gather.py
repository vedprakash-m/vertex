"""Unit tests for chart_gather.py — spec §11."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.core.kusto_result_limits import KustoResultLimits
from src.core.integration_types import KustoResultColumn, KustoResultKind, KustoResultSchema, KustoResultSet
from src.core.models_v2 import KustoQuery
from src.core.exceptions import AuthError, QueryError
from src.commands.chart_gather import (
    ChartGatherResult,
    _eviction_grace_hours_for_ttl,
    _scrub_pii_rows,
    _sanitize_rows,
    _truncate_rows,
    gather_chart_data,
)
from src.core.chart_cache_store import chart_cache_contract_identity, load_chart_cache, write_chart_cache

_NOW = datetime(2026, 5, 28, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("failure_type", [AuthError, QueryError])
def test_chart_gather_failure_omits_provider_text_and_continues(
    failure_type, tmp_path, monkeypatch, caplog,
):
    import src.commands.chart_gather as cg

    sample = "sample@example.invalid"
    bad = KustoQuery(
        id="bad", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high",
        chart_config={"type": "line"},
    )
    good = replace(bad, id="good")
    existing = write_chart_cache(
        "synthetic", bad.id, [{"value": 7}], _NOW, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
    )
    before = existing.read_bytes()

    def execute(query):
        if query.id == "bad":
            raise failure_type(sample)
        return [{"value": 1}]

    monkeypatch.setattr(cg, "_VERTEX_CHARTS_ENABLED", True)
    monkeypatch.setattr(cg, "_load_chart_queries", lambda *_args, **_kwargs: (bad, good))
    monkeypatch.setattr(cg, "_evict_chart_caches", lambda *_args: 0)
    sink = []
    results, errors = gather_chart_data(
        "synthetic", programs_root=tmp_path, program=None, workstreams=(),
        executor=execute, current_time=_NOW, integration_error_sink=sink,
    )

    assert sample not in str((results, errors, sink))
    assert sample not in caplog.text
    assert results[0].rows_written is None and results[0].error
    assert ("authentication required" if failure_type is AuthError else "query unavailable") in results[0].error
    assert existing.read_bytes() == before
    assert len(errors) == 1 and errors[0].retryable
    assert sink == list(errors)
    assert results[1].error is None and results[1].rows_written == 1


@pytest.mark.parametrize("failure", ["rows", "bytes", "schema", "scalar"])
def test_chart_gather_refuses_before_sanitization_or_cache_write(failure: str, tmp_path, monkeypatch):
    import src.commands.chart_gather as cg
    from src.core.chart_cache_store import write_chart_cache

    now = datetime.now(timezone.utc)
    existing = write_chart_cache(
        "synthetic", "synthetic", [{"Value": 7}], now, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
    )
    before = existing.read_bytes()
    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
        result_limits=KustoResultLimits(("Value",), 2 if failure == "scalar" else 1, 2 if failure == "bytes" else 100),
        expected_cardinality="scalar_required" if failure == "scalar" else "zero_ok",
    )
    monkeypatch.setattr(cg, "_VERTEX_CHARTS_ENABLED", True)
    monkeypatch.setattr(cg, "_load_chart_queries", lambda *_args, **_kwargs: (query,))
    monkeypatch.setattr(cg, "_sanitize_rows", lambda *_args: pytest.fail("Refused rows cannot reach sanitization"))
    monkeypatch.setattr(cg, "write_chart_cache", lambda **_kwargs: pytest.fail("Refused rows cannot overwrite cache"))
    rows = [{"Other" if failure == "schema" else "Value": value} for value in ([1, 2] if failure in {"rows", "scalar"} else [1])]
    error_sink = []
    results, errors = gather_chart_data(
        "synthetic", programs_root=tmp_path, program=_make_mock_program(), workstreams=(),
        executor=lambda _query: rows, current_time=now, integration_error_sink=error_sink,
    )
    assert results[0].rows_written is None
    assert results[0].error
    assert error_sink == list(errors)
    assert len(errors) == 1
    assert existing.read_bytes() == before


def test_chart_gather_admits_typed_kusto_result(tmp_path, monkeypatch):
    import src.commands.chart_gather as cg

    query = KustoQuery(
        id="typed", cluster="synthetic", database="synthetic", kql="Metrics | take 1",
        section="Synthetic", render_as="chart_image", confidence="high",
        chart_config={"type": "line"}, result_column="Value",
    )
    schema = KustoResultSchema(
        kind=KustoResultKind.TABLE,
        columns=(
            KustoResultColumn("Timestamp", "datetime"),
            KustoResultColumn("Value", "real"),
        ),
        row_count=1,
    )
    typed = KustoResultSet(
        query_id=query.id,
        rows=({"Timestamp": "2026-05-28T12:00:00Z", "Value": 7.0},),
        observed_at=_NOW,
        row_count=1,
        schema=schema,
    )
    monkeypatch.setattr(cg, "_VERTEX_CHARTS_ENABLED", True)
    monkeypatch.setattr(cg, "_load_chart_queries", lambda *_args, **_kwargs: (query,))
    monkeypatch.setattr(cg, "_evict_chart_caches", lambda *_args: 0)

    results, errors = gather_chart_data(
        "synthetic",
        programs_root=tmp_path,
        program=None,
        workstreams=(),
        executor=lambda _query: typed,
        current_time=_NOW,
    )

    assert errors == ()
    assert results[0].rows_written == 1
    assert results[0].error is None


@pytest.mark.parametrize("declared", [False, True])
def test_chart_gather_valid_rows_reach_actual_cache_unchanged(declared: bool, tmp_path, monkeypatch):
    import src.commands.chart_gather as cg

    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
        result_limits=KustoResultLimits(("Value",), 1, 100) if declared else None,
    )
    monkeypatch.setattr(cg, "_scrub_pii_rows", lambda rows: rows)
    result = cg._gather_single_chart(
        query, "synthetic", lambda _query: [{"Value": 0}], _NOW, None, tmp_path,
    )
    assert result.error is None
    assert result.rows_written == 1
    cached = load_chart_cache("synthetic", query.id, programs_root=tmp_path)
    assert cached.rows == ({"Value": 0},)


def test_chart_gather_refuses_decimal_cache_without_replacing_previous_entry(tmp_path, monkeypatch):
    import src.commands.chart_gather as cg

    now = datetime.now(timezone.utc)
    decimal_query = KustoQuery(
        id="decimal", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
        result_limits=KustoResultLimits(("Value",), 1, 100),
    )
    valid_query = replace(decimal_query, id="valid")
    existing = write_chart_cache(
        "synthetic", "decimal", [{"Value": 7}], now, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
    )
    before = existing.read_bytes()
    monkeypatch.setattr(cg, "_VERTEX_CHARTS_ENABLED", True)
    monkeypatch.setattr(cg, "_load_chart_queries", lambda *_args, **_kwargs: (decimal_query, valid_query))
    monkeypatch.setattr(cg, "_scrub_pii_rows", lambda rows: rows)
    error_sink = []

    results, errors = gather_chart_data(
        "synthetic", programs_root=tmp_path, program=_make_mock_program(), workstreams=(),
        executor=lambda query: [{"Value": Decimal("1.25") if query.id == "decimal" else 3.0}],
        current_time=now, integration_error_sink=error_sink,
    )

    assert results[0].rows_written is None
    assert not results[0].cache_hit
    assert "Decimal values cannot be stored losslessly" in results[0].error
    assert existing.read_bytes() == before
    assert results[1].error is None
    assert results[1].rows_written == 1
    assert load_chart_cache("synthetic", "valid", programs_root=tmp_path).rows == ({"Value": 3.0},)
    assert len(errors) == 1
    assert error_sink == list(errors)


@pytest.mark.parametrize("failure", [
    OSError(13, "Access denied", "sample@example.invalid"),
    ValueError("sample@example.invalid"),
])
def test_chart_gather_cache_failure_omits_exception_text_and_continues(failure, tmp_path, monkeypatch, caplog):
    import src.commands.chart_gather as cg

    bad = KustoQuery(
        id="bad", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
    )
    good = replace(bad, id="good")
    original_write = cg.write_chart_cache

    def write(*args, **kwargs):
        if kwargs["query_id"] == bad.id:
            raise failure
        return original_write(*args, **kwargs)

    monkeypatch.setattr(cg, "_VERTEX_CHARTS_ENABLED", True)
    monkeypatch.setattr(cg, "_load_chart_queries", lambda *_args, **_kwargs: (bad, good))
    monkeypatch.setattr(cg, "write_chart_cache", write)
    sink = []
    results, errors = gather_chart_data(
        "synthetic", programs_root=tmp_path, program=None, workstreams=(),
        executor=lambda _: [{"value": 1}], current_time=_NOW, integration_error_sink=sink,
    )
    assert results[0].error and results[0].rows_written is None
    assert "sample@example.invalid" not in str((results, errors, sink))
    assert "sample@example.invalid" not in caplog.text
    assert errors and sink == list(errors)
    assert results[1].error is None and results[1].rows_written == 1


def test_chart_cache_eviction_failure_omits_exception_text(tmp_path, monkeypatch, caplog):
    import src.commands.chart_gather as cg

    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
    )
    monkeypatch.setattr(cg, "_VERTEX_CHARTS_ENABLED", True)
    monkeypatch.setattr(cg, "_load_chart_queries", lambda *_args, **_kwargs: (query,))
    monkeypatch.setattr(
        cg, "_evict_chart_caches",
        MagicMock(side_effect=OSError(13, "Access denied", "sample@example.invalid")),
    )
    results, errors = gather_chart_data(
        "synthetic", programs_root=tmp_path, program=None, workstreams=(),
        executor=lambda _: [{"value": 1}], current_time=_NOW,
    )
    assert results[0].error is None and not errors
    assert "cache eviction failed" in caplog.text
    assert "sample@example.invalid" not in caplog.text


@pytest.mark.parametrize(
    ("token", "live_refusal"),
    [("NaN", False), ("Infinity", False), ("-Infinity", False), ("1e10000", False),
     ("NaN", True), ("1e10000", True)],
)
def test_chart_gather_refreshes_nonfinite_cache_or_preserves_failed_replacement(
    token, live_refusal, tmp_path, monkeypatch,
):
    import json
    import src.commands.chart_gather as cg

    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
    )
    path = write_chart_cache(
        "synthetic", query.id, [{"Value": 7.0}], _NOW, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
        query_contract_hash=chart_cache_contract_identity(query),
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["rows"] = [{"Value": "REPLACE"}]
    path.write_text(json.dumps(payload).replace('"REPLACE"', token), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(cg, "_scrub_pii_rows", lambda rows: rows)
    executor = MagicMock(return_value=[{"Value": float("inf") if live_refusal else 0.0}])

    result = cg._gather_single_chart(query, "synthetic", executor, _NOW, load_chart_cache, tmp_path)

    executor.assert_called_once_with(query)
    assert not result.cache_hit
    if live_refusal:
        assert "non-finite" in result.error
        assert result.rows_written is None
        assert path.read_bytes() == before
    else:
        assert result.error is None
        assert result.rows_written == 1
        assert load_chart_cache("synthetic", query.id, programs_root=tmp_path).rows == ({"Value": 0.0},)


def test_chart_gather_refuses_nonfinite_cache_and_continues_valid_query(tmp_path, monkeypatch):
    import src.commands.chart_gather as cg

    now = datetime.now(timezone.utc)
    bad = KustoQuery(
        id="bad", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
    )
    good = replace(bad, id="good")
    path = write_chart_cache(
        "synthetic", bad.id, [{"Value": 7.0}], now, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
    )
    before = path.read_bytes()
    monkeypatch.setattr(cg, "_VERTEX_CHARTS_ENABLED", True)
    monkeypatch.setattr(cg, "_load_chart_queries", lambda *_args, **_kwargs: (bad, good))
    monkeypatch.setattr(cg, "_scrub_pii_rows", lambda rows: rows)
    sink = []

    results, errors = gather_chart_data(
        "synthetic", programs_root=tmp_path, program=_make_mock_program(), workstreams=(),
        executor=lambda query: [{"Value": float("inf") if query.id == bad.id else 3.0}],
        current_time=now, integration_error_sink=sink,
    )

    assert results[0].rows_written is None
    assert "non-finite" in results[0].error
    assert path.read_bytes() == before
    assert results[1].error is None
    assert load_chart_cache("synthetic", good.id, programs_root=tmp_path).rows == ({"Value": 3.0},)
    assert len(errors) == 1
    assert sink == list(errors)


@pytest.mark.parametrize(
    ("age_hours", "contract", "cache_hit"),
    [(0, "legacy", True), (1, "legacy", True), (25.999, "legacy", True),
     (26, "legacy", False), (27, "legacy", False),
     (-1, "legacy", False), (1, "limits", False), (1, "scalar", False)],
)
def test_chart_gather_cache_admission_uses_selected_root_and_contract(
    age_hours, contract, cache_hit, tmp_path, monkeypatch,
):
    import src.commands.chart_gather as cg

    now = datetime.now(timezone.utc)
    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
        chart_cache_ttl_hours=26,
        result_limits=KustoResultLimits(("Value",), 1, 100) if contract == "limits" else None,
        expected_cardinality="scalar_required" if contract == "scalar" else "zero_ok",
    )
    selected = tmp_path / "selected"
    other = tmp_path / "other"
    existing = write_chart_cache(
        "synthetic", query.id, [{"Value": 7}], now - timedelta(hours=age_hours), "synthetic",
        programs_root=selected, pii_prescrubbed=True,
        query_contract_hash=chart_cache_contract_identity(query),
    )
    unrelated = write_chart_cache(
        "synthetic", query.id, [{"Value": 99}], now, "synthetic",
        programs_root=other, pii_prescrubbed=True,
    )
    before = existing.read_bytes()
    unrelated_before = unrelated.read_bytes()
    monkeypatch.setattr(cg, "_VERTEX_CHARTS_ENABLED", True)
    monkeypatch.setattr(cg, "_load_chart_queries", lambda *_args, **_kwargs: (query,))
    monkeypatch.setattr(cg, "_scrub_pii_rows", lambda rows: rows)
    executor = MagicMock(return_value=[{"Value": 0}])
    results, errors = gather_chart_data(
        "synthetic", programs_root=selected, program=_make_mock_program(), workstreams=(),
        executor=executor, current_time=now,
    )
    assert not errors
    assert results[0].error is None
    assert results[0].cache_hit is cache_hit
    assert executor.call_count == (0 if cache_hit else 1)
    assert (existing.read_bytes() == before) is cache_hit
    assert unrelated.read_bytes() == unrelated_before
    cached = load_chart_cache("synthetic", query.id, programs_root=selected)
    assert cached.rows == ({"Value": 7 if cache_hit else 0},)


@pytest.mark.parametrize(
    ("age_hours", "contract", "cache_hit"),
    [(0, "legacy", True), (1, "legacy", True), (25.999, "legacy", True),
     (26, "legacy", False), (27, "legacy", False),
     (-1, "legacy", False), (1, "limits", False), (1, "scalar", False)],
)
def test_chart_gather_cache_admission_independent_of_loader_signature(
    age_hours, contract, cache_hit, tmp_path, monkeypatch,
):
    import src.commands.chart_gather as cg
    from src.core.chart_cache_store import ChartCacheEntry

    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
        chart_cache_ttl_hours=26,
        result_limits=KustoResultLimits(("Value",), 1, 100) if contract == "limits" else None,
        expected_cardinality="scalar_required" if contract == "scalar" else "zero_ok",
    )
    entry = ChartCacheEntry(
        program_id="synthetic", query_id=query.id, captured_at=_NOW - timedelta(hours=age_hours),
        chart_config_hash="synthetic", rows=({"Value": 7},), row_count=1,
        query_contract_hash=chart_cache_contract_identity(query),
    )

    def loader(*_args, **_kwargs):
        return entry

    monkeypatch.setattr(cg, "_scrub_pii_rows", lambda rows: rows)
    executor = MagicMock(return_value=[{"Value": 0}])
    result = cg._gather_single_chart(query, "synthetic", executor, _NOW, loader, tmp_path)
    assert result.error is None
    assert result.cache_hit is cache_hit
    assert executor.call_count == (0 if cache_hit else 1)


@pytest.mark.parametrize("field", ["program_id", "query_id"])
def test_chart_gather_refuses_foreign_cache_identity(field, tmp_path, monkeypatch):
    import src.commands.chart_gather as cg

    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
    )
    write_chart_cache(
        "synthetic", query.id, [{"Value": 7}], _NOW, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
        query_contract_hash=chart_cache_contract_identity(query),
    )
    entry = load_chart_cache("synthetic", query.id, programs_root=tmp_path)
    foreign = replace(entry, **{field: "foreign"})

    def loader(_program, _query, **_kwargs):
        return foreign

    monkeypatch.setattr(cg, "_scrub_pii_rows", lambda rows: rows)
    executor = MagicMock(return_value=[{"Value": 0}])
    result = cg._gather_single_chart(query, "synthetic", executor, _NOW, loader, tmp_path)
    assert not result.cache_hit
    assert result.error is None
    executor.assert_called_once_with(query)


def test_chart_gather_logs_loader_failure_and_executes_live(tmp_path, monkeypatch, caplog):
    import src.commands.chart_gather as cg

    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
    )

    def loader(*_args, **_kwargs):
        raise OSError("synthetic cache unavailable")

    monkeypatch.setattr(cg, "_scrub_pii_rows", lambda rows: rows)
    result = cg._gather_single_chart(
        query, "synthetic", lambda _query: [{"Value": 0}], _NOW, loader, tmp_path,
    )
    assert result.error is None
    assert result.rows_written == 1
    assert "cache load failed for synthetic/synthetic" in caplog.text
    assert "synthetic cache unavailable" not in caplog.text


def test_chart_gather_size_refusal_is_explicit_and_independent_query_progresses(tmp_path, monkeypatch):
    import src.commands.chart_gather as cg
    import src.core.chart_cache_store as store

    now = datetime.now(timezone.utc)
    refused = KustoQuery(
        id="refused", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
        result_limits=KustoResultLimits(("Value",), 1, 10000),
    )
    valid = replace(refused, id="valid")
    existing = write_chart_cache(
        "synthetic", refused.id, [{"Value": 7}], now, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
    )
    before = existing.read_bytes()
    monkeypatch.setattr(store, "MAX_CACHE_ENTRY_BYTES", 1000)
    monkeypatch.setattr(cg, "_VERTEX_CHARTS_ENABLED", True)
    monkeypatch.setattr(cg, "_load_chart_queries", lambda *_args, **_kwargs: (refused, valid))
    monkeypatch.setattr(cg, "_scrub_pii_rows", lambda rows: rows)
    sink = []
    results, errors = gather_chart_data(
        "synthetic", programs_root=tmp_path, program=_make_mock_program(), workstreams=(),
        executor=lambda query: [{"Value": "x" * 2000 if query.id == refused.id else 0}],
        current_time=now, integration_error_sink=sink,
    )
    assert results[0].error and "Cache write" in results[0].error
    assert results[0].rows_written is None
    assert not results[0].cache_hit
    assert len(errors) == 1
    assert list(errors) == sink
    assert existing.read_bytes() == before
    assert results[1].error is None
    assert results[1].rows_written == 1
    assert load_chart_cache("synthetic", valid.id, programs_root=tmp_path).rows == ({"Value": 0},)


@pytest.mark.parametrize(
    "change",
    [{"cluster": "other"}, {"database": "other"}, {"kql": "other"},
     {"render_as": "table"}, {"chart_renderer_id": "synthetic::other"},
     {"chart_config": {"type": "line", "options": {"axis": "other"}}}],
)
def test_chart_gather_refreshes_changed_definition(change, tmp_path, monkeypatch):
    import src.commands.chart_gather as cg

    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high",
        chart_config={"type": "line", "options": {"axis": "initial"}},
    )
    monkeypatch.setattr(cg, "_scrub_pii_rows", lambda rows: rows)
    executor = MagicMock(return_value=[{"Value": 0}])
    first = cg._gather_single_chart(query, "synthetic", executor, _NOW, load_chart_cache, tmp_path)
    assert first.rows_written == 1
    executor.reset_mock()
    executor.return_value = [{"Value": 9}]
    updated = replace(query, **change)
    result = cg._gather_single_chart(updated, "synthetic", executor, _NOW, load_chart_cache, tmp_path)
    assert result.error is None
    assert not result.cache_hit
    executor.assert_called_once_with(updated)
    assert load_chart_cache("synthetic", query.id, programs_root=tmp_path).rows == ({"Value": 9},)


@pytest.mark.parametrize("legacy_hash", ["missing", None])
def test_chart_gather_refreshes_unbound_legacy_cache(legacy_hash, tmp_path, monkeypatch):
    import json
    import src.commands.chart_gather as cg

    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
    )
    path = write_chart_cache(
        "synthetic", query.id, [{"Value": 7}], _NOW, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    if legacy_hash is None:
        data["query_contract_hash"] = None
    else:
        data.pop("query_contract_hash", None)
    path.write_text(json.dumps(data), encoding="utf-8")
    assert load_chart_cache("synthetic", query.id, programs_root=tmp_path) is not None
    monkeypatch.setattr(cg, "_scrub_pii_rows", lambda rows: rows)
    executor = MagicMock(return_value=[{"Value": 0}])
    result = cg._gather_single_chart(query, "synthetic", executor, _NOW, load_chart_cache, tmp_path)
    assert result.error is None
    assert not result.cache_hit
    executor.assert_called_once_with(query)


@pytest.mark.parametrize("reorder", [False, True])
def test_chart_gather_reuses_same_definition_with_stable_nested_order(reorder, tmp_path, monkeypatch):
    import src.commands.chart_gather as cg

    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high",
        chart_config={"type": "line", "options": {"x": "first", "y": "second"}},
    )
    monkeypatch.setattr(cg, "_scrub_pii_rows", lambda rows: rows)
    executor = MagicMock(return_value=[{"Value": 0}])
    first = cg._gather_single_chart(query, "synthetic", executor, _NOW, load_chart_cache, tmp_path)
    assert first.rows_written == 1
    cached = load_chart_cache("synthetic", query.id, programs_root=tmp_path)
    assert cached.query_contract_hash == chart_cache_contract_identity(query)
    executor.reset_mock()
    if reorder:
        query = replace(query, chart_config={"options": {"y": "second", "x": "first"}, "type": "line"})
    result = cg._gather_single_chart(query, "synthetic", executor, _NOW, load_chart_cache, tmp_path)
    assert result.cache_hit
    assert result.error is None
    executor.assert_not_called()


# ---------------------------------------------------------------------------
# TTL grace calculation
# ---------------------------------------------------------------------------

def test_eviction_grace_hours_for_ttl_min_168():
    assert _eviction_grace_hours_for_ttl(26) == 168  # max(26*3, 168) = 168
    assert _eviction_grace_hours_for_ttl(60) == 180  # max(60*3, 168) = 180
    assert _eviction_grace_hours_for_ttl(10) == 168  # max(30, 168) = 168


# ---------------------------------------------------------------------------
# Row sanitization
# ---------------------------------------------------------------------------

def test_sanitize_rows_drops_dynamic_columns():
    rows = [{"key": "val", "dynamic": {"nested": "data"}}]
    result = _sanitize_rows(rows)
    assert "dynamic" not in result[0]
    assert result[0]["key"] == "val"


def test_sanitize_rows_coerces_datetime():
    from datetime import date
    rows = [{"dt": datetime(2026, 5, 1, 12, 0, 0, tzinfo=timezone.utc), "d": date(2026, 5, 1)}]
    result = _sanitize_rows(rows)
    assert isinstance(result[0]["dt"], str)
    assert "2026" in result[0]["dt"]


def test_sanitize_rows_passes_primitives():
    rows = [{"i": 42, "f": 3.14, "s": "hello", "b": True, "n": None}]
    result = _sanitize_rows(rows)
    assert result[0]["i"] == 42
    assert result[0]["n"] is None


# ---------------------------------------------------------------------------
# Row truncation
# ---------------------------------------------------------------------------

def test_truncate_rows_not_truncated_with_summarize():
    rows = [{"x": i} for i in range(600)]
    kql = "T | summarize count() by x"
    result, was = _truncate_rows(rows, kql)
    assert not was
    assert len(result) == 600


def test_truncate_rows_truncated_without_summarize():
    rows = [{"x": i} for i in range(600)]
    kql = "T | where x > 0"
    result, was = _truncate_rows(rows, kql)
    assert was
    assert len(result) == 500


def test_truncate_rows_empty_no_truncation():
    result, was = _truncate_rows([], "T | where x > 0")
    assert not was
    assert result == []


# ---------------------------------------------------------------------------
# PII scrubbing (unit — no network)
# ---------------------------------------------------------------------------

def test_scrub_pii_rows_does_not_fall_back_to_raw_rows():
    with patch.dict("sys.modules", {"src.ai.safety.pii_scrubber": None}):
        with pytest.raises(ImportError):
            _scrub_pii_rows([{"field": "sample@example.invalid"}])


def test_scrub_pii_rows_handles_nested_values_without_changing_numeric_or_date_values():
    from datetime import date

    sample = "sample@example.invalid"
    original = [{
        "nested": {"contact": sample, "items": [sample]},
        "number": 1234567890,
        "phone_count": 1234567890,
        "decimal": Decimal("12.50"),
        "date": date(2026, 5, 1),
    }]
    scrubbed = _scrub_pii_rows(original)
    assert scrubbed[0]["nested"] == {
        "contact": "[PII-FILTERED-EMAIL]", "items": ["[PII-FILTERED-EMAIL]"],
    }
    assert scrubbed[0]["number"] == 1234567890
    assert scrubbed[0]["phone_count"] == 1234567890
    assert scrubbed[0]["decimal"] == Decimal("12.50")
    assert scrubbed[0]["date"] == date(2026, 5, 1)
    assert original[0]["nested"]["contact"] == sample


def test_scrub_pii_rows_rejects_sensitive_nested_column_names_without_echoing_them():
    sample = "sample@example.invalid"
    with pytest.raises(ValueError) as error:
        _scrub_pii_rows([{"nested": {sample: "value"}}])
    assert sample not in str(error.value)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("PhoneNumber", 15555550199),
        ("mobile_phone", Decimal("15555550199")),
        ("SSN", 123456789),
        ("PhoneNumber", [15555550199]),
        ("mobile_phone", (Decimal("15555550199"),)),
        ("SSN", {"value": 123456789}),
        ("SSN", {"nested": [{"value": 123456789}]}),
        ("SSN", "123456789"),
        ("SSN", {"nested": ["123 45 6789"]}),
        ("PhoneNumber", "ext 123"),
        ("SSN", b"123456789"),
    ],
)
def test_scrub_pii_rows_rejects_numeric_identifiers_in_sensitive_columns(column, value):
    with pytest.raises(ValueError) as error:
        _scrub_pii_rows([{"nested": {column: value}}])
    assert str(value) not in str(error.value)


def test_scrub_pii_rows_preserves_numeric_collections_under_other_columns():
    rows = [{"phone_count": [1, {"value": 2}], "metric": (3, 4), "phone": [True, None]}]
    assert _scrub_pii_rows(rows) == rows


def test_scrub_pii_rows_preserves_filtered_identifiers_and_other_strings():
    rows = [{"SSN": "123-45-6789", "PhoneNumber": "555-555-5555", "metric": "123456789"}]
    scrubbed = _scrub_pii_rows(rows)
    assert scrubbed == [{
        "SSN": "[PII-FILTERED-SSN]",
        "PhoneNumber": "[PII-FILTERED-PHONE]",
        "metric": "123456789",
    }]
    assert _scrub_pii_rows(scrubbed) == scrubbed


@pytest.mark.parametrize("sample", [15555550199, "123456789"])
def test_chart_gather_rejects_nested_numeric_identifiers_without_overwriting_cache(sample, tmp_path, caplog):
    import src.commands.chart_gather as cg

    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
    )
    existing = write_chart_cache(
        "synthetic", query.id, [{"value": 1}], _NOW, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
    )
    before = existing.read_bytes()
    result = cg._gather_single_chart(
        query, "synthetic", lambda _: [{"phone": {"value": sample}}], _NOW, None, tmp_path,
    )
    assert result.error
    assert result.rows_written is None
    assert str(sample) not in result.error
    assert str(sample) not in caplog.text
    assert existing.read_bytes() == before


@pytest.mark.parametrize("failure", ["missing", "exception"])
def test_chart_gather_scrub_failure_preserves_cache_and_reports_safely(failure, tmp_path, monkeypatch, caplog):
    import src.commands.chart_gather as cg
    import src.ai.safety.pii_scrubber as scrubber

    sample = "sample@example.invalid"
    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
    )
    existing = write_chart_cache(
        "synthetic", query.id, [{"value": 1}], _NOW, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
    )
    before = existing.read_bytes()
    if failure == "exception":
        def fail(_text):
            raise RuntimeError(sample)
        monkeypatch.setattr(scrubber, "scan_text", fail)
    with patch.dict("sys.modules", {"src.ai.safety.pii_scrubber": None} if failure == "missing" else {}):
        result = cg._gather_single_chart(
            query, "synthetic", lambda _: [{"label": sample, "value": 2}], _NOW, None, tmp_path,
        )
    assert result.error
    assert result.rows_written is None
    assert not result.cache_hit
    assert sample not in result.error
    assert sample not in caplog.text
    assert existing.read_bytes() == before


def test_chart_gather_cannot_trust_fresh_cache_when_scrubber_is_unavailable(tmp_path):
    import src.commands.chart_gather as cg

    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
    )
    existing = write_chart_cache(
        "synthetic", query.id, [{"value": 1}], _NOW, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True, query_contract_hash=chart_cache_contract_identity(query),
    )
    before = existing.read_bytes()
    executor = MagicMock(return_value=[{"value": 2}])
    with patch.dict("sys.modules", {"src.ai.safety.pii_scrubber": None}):
        result = cg._gather_single_chart(query, "synthetic", executor, _NOW, load_chart_cache, tmp_path)
    assert result.error
    assert not result.cache_hit
    assert result.rows_written is None
    assert existing.read_bytes() == before


@pytest.mark.parametrize("row_kind", ["value", "column", "identifier_string"])
@pytest.mark.parametrize("refresh_fails", [False, True])
def test_chart_gather_revalidates_legacy_cache_before_reuse(
    row_kind, refresh_fails, tmp_path,
):
    import src.commands.chart_gather as cg
    from src.core.exceptions import QueryError

    sample = "sample@example.invalid"
    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
    )
    if row_kind == "identifier_string":
        row = {"SSN": "123456789"}
    else:
        row = {sample: 1} if row_kind == "column" else {"label": sample, "value": 1}
    existing = write_chart_cache(
        "synthetic", query.id, [row], _NOW, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True, query_contract_hash=chart_cache_contract_identity(query),
    )
    before = existing.read_bytes()
    executor = MagicMock(side_effect=QueryError("synthetic failure") if refresh_fails else None)
    if not refresh_fails:
        executor.return_value = [{"label": "safe", "value": 2}]
    result = cg._gather_single_chart(query, "synthetic", executor, _NOW, load_chart_cache, tmp_path)
    executor.assert_called_once_with(query)
    assert not result.cache_hit
    if refresh_fails:
        assert result.error
        assert existing.read_bytes() == before
    else:
        assert result.error is None
        assert load_chart_cache("synthetic", query.id, programs_root=tmp_path).rows == (
            {"label": "safe", "value": 2},
        )


def test_chart_gather_rejects_sensitive_column_without_logging_it(tmp_path, caplog):
    import src.commands.chart_gather as cg

    sample = "sample@example.invalid"
    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
    )
    existing = write_chart_cache(
        "synthetic", query.id, [{"value": 1}], _NOW, "synthetic",
        programs_root=tmp_path, pii_prescrubbed=True,
    )
    before = existing.read_bytes()
    result = cg._gather_single_chart(
        query, "synthetic", lambda _: [{sample: {"nested": "value"}}], _NOW, None, tmp_path,
    )
    assert result.error
    assert result.rows_written is None
    assert sample not in result.error
    assert sample not in caplog.text
    assert existing.read_bytes() == before


def test_chart_gather_scrubs_before_truncation_and_after_string_conversion(tmp_path):
    import src.commands.chart_gather as cg

    class Label:
        def __str__(self):
            return "sample@example.invalid"

    query = KustoQuery(
        id="synthetic", cluster="synthetic", database="synthetic", kql="synthetic",
        section="Synthetic", render_as="chart_image", confidence="high", chart_config={"type": "line"},
    )
    long_value = "x" * 9990 + " sample@example.invalid" + " " * (5 * 1024 * 1024)
    result = cg._gather_single_chart(
        query, "synthetic", lambda _: [{"label": long_value, "other": Label()}], _NOW, None, tmp_path,
    )
    assert result.error is None
    rows = load_chart_cache("synthetic", query.id, programs_root=tmp_path).rows
    assert "sample@" not in str(rows)
    assert rows[0]["other"] == "[PII-FILTERED-EMAIL]"


# ---------------------------------------------------------------------------
# gather_chart_data integration (with mocks)
# ---------------------------------------------------------------------------

def _make_mock_program():
    prog = MagicMock()
    prog.id = "acme"
    return prog


def _make_workstream(ws_id: str):
    ws = MagicMock()
    ws.id = ws_id
    return ws


@pytest.fixture
def mock_programs_root(tmp_path):
    return tmp_path


def test_gather_chart_data_disabled_by_env(mock_programs_root, monkeypatch):
    monkeypatch.setenv("VERTEX_CHARTS", "0")
    import importlib
    import src.commands.chart_gather as cg
    monkeypatch.setattr(cg, "_VERTEX_CHARTS_ENABLED", False)

    results, errors = gather_chart_data(
        "acme",
        programs_root=mock_programs_root,
        program=_make_mock_program(),
        workstreams=(_make_workstream("ws1"),),
        executor=MagicMock(),
    )
    assert results == ()
    assert errors == ()


def test_gather_chart_data_no_programs_root():
    results, errors = gather_chart_data(
        "acme",
        programs_root=None,  # type: ignore[arg-type]
        program=_make_mock_program(),
        workstreams=(),
        executor=MagicMock(),
    )
    assert results == ()
    assert errors == ()
