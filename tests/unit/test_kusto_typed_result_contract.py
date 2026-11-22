from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from unittest.mock import MagicMock

from src.core.config_loader import KustoQuerySettings, KustoSettings
from src.core.exceptions import QueryError
from src.core.integration_types import (
    ChannelRegistration,
    KustoResultColumn,
    KustoResultKind,
    KustoResultSchema,
    KustoResultSet,
    RegistrationStatus,
)
from src.core.kusto_hydration import KustoHydrationConfig, KustoHydrationProvider
from src.core.kusto_rendering import build_kusto_sections
from src.core.kusto_result_contract import (
    admit_kusto_execution,
    build_kusto_result_schema,
)
from src.core.models_v2 import KustoQuery

NOW = datetime(2026, 11, 18, 12, tzinfo=UTC)


def _query(*, query_id: str = "query-a", cardinality: str = "zero_ok") -> KustoQuery:
    return KustoQuery(
        id=query_id,
        cluster="https://cluster",
        database="db",
        kql=f"{query_id} | take 100",
        section="A",
        render_as="table",
        confidence="high",
        workstream_ids=("ws-a",),
        validated=True,
        expected_cardinality=cardinality,
    )


def _registration(query_id: str = "query-a") -> ChannelRegistration:
    return ChannelRegistration(
        channel="kusto",
        program_id="demo",
        provider_instance_id="default",
        ref_id=query_id,
        ref_kind="kusto_query",
        status=RegistrationStatus.ACTIVE,
        first_discovered_at=NOW,
        last_seen_at=NOW,
        workstream_ids=("ws-a",),
    )


@pytest.mark.parametrize(
    ("cardinality", "rows", "columns", "expected_kind"),
    [
        ("scalar_required", ({"Value": 12},), (("Value", "long"),), KustoResultKind.SCALAR),
        (
            "zero_ok",
            ({"Name": "East", "Count": 4},),
            (("Name", "string"), ("Count", "long")),
            KustoResultKind.TABLE,
        ),
        (
            "zero_ok",
            (
                {"Timestamp": "2026-11-17T00:00:00Z", "Count": 4},
                {"Timestamp": "2026-11-18T00:00:00Z", "Count": 5},
            ),
            (("Timestamp", "datetime"), ("Count", "long")),
            KustoResultKind.TIME_SERIES,
        ),
    ],
)
def test_schema_classifies_scalar_table_and_time_series(
    cardinality: str,
    rows: tuple[dict[str, object], ...],
    columns: tuple[tuple[str, str], ...],
    expected_kind: KustoResultKind,
) -> None:
    schema = build_kusto_result_schema(
        rows,
        columns=tuple(KustoResultColumn(name, kind) for name, kind in columns),
        expected_cardinality=cardinality,
    )

    assert schema.kind is expected_kind
    assert tuple(column.name for column in schema.columns) == tuple(name for name, _ in columns)
    assert schema.row_count == len(rows)
    if expected_kind is KustoResultKind.TIME_SERIES:
        assert schema.time_column == "Timestamp"
        assert schema.value_columns == ("Count",)


def test_schema_infers_typed_time_series_without_provider_column_metadata() -> None:
    schema = build_kusto_result_schema(
        (
            {"ObservedAt": datetime(2026, 11, 17, tzinfo=UTC), "Count": 4},
            {"ObservedAt": date(2026, 11, 18), "Count": 5},
        )
    )

    assert schema.kind is KustoResultKind.TIME_SERIES
    assert schema.time_column == "ObservedAt"
    assert schema.value_columns == ("Count",)


def test_schema_refuses_duplicate_column_names() -> None:
    with pytest.raises(QueryError, match="duplicate"):
        build_kusto_result_schema(
            ({"Value": 1},),
            columns=(KustoResultColumn("Value", "long"), KustoResultColumn("Value", "long")),
        )


def test_legacy_row_lists_remain_admitted_as_table_results() -> None:
    rows, schema = admit_kusto_execution(
        [{"Name": "East", "Count": 4}],
        query_id="legacy",
        expected_cardinality="zero_ok",
        result_limits=None,
    )

    assert rows == [{"Name": "East", "Count": 4}]
    assert schema.kind is KustoResultKind.TABLE


@pytest.mark.parametrize("rows", [[], [{"Value": 1}, {"Value": 2}]])
def test_execution_admission_enforces_scalar_cardinality(rows: list[dict[str, int]]) -> None:
    with pytest.raises(QueryError, match="scalar_required"):
        admit_kusto_execution(
            rows,
            query_id="scalar",
            expected_cardinality="scalar_required",
            result_limits=None,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("query_id", "other", "query identity"),
        ("is_partial", True, "partial"),
        ("row_count", 2, "row count"),
    ],
)
def test_typed_result_refuses_mismatched_admission_metadata(
    field: str, value: object, message: str
) -> None:
    values: dict[str, object] = {
        "query_id": "query-a",
        "rows": ({"Value": 1},),
        "observed_at": NOW,
        "is_partial": False,
        "row_count": 1,
    }
    values[field] = value
    result = KustoResultSet(**values)  # type: ignore[arg-type]

    with pytest.raises(QueryError, match=message):
        admit_kusto_execution(
            result,
            query_id="query-a",
            expected_cardinality="scalar_required",
            result_limits=None,
        )


def test_typed_result_schema_cannot_claim_columns_absent_from_rows() -> None:
    result = KustoResultSet(
        query_id="query-a",
        rows=({"Value": 1},),
        observed_at=NOW,
        schema=KustoResultSchema(
            kind=KustoResultKind.SCALAR,
            columns=(KustoResultColumn("Missing", "long"),),
            row_count=1,
        ),
        row_count=1,
    )

    with pytest.raises(QueryError, match="schema"):
        admit_kusto_execution(
            result,
            query_id="query-a",
            expected_cardinality="scalar_required",
            result_limits=None,
        )


def test_typed_result_refuses_unsupported_executor_value() -> None:
    with pytest.raises(QueryError, match="unsupported"):
        admit_kusto_execution(  # type: ignore[arg-type]
            "not-a-result",
            query_id="query-a",
            expected_cardinality="zero_ok",
            result_limits=None,
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("kind", KustoResultKind.TABLE, "kind"),
        ("time_column", "WrongTime", "time column"),
        ("value_columns", ("WrongValue",), "value columns"),
    ],
)
def test_typed_result_refuses_mismatched_semantic_schema_fields(
    field: str, value: object, message: str
) -> None:
    rows = (
        {"Timestamp": "2026-11-17T00:00:00Z", "Count": 4},
    )
    schema = build_kusto_result_schema(
        rows,
        columns=(KustoResultColumn("Timestamp", "datetime"), KustoResultColumn("Count", "long")),
    )
    result = KustoResultSet(
        query_id="query-a",
        rows=rows,
        observed_at=NOW,
        schema=replace(schema, **{field: value}),
        row_count=1,
    )

    with pytest.raises(QueryError, match=message):
        admit_kusto_execution(
            result,
            query_id="query-a",
            expected_cardinality="zero_ok",
            result_limits=None,
        )


def test_typed_result_refuses_schema_row_count_mismatch() -> None:
    rows = ({"Value": 1},)
    schema = replace(
        build_kusto_result_schema(rows, columns=(KustoResultColumn("Value", "long"),)),
        row_count=2,
    )
    result = KustoResultSet(
        query_id="query-a",
        rows=rows,
        observed_at=NOW,
        schema=schema,
        row_count=1,
    )

    with pytest.raises(QueryError, match="schema row count"):
        admit_kusto_execution(
            result,
            query_id="query-a",
            expected_cardinality="zero_ok",
            result_limits=None,
        )


def test_scalar_required_typed_result_must_have_scalar_kind() -> None:
    result = KustoResultSet(
        query_id="query-a",
        rows=({"Value": 1},),
        observed_at=NOW,
        schema=KustoResultSchema(
            kind=KustoResultKind.TABLE,
            columns=(KustoResultColumn("Value", "long"),),
            row_count=1,
        ),
        row_count=1,
    )

    with pytest.raises(QueryError, match="requires a scalar"):
        admit_kusto_execution(
            result,
            query_id="query-a",
            expected_cardinality="scalar_required",
            result_limits=None,
        )


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        (
            KustoResultSchema(
                kind=KustoResultKind.TABLE,
                columns=(KustoResultColumn("", "string"),),
                row_count=1,
            ),
            "invalid or duplicate",
        ),
        (
            KustoResultSchema(
                kind=KustoResultKind.SCALAR,
                columns=(KustoResultColumn("Value", "long"),),
                row_count=1,
                time_column="Timestamp",
            ),
            "scalar result schema",
        ),
    ],
)
def test_typed_schema_refuses_invalid_semantic_columns(
    schema: KustoResultSchema, message: str,
) -> None:
    with pytest.raises(QueryError, match=message):
        admit_kusto_execution(
            KustoResultSet(
                query_id="query-a",
                rows=({"Value": 1},) if schema.columns[0].name else ({"": 1},),
                observed_at=NOW,
                schema=schema,
                row_count=1,
            ),
            query_id="query-a",
            expected_cardinality="zero_ok",
            result_limits=None,
        )


def test_typed_schema_refuses_unsupported_kind() -> None:
    schema = KustoResultSchema(
        kind="unsupported",  # type: ignore[arg-type]
        columns=(KustoResultColumn("Value", "long"),),
        row_count=1,
    )

    with pytest.raises(QueryError, match="unsupported result kind"):
        admit_kusto_execution(
            KustoResultSet(
                query_id="query-a",
                rows=({"Value": 1},),
                observed_at=NOW,
                schema=schema,
                row_count=1,
            ),
            query_id="query-a",
            expected_cardinality="zero_ok",
            result_limits=None,
        )


def test_hydration_preserves_typed_schema_and_result_kind(tmp_path: Path) -> None:
    query = _query()
    schema = build_kusto_result_schema(
        typed_rows := (
            {"Timestamp": "2026-11-17T00:00:00Z", "Count": 4},
            {"Timestamp": "2026-11-18T00:00:00Z", "Count": 5},
        ),
        columns=(KustoResultColumn("Timestamp", "datetime"), KustoResultColumn("Count", "long")),
    )
    typed = KustoResultSet(
        query_id=query.id,
        rows=typed_rows,
        observed_at=NOW,
        schema=schema,
        row_count=2,
    )
    provider = KustoHydrationProvider(
        executor=lambda _: typed,
        query_loader=lambda *_, **__: (query,),
    )

    result = provider.hydrate(
        (_registration(),),
        NOW,
        "demo",
        KustoHydrationConfig(programs_root=tmp_path),
    )

    assert result.errors == ()
    result_set = result.resources.result_sets[0]
    assert result_set.schema is not None
    assert result_set.schema.kind is KustoResultKind.TIME_SERIES
    assert result_set.schema.value_columns == ("Count",)


def test_rendering_preserves_result_kind_for_typed_time_series(
    tmp_path: Path,
) -> None:
    rows = (
        {"Timestamp": "2026-11-17T00:00:00Z", "Count": 4},
        {"Timestamp": "2026-11-18T00:00:00Z", "Count": 5},
    )
    schema = build_kusto_result_schema(
        rows,
        columns=(KustoResultColumn("Timestamp", "datetime"), KustoResultColumn("Count", "long")),
    )
    query = KustoQuerySettings(
        id="trend",
        cluster="https://cluster",
        database="db",
        kql="trend",
        section="Trend",
        render_as="table",
        confidence="high",
        kusto_section_validates_slice=False,
        caveats=(),
        reference_url=None,
    )

    sections, observations, warnings = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)),
        lambda _: KustoResultSet(
            query_id="trend",
            rows=rows,
            observed_at=NOW,
            schema=schema,
            row_count=2,
        ),
        observed_at=NOW,
        programs_root=tmp_path,
    )

    assert warnings == ()
    assert observations[0].execution_state == "success"
    assert sections[0].result_kind == KustoResultKind.TIME_SERIES.value
    assert sections[0].columns == ("Timestamp", "Count")


def test_registry_chart_preserves_typed_result_kind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.core import kusto_rendering

    rows = (
        {"Timestamp": "2026-11-17T00:00:00Z", "Count": 4},
        {"Timestamp": "2026-11-18T00:00:00Z", "Count": 5},
    )
    schema = build_kusto_result_schema(
        rows,
        columns=(KustoResultColumn("Timestamp", "datetime"), KustoResultColumn("Count", "long")),
    )
    query = KustoQuerySettings(
        id="trend",
        cluster="https://cluster",
        database="db",
        kql="trend",
        section="Trend",
        render_as="chart_image",
        confidence="high",
        kusto_section_validates_slice=False,
        caveats=(),
        reference_url=None,
        chart_config={"type": "line"},
        chart_renderer_id="synthetic::line",
    )
    registry = MagicMock()
    registry.get_renderer.return_value.build.return_value = (b"image", {})
    monkeypatch.setattr(kusto_rendering, "_VERTEX_CHARTS_ENABLED", True)

    sections, _, _ = build_kusto_sections(
        KustoSettings(enabled=True, queries=(query,)),
        lambda _: KustoResultSet(
            query_id="trend",
            rows=rows,
            observed_at=NOW,
            schema=schema,
            row_count=2,
        ),
        chart_registry=registry,
        chart_rows_scrubber=lambda value: value,
        observed_at=NOW,
        programs_root=tmp_path,
    )

    assert sections[0].render_mode == "chart"
    assert sections[0].result_kind == KustoResultKind.TIME_SERIES.value
