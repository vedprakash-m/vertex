from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core import metric_binding_validator as validator
from src.core.exceptions import ConfigError, QueryError
from src.core.kusto_client import KustoColumn
from src.core.metric_models import MetricAggregation, MetricDefinition, MetricSourceBinding


METRICS = Path("synthetic") / "metrics" / "catalog.yaml"
KPIS = Path("synthetic") / "programs" / "demo" / "kpis.yaml"
GOLDEN = Path("synthetic") / "knowledge" / "golden_queries.yaml"


def metric() -> dict[str, object]:
    return {"id": "demo.count", "title": "Count", "unit": "count", "aggregation": "last"}


def query() -> dict[str, object]:
    return {
        "id": "count-query",
        "metric_id": "demo.count",
        "cluster": "https://synthetic.invalid",
        "database": "synthetic",
        "kql": "print Count=1",
        "result_column": "Count",
    }


def validate(
    metrics: object = None,
    queries: object = None,
) -> validator.MetricBindingValidation:
    return validator.validate_metric_binding_documents(
        program_id="demo",
        metric_documents={METRICS: {"metrics": [metric()]} if metrics is None else metrics},
        query_documents={KPIS: {"kpis": [query()]} if queries is None else queries},
    )


def test_valid_inline_binding_has_bounded_scope_and_no_live_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> object:
        raise AssertionError("offline validation attempted I/O")

    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr(validator, "build_live_metric_binding_probe", forbidden)
    monkeypatch.setattr(validator, "KustoClient", forbidden)
    report = validate()
    assert report.errors == ()
    assert (report.metric_count, report.query_count, report.binding_count) == (1, 1, 1)
    assert report.scope == "offline_metric_query_bindings"
    limitations = " ".join(report.warnings).lower()
    for limitation in ("live", "schema", "numeric", "cardinality", "freshness", "persisted"):
        assert limitation in limitations


@pytest.mark.parametrize("key", ["metrics", "kpis"])
def test_version_only_document_does_not_prove_content_validity(key: str) -> None:
    document = {"schema_version": "1.0"}
    report = validate(metrics=document) if key == "metrics" else validate(queries=document)
    assert any(key in error for error in report.errors)


@pytest.mark.parametrize("document", [[], "not a mapping", {"kpis": None}, {"kpis": {}}, {"kpis": [1]}])
def test_malformed_query_documents_are_not_silently_skipped(document: object) -> None:
    assert validate(queries=document).errors


@pytest.mark.parametrize("document", ["not metrics", {"metrics": None}, {"metrics": {}}, {"metrics": [1]}])
def test_malformed_metric_documents_are_not_silently_skipped(document: object) -> None:
    assert validate(metrics=document).errors


def test_missing_optional_documents_and_explicit_empty_collections_are_bounded_empty() -> None:
    report = validator.validate_metric_binding_documents(
        program_id="demo", metric_documents={}, query_documents={},
    )
    assert report.errors == ()
    assert (report.metric_count, report.query_count, report.binding_count) == (0, 0, 0)
    assert report.warnings
    report = validate(metrics={"metrics": []}, queries={"kpis": []})
    assert report.errors == ()


def test_authored_validation_flag_cannot_hide_missing_registry_reference() -> None:
    entry = query()
    entry["validated"] = True
    before = deepcopy(entry)
    report = validator.validate_metric_binding_documents(
        program_id="demo", metric_documents={}, query_documents={KPIS: {"kpis": [entry]}},
    )
    assert any("demo.count" in error for error in report.errors)
    assert report.binding_count == 0
    assert entry == before


@pytest.mark.parametrize("field", ["id", "title", "unit", "aggregation"])
def test_required_metric_fields_use_existing_parser(field: str) -> None:
    entry = metric()
    del entry[field]
    assert validate(metrics={"metrics": [entry]}).errors


@pytest.mark.parametrize("field,value", [
    ("freshness_tier", "instant"),
    ("retention_days", -1),
    ("dimension_columns", "region"),
    ("valid_from", "not-a-date"),
    ("aggregation", "median-of-percentiles"),
])
def test_existing_metric_semantics_reject_invalid_values(field: str, value: object) -> None:
    entry = metric()
    entry[field] = value
    assert validate(metrics={"metrics": [entry]}).errors


def test_dangling_metric_and_comparability_references_are_visible() -> None:
    entry = metric()
    entry["comparable_with"] = ["absent.metric"]
    authored_query = query()
    authored_query["metric_id"] = "missing.metric"
    report = validate(metrics=[entry], queries={"kpis": [authored_query]})
    assert any("absent.metric" in error for error in report.errors)
    assert any("missing.metric" in error for error in report.errors)


def test_valid_comparability_references_use_all_supplied_definitions() -> None:
    first = metric()
    first["comparable_with"] = ["demo.previous"]
    second = metric()
    second["id"] = "demo.previous"
    assert validate(metrics=[first, second]).errors == ()


@pytest.mark.parametrize("field", ["cluster", "database", "kql", "result_column"])
def test_kusto_binding_required_fields_match_existing_validator(field: str) -> None:
    entry = query()
    del entry[field]
    assert any(field in error for error in validate(queries={"kpis": [entry]}).errors)


@pytest.mark.parametrize("field,value", [
    ("id", 42), ("metric_id", 42), ("engine", 42),
    ("program_ids", "demo"), ("program_ids", [42]),
    ("kql", 42), ("kql_file", 42), ("result_column", 42),
])
def test_malformed_binding_metadata_is_not_coerced_or_dropped(field: str, value: object) -> None:
    entry = query()
    entry[field] = value
    assert validate(queries={"kpis": [entry]}).errors


def test_wiql_binding_does_not_require_kusto_connection() -> None:
    entry = query()
    for field in ("cluster", "database", "kql"):
        del entry[field]
    entry.update(engine="wiql", wiql="SELECT [System.Id] FROM WorkItems")
    report = validate(queries={"kpis": [entry]})
    assert report.errors == ()
    assert report.binding_count == 1
    del entry["wiql"]
    assert any("wiql" in error for error in validate(queries={"kpis": [entry]}).errors)


def test_unsupported_engine_is_not_reported_as_validated_binding() -> None:
    entry = query()
    entry["engine"] = "ado_odata"
    report = validate(queries={"kpis": [entry]})
    assert report.errors == ()
    assert report.binding_count == 0
    assert any("ado_odata" in warning and "not" in warning for warning in report.warnings)


def test_file_backed_kql_is_not_loaded_or_reported_as_validated_binding() -> None:
    entry = query()
    del entry["kql"]
    entry["kql_file"] = "..\\outside\\query.kql"
    report = validate(queries={"kpis": [entry]})
    assert report.errors == ()
    assert report.binding_count == 0
    assert any("kql_file" in warning for warning in report.warnings)
    del entry["cluster"]
    assert any("cluster" in error for error in validate(queries={"kpis": [entry]}).errors)


def test_non_metric_query_retains_optional_metric_metadata() -> None:
    report = validate(queries={"kpis": [{"id": "diagnostic"}]})
    assert report.errors == ()
    assert report.query_count == 1
    assert report.binding_count == 0


def test_scoped_shared_queries_do_not_validate_another_programs_binding() -> None:
    excluded = query()
    excluded.update(program_ids=["other"], metric_id="other.missing")
    report = validator.validate_metric_binding_documents(
        program_id="demo",
        metric_documents={METRICS: [metric()]},
        query_documents={GOLDEN: {"queries": [excluded, query()]}},
    )
    assert report.errors == ()
    assert (report.query_count, report.binding_count) == (1, 1)


def test_duplicate_query_ids_across_catalogs_are_errors() -> None:
    report = validator.validate_metric_binding_documents(
        program_id="demo",
        metric_documents={METRICS: [metric()]},
        query_documents={GOLDEN: {"queries": [query()]}, KPIS: {"kpis": [query()]}},
    )
    assert any("Duplicate" in error for error in report.errors)


def test_versioned_definitions_are_not_mistaken_for_duplicate_metric_ids() -> None:
    first = metric()
    first.update(valid_from="2026-01-01T00:00:00Z", valid_until="2026-02-01T00:00:00Z")
    second = metric()
    second["valid_from"] = "2026-02-01T00:00:00Z"
    documents = {"metrics": [first, second]}
    before = deepcopy(documents)
    report = validate(metrics=documents)
    assert report.errors == ()
    assert report.metric_count == 1
    assert documents == before


def test_unknown_query_document_collection_is_visible() -> None:
    report = validator.validate_metric_binding_documents(
        program_id="demo", metric_documents={}, query_documents={Path("unknown.yaml"): {"bindings": []}},
    )
    assert report.errors


def test_empty_authored_documents_and_missing_query_id_are_invalid() -> None:
    report = validator.validate_metric_binding_documents(
        program_id="demo", metric_documents={METRICS: None}, query_documents={KPIS: None},
    )
    assert len(report.errors) == 2
    entry = query()
    del entry["id"]
    assert any("Query id" in error for error in validate(queries={"kpis": [entry]}).errors)


def test_golden_wiql_inference_and_kpi_default_are_preserved() -> None:
    entry = query()
    del entry["kql"]
    entry["wiql"] = "SELECT [System.Id] FROM WorkItems"
    report = validator.validate_metric_binding_documents(
        program_id="demo",
        metric_documents={METRICS: [metric()]},
        query_documents={GOLDEN: {"queries": [entry]}},
    )
    assert report.errors == ()
    assert any("kql" in error for error in validate(queries={"kpis": [entry]}).errors)


def test_live_validator_still_requires_probe_and_sets_validation_evidence() -> None:
    definition = MetricDefinition(id="demo.count", title="Count", unit="count", aggregation=MetricAggregation.LAST)
    binding = MetricSourceBinding(
        binding_id="query", metric_id=definition.id, program_id="demo", source_kind="kusto",
        cluster="synthetic", database="synthetic", kql_template="print Count=1;", result_column="Count",
    )
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    validated = validator.validate_metric_source_binding(
        binding, metric_definitions={definition.id: definition},
        probe=lambda _: ([{"Count": 1}], (KustoColumn(name="Count", type_name="long"),)),
        validated_at=now,
    )
    assert validated.validated and validated.last_validated_at == now
    assert validated.last_validated_kql_hash == validator.compute_metric_binding_validation_hash(binding)
    assert not binding.validated
    with pytest.raises(ConfigError, match="missing cluster"):
        validator.validate_metric_source_binding(
            MetricSourceBinding(binding_id="bad", metric_id=definition.id, program_id="demo", source_kind="kusto"),
            metric_definitions={definition.id: definition}, probe=lambda _: ([], ()), validated_at=now,
        )
    with pytest.raises(QueryError, match="not returned"):
        validator.validate_metric_source_binding(
            binding, metric_definitions={definition.id: definition},
            probe=lambda _: ([], ()), validated_at=now,
        )


@pytest.mark.parametrize("field,value,message", [
    ("metric_id", "missing.metric", "not found"),
    ("database", "", "missing database"),
    ("kql_template", "", "missing kql_template"),
    ("result_column", "", "missing result_column"),
])
def test_live_binding_metadata_validation_is_preserved(field: str, value: str, message: str) -> None:
    definition = MetricDefinition(id="demo.count", title="Count", unit="count", aggregation=MetricAggregation.LAST)
    binding = MetricSourceBinding(
        binding_id="query", metric_id=definition.id, program_id="demo", source_kind="kusto",
        cluster="synthetic", database="synthetic", kql_template="print Count=1", result_column="Count",
    )
    with pytest.raises(ConfigError, match=message):
        validator.validate_metric_source_binding(
            replace(binding, **{field: value}), metric_definitions={definition.id: definition},
            probe=lambda _: pytest.fail("malformed binding reached live probe"),
            validated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("schema,rows,message", [
    ((KustoColumn(name="Count", type_name="string"),), [{"Count": "one"}], "not numeric"),
    ((KustoColumn(name="Count", type_name="long"),), [{"Count": 1}], "dimension column"),
])
def test_live_result_contract_remains_separate_from_offline_validation(
    schema: tuple[KustoColumn, ...], rows: list[dict[str, object]], message: str,
) -> None:
    definition = MetricDefinition(
        id="demo.count", title="Count", unit="count", aggregation=MetricAggregation.LAST,
        dimension_columns=("Region",),
    )
    binding = MetricSourceBinding(
        binding_id="query", metric_id=definition.id, program_id="demo", source_kind="kusto",
        cluster="synthetic", database="synthetic", kql_template="print Count=1", result_column="Count",
    )
    with pytest.raises(QueryError, match=message):
        validator.validate_metric_source_binding(
            binding, metric_definitions={definition.id: definition},
            probe=lambda _: (rows, schema), validated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )


@pytest.mark.parametrize("schema,rows,expected", [
    ((KustoColumn(name="Count", type_name="long"),), [], True),
    ((KustoColumn(name="other", type_name="long"),), [{"other": 1, " COUNT ": 2}], True),
    ((KustoColumn(name="Count", type_name=None),), [{"Count": False}], False),
    ((), [{"Count": 2.5}], True),
    ((), [{"Count": None}], False),
])
def test_numeric_probe_fallback_semantics_are_preserved(
    schema: tuple[KustoColumn, ...], rows: list[dict[str, object]], expected: bool,
) -> None:
    assert validator.column_is_numeric(schema, "count", rows) is expected


def test_live_probe_builder_uses_injected_synthetic_client_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str]] = []

    class SyntheticClient:
        def execute_with_schema(
            self, cluster: str, database: str, kql: str,
        ) -> tuple[list[dict[str, object]], tuple[KustoColumn, ...]]:
            calls.append((cluster, database, kql))
            return [], ()

    monkeypatch.setattr(validator, "KustoClient", SyntheticClient)
    binding = MetricSourceBinding(binding_id="empty", metric_id="demo.count", program_id="demo", source_kind="kusto")
    assert validator.build_live_metric_binding_probe()(binding) == ([], ())
    assert calls == [("", "", "\n| take 1")]
    wiql_binding = replace(binding, source_kind="wiql", kql_template="SELECT [System.Id] FROM WorkItems")
    assert validator.compute_metric_binding_validation_hash(wiql_binding) == validator.compute_metric_validation_kql_hash(
        wiql_binding.kql_template or "",
    )
