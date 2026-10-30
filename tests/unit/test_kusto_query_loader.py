from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.core.exceptions import ConfigError
from src.core.knowledge_store import KnowledgeStore
from src.core.kusto_query_loader import kpi_queries_for_workstream, load_kpi_queries
from src.core.models_v2 import KustoQuery


@pytest.mark.parametrize("identity", ["matching", "different", "missing"])
@pytest.mark.parametrize("explicit", [False, True])
def test_overlay_requires_success_identity_for_validation_time(
    identity: str, explicit: bool,
) -> None:
    from src.core.kusto_query_loader import _apply_gather_query_state, query_history_contract_identity

    query = KustoQuery(
        id="rate", cluster="synthetic", database="synthetic", kql="print Rate=1",
        section="Rate", render_as="metric_highlight", confidence="high", validated=True,
    )
    state = {
        "last_succeeded_at": "2026-10-24T00:00:00+00:00",
        "last_cycle_succeeded": False,
    }
    if explicit:
        state["validated_at"] = "2026-10-25T00:00:00+00:00"
    if identity != "missing":
        state["history_contract_identity"] = (
            query_history_contract_identity(query) if identity == "matching" else "different"
        )
    loaded = _apply_gather_query_state(query, state)
    assert loaded.validated is True
    assert loaded.last_cycle_succeeded is False
    expected = datetime(2026, 10, 25 if explicit else 24, tzinfo=timezone.utc)
    assert loaded.validated_at == (expected if identity == "matching" else None)


@pytest.mark.parametrize("source", ["kpi", "golden"])
@pytest.mark.parametrize("program_ids", [[], ["synthetic"], ["other", "synthetic"], ["other"]])
def test_program_scope_admission_precedes_state_overlay(
    source: str, program_ids: list[str], monkeypatch, tmp_path: Path,
) -> None:
    program_dir = tmp_path / "synthetic"
    knowledge_dir = program_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    path = program_dir / "kpis.yaml" if source == "kpi" else knowledge_dir / "golden_queries.yaml"
    collection = "kpis" if source == "kpi" else "queries"
    path.write_text(json.dumps({collection: [{
        "id": "rate", "program_ids": program_ids, "kql": "print Rate=0",
        "metric_id": "synthetic.rate", "result_column": "Rate", "unit": "%",
        "slo_target": 0, "comparison": ">=",
    }]}), encoding="utf-8")
    (program_dir / "gather_state.json").write_text(json.dumps({
        "schema_version": "2.0", "queries": {"rate": {
            "last_cycle_succeeded": False, "last_succeeded_at": "2026-10-24T00:00:00+00:00",
        }},
    }), encoding="utf-8")
    from src.core import kusto_query_loader

    overlaid: list[str] = []
    apply_state = kusto_query_loader._apply_gather_query_state

    def track_overlay(query, state):
        overlaid.append(query.id)
        return apply_state(query, state)

    monkeypatch.setattr(kusto_query_loader, "_apply_gather_query_state", track_overlay)
    queries = load_kpi_queries("synthetic", programs_root=tmp_path)
    applicable = not program_ids or "synthetic" in program_ids
    assert [query.id for query in queries] == (["rate"] if applicable else [])
    assert overlaid == (["rate"] if applicable else [])
    if applicable:
        query = queries[0]
        assert (query.unit, query.slo_target, query.comparison) == ("%", 0, ">=")
        assert query.last_cycle_succeeded is False
        assert query.validated_at is None


@pytest.mark.parametrize("sources", [("kpi", "kpi"), ("golden", "kpi"), ("kpi", "golden")])
@pytest.mark.parametrize("excluded_first", [False, True])
@pytest.mark.parametrize("excluded", [False, True])
def test_program_scope_collision_admission(
    sources: tuple[str, str], excluded_first: bool, excluded: bool, tmp_path: Path,
) -> None:
    program_dir = tmp_path / "synthetic"
    knowledge_dir = program_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    entries = [
        {"id": "rate", "program_ids": ["synthetic"], "kql": "print Rate=1"},
        {"id": "rate", "program_ids": ["other"] if excluded else ["synthetic"], "kql": "print Rate=2"},
    ]
    pairs = list(zip(sources, entries))
    if excluded_first:
        pairs.reverse()
    for source, filename, collection in [
        ("kpi", program_dir / "kpis.yaml", "kpis"),
        ("golden", knowledge_dir / "golden_queries.yaml", "queries"),
    ]:
        filename.write_text(json.dumps({
            collection: [entry for kind, entry in pairs if kind == source],
        }), encoding="utf-8")
    if excluded:
        queries = load_kpi_queries("synthetic", programs_root=tmp_path)
        assert [query.kql for query in queries] == ["print Rate=1"]
    else:
        with pytest.raises(ConfigError, match="Duplicate Kusto query id 'rate'"):
            load_kpi_queries("synthetic", programs_root=tmp_path)


@pytest.mark.parametrize("source", ["kpi", "golden"])
@pytest.mark.parametrize("invalid", [{"slo_target": True}, {"slo_target": "NaN"}, {"id": ""}])
def test_excluded_program_queries_still_validate_authored_fields(
    source: str, invalid: dict, tmp_path: Path,
) -> None:
    program_dir = tmp_path / "synthetic"
    knowledge_dir = program_dir / "knowledge"
    knowledge_dir.mkdir(parents=True)
    path = program_dir / "kpis.yaml" if source == "kpi" else knowledge_dir / "golden_queries.yaml"
    collection = "kpis" if source == "kpi" else "queries"
    path.write_text(json.dumps({collection: [{
        "id": "rate", "program_ids": ["other"], "kql": "print Rate=0", **invalid,
    }]}), encoding="utf-8")
    with pytest.raises(ConfigError, match="slo_target.*finite|id"):
        load_kpi_queries("synthetic", programs_root=tmp_path)


@pytest.mark.parametrize("source", ["kpi", "golden"])
@pytest.mark.parametrize("target", [0, 95.5, None])
@pytest.mark.parametrize("state", [None, {}, {"last_cycle_succeeded": True}, {"last_cycle_succeeded": False}])
def test_state_overlay_preserves_authored_metric_semantics(
    source: str, target: float | None, state: dict | None, monkeypatch, tmp_path: Path,
) -> None:
    program_dir = tmp_path / "synthetic"
    program_dir.mkdir()
    query = KustoQuery(
        id="rate", cluster="synthetic", database="synthetic", kql="print Rate=0",
        section="Rate", render_as="metric_highlight", confidence="high",
        metric_id="synthetic.rate", result_column="Rate", unit="%",
        slo_target=target, comparison=">=",
    )
    if source == "kpi":
        (program_dir / "kpis.yaml").write_text(
            "kpis:\n  - id: rate\n    kql: print Rate=0\n    metric_id: synthetic.rate\n"
            "    result_column: Rate\n    unit: '%'\n    comparison: '>='\n"
            f"    slo_target: {target if target is not None else 'null'}\n",
            encoding="utf-8",
        )
    monkeypatch.setattr(
        "src.core.kusto_query_loader.load_program_knowledge",
        lambda *args, **kwargs: KnowledgeStore((), (), (), (), (query,) if source == "golden" else ()),
    )
    monkeypatch.setattr(
        "src.core.kusto_query_loader._load_gather_query_state",
        lambda *args, **kwargs: {"unmatched": {}} if state is None else {"rate": state},
    )

    loaded = load_kpi_queries("synthetic", programs_root=tmp_path)[0]

    assert loaded.unit == "%"
    assert loaded.slo_target == target
    assert loaded.comparison == ">="
    assert loaded.last_cycle_succeeded == (state or {}).get("last_cycle_succeeded")
    assert loaded.validated_at is None


@pytest.mark.parametrize("target", [".nan", ".inf", "-.inf", "'NaN'", "'inf'", "'-inf'", "'1e999'", "'unavailable'", "{}"])
def test_kpi_loader_rejects_nonfinite_slo_target(target: str, monkeypatch, tmp_path: Path) -> None:
    program_dir = tmp_path / "synthetic"
    program_dir.mkdir()
    (program_dir / "kpis.yaml").write_text(
        f"kpis:\n  - id: count\n    kql: print Count=0\n    slo_target: {target}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.core.kusto_query_loader.load_program_knowledge",
        lambda *args, **kwargs: KnowledgeStore((), (), (), (), ()),
    )
    with pytest.raises(ConfigError, match="slo_target.*finite"):
        load_kpi_queries("synthetic", programs_root=tmp_path)


@pytest.mark.parametrize("target,expected", [("null", None), ("0", 0), ("-2", -2), ("'95.5'", 95.5), ("'0'", 0)])
def test_kpi_loader_keeps_absent_and_finite_slo_target(
    target: str, expected: float | None, monkeypatch, tmp_path: Path,
) -> None:
    program_dir = tmp_path / "synthetic"
    program_dir.mkdir()
    (program_dir / "kpis.yaml").write_text(
        f"kpis:\n  - id: count\n    kql: print Count=0\n    slo_target: {target}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.core.kusto_query_loader.load_program_knowledge",
        lambda *args, **kwargs: KnowledgeStore((), (), (), (), ()),
    )
    assert load_kpi_queries("synthetic", programs_root=tmp_path)[0].slo_target == expected


def test_kpi_loader_keeps_golden_slo_target_without_program_kpis(monkeypatch, tmp_path: Path) -> None:
    query = KustoQuery(
        id="count", cluster="synthetic", database="synthetic", kql="print Count=0",
        section="Count", render_as="metric_highlight", confidence="high", slo_target=0,
    )
    monkeypatch.setattr(
        "src.core.kusto_query_loader.load_program_knowledge",
        lambda *args, **kwargs: KnowledgeStore((), (), (), (), (query,)),
    )
    assert load_kpi_queries("synthetic", programs_root=tmp_path)[0].slo_target == 0


def test_load_kpi_queries_merges_program_kpis_with_golden_queries(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-deployment-velocity
    metric_id: acme.deployment_velocity
    workstream_ids: [acme]
    cluster: https://adventure.kusto.windows.net
    database: ExampleDb4
    kql: Metrics | take 1
    section: Deployment Velocity
    render_as: metric_highlight
    confidence: high
    refresh_on_gather: true
    label: Deploy P50 (hrs)
    result_column: P50
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.core.kusto_query_loader.load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(
                KustoQuery(
                    id="velocity-p50",
                    cluster="https://adventure.kusto.windows.net",
                    database="ExampleDb4",
                    kql="Golden | take 1",
                    section="Velocity",
                    render_as="table",
                    confidence="medium",
                    program_ids=("acme",),
                ),
                KustoQuery(
                    id="other-program",
                    cluster="https://adventure.kusto.windows.net",
                    database="ExampleDb4",
                    kql="Other | take 1",
                    section="Other",
                    render_as="table",
                    confidence="low",
                    program_ids=("demo",),
                ),
            ),
        ),
    )

    queries = load_kpi_queries("acme", programs_root=programs_root)

    assert [query.id for query in queries] == ["velocity-p50", "acme-deployment-velocity"]
    assert queries[1].refresh_on_gather is True
    assert queries[1].label == "Deploy P50 (hrs)"
    assert queries[1].result_column == "P50"
    assert queries[1].metric_id == "acme.deployment_velocity"


def test_load_kpi_queries_parses_slo_semantic_fields(monkeypatch, tmp_path: Path) -> None:
    """ADF-W2.3 (Section 8.5.1): unit/slo_target/comparison are additive
    kpis.yaml fields, all optional."""
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-safety-pass-rate
    metric_id: acme.safety_pass_rate
    workstream_ids: [acme]
    cluster: https://adventure.kusto.windows.net
    database: ExampleDb4
    kql: Metrics | take 1
    section: Safety
    render_as: metric_highlight
    confidence: high
    result_column: PassRate
    unit: "%"
    slo_target: 95.0
    comparison: ">="
  - id: acme-no-slo
    metric_id: acme.no_slo_metric
    cluster: https://adventure.kusto.windows.net
    database: ExampleDb4
    kql: Metrics | take 1
    section: Other
    render_as: table
    confidence: medium
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.core.kusto_query_loader.load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(), people_profiles=(), teams=(), products=(), golden_queries=(),
        ),
    )

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    slo_query = queries["acme-safety-pass-rate"]
    assert slo_query.unit == "%"
    assert slo_query.slo_target == 95.0
    assert slo_query.comparison == ">="

    no_slo_query = queries["acme-no-slo"]
    assert no_slo_query.unit is None
    assert no_slo_query.slo_target is None
    assert no_slo_query.comparison is None


def test_load_kpi_queries_raises_for_duplicate_ids(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: velocity-p50
    workstream_ids: [acme]
    cluster: https://adventure.kusto.windows.net
    database: ExampleDb4
    kql: Metrics | take 1
    section: Deployment Velocity
    render_as: metric_highlight
    confidence: high
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.core.kusto_query_loader.load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(
                KustoQuery(
                    id="velocity-p50",
                    cluster="https://adventure.kusto.windows.net",
                    database="ExampleDb4",
                    kql="Golden | take 1",
                    section="Velocity",
                    render_as="table",
                    confidence="medium",
                    program_ids=("acme",),
                ),
            ),
        ),
    )

    with pytest.raises(ConfigError, match="Duplicate Kusto query id 'velocity-p50'"):
        load_kpi_queries("acme", programs_root=programs_root)


def test_kpi_queries_for_workstream_filters_refresh_only() -> None:
    queries = (
        KustoQuery(
            id="acme-deployment-velocity",
            cluster="https://adventure.kusto.windows.net",
            database="ExampleDb4",
            kql="Metrics | take 1",
            section="Deployment Velocity",
            render_as="metric_highlight",
            confidence="high",
            workstream_ids=("acme",),
            refresh_on_gather=True,
        ),
        KustoQuery(
            id="acme-fleet-health",
            cluster="https://adventure.kusto.windows.net",
            database="ExampleDb4",
            kql="FleetHealth | take 1",
            section="Fleet Health",
            render_as="metric_highlight",
            confidence="medium",
            workstream_ids=("acme",),
            refresh_on_gather=False,
        ),
        KustoQuery(
            id="contoso-perf-baseline",
            cluster="https://adventure.kusto.windows.net",
            database="ExampleDb4",
            kql="Performance | take 1",
            section="Performance",
            render_as="metric_highlight",
            confidence="medium",
            workstream_ids=("exd_on_exn",),
            refresh_on_gather=True,
        ),
    )

    scoped = kpi_queries_for_workstream(queries, "acme")
    refresh_scoped = kpi_queries_for_workstream(queries, "acme", refresh_only=True)

    assert [query.id for query in scoped] == ["acme-deployment-velocity", "acme-fleet-health"]
    assert [query.id for query in refresh_scoped] == ["acme-deployment-velocity"]


def test_load_kpi_queries_passes_through_phase1_extension_fields_and_merges_gather_state(
    monkeypatch, tmp_path: Path
) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-readiness-scorecard
    metric_id: acme.readiness_scorecard
    assertion_ids: [assertion-001, assertion-002]
    workstream_ids: [acme]
    program_ids: [acme]
    cluster: https://example1.kusto.windows.net
    database: ExampleDb1
    kql: print Current=0.0
    section: Readiness Scorecard
    chapter: readiness_scorecards
    render_as: metric_highlight
    confidence: low
    refresh_on_gather: false
    validated: false
    owner_alias: testowner
    expected_cardinality: scalar_required
    kusto_no_safety: true
    catalog_source:
      dashboard_name: Acme Readiness
      page_name: Readiness Scorecard
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "gather_state.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "queries": {
                    "acme-readiness-scorecard": {
                        "last_succeeded_at": "2026-05-17T14:02:14Z",
                        "last_cycle_succeeded": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.core.kusto_query_loader.load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    queries = load_kpi_queries("acme", programs_root=programs_root)

    assert len(queries) == 1
    assert queries[0].catalog_source == {"dashboard_name": "Acme Readiness", "page_name": "Readiness Scorecard"}
    assert queries[0].owner_alias == "testowner"
    assert queries[0].expected_cardinality == "scalar_required"
    assert queries[0].kusto_no_safety is True
    assert queries[0].last_cycle_succeeded is True
    assert queries[0].validated_at is None
    assert queries[0].metric_id == "acme.readiness_scorecard"
    assert queries[0].assertion_ids == ("assertion-001", "assertion-002")
    assert queries[0].chapter == "readiness_scorecards"


def test_load_kpi_queries_prefers_explicit_validated_at_from_gather_state(
    monkeypatch, tmp_path: Path
) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-deployment-p50-p90
    workstream_ids: [acme]
    cluster: https://adventure.kusto.windows.net
    database: ExampleDb4
    kql: Metrics | take 1
    section: Deployment Velocity
    render_as: metric_highlight
    confidence: high
    refresh_on_gather: true
    validated: false
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "gather_state.json").write_text(
        json.dumps(
            {
                "schema_version": "2.0",
                "queries": {
                    "acme-deployment-p50-p90": {
                        "validated_at": "2026-05-18T09:15:00Z",
                        "last_succeeded_at": "2026-05-18T09:20:00Z",
                        "last_cycle_succeeded": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.core.kusto_query_loader.load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    from src.core.kusto_query_loader import query_history_contract_identity

    state_path = program_dir / "gather_state.json"
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    query = load_kpi_queries("acme", programs_root=programs_root)[0]
    assert query.validated_at is None
    payload["queries"][query.id]["history_contract_identity"] = query_history_contract_identity(query)
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    queries = load_kpi_queries("acme", programs_root=programs_root)

    assert len(queries) == 1
    assert queries[0].validated_at == datetime(2026, 5, 18, 9, 15, tzinfo=timezone.utc)
    assert queries[0].last_cycle_succeeded is True


def test_load_kpi_queries_ignores_gather_state_when_queries_map_missing(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-fleet-size
    workstream_ids: [acme]
    cluster: https://adventure.kusto.windows.net
    database: adventure
    kql: TenantCatalogSnapshot_NOVAWithoutEAP() | summarize FleetTenants=dcount(Name)
    section: Fleet Health
    render_as: metric_highlight
    confidence: medium
""".strip(),
        encoding="utf-8",
    )
    (program_dir / "gather_state.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "integration_errors": 0,
                "integration_error_details": [],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.core.kusto_query_loader.load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    queries = load_kpi_queries("acme", programs_root=programs_root)

    assert len(queries) == 1
    assert queries[0].validated_at is None
    assert queries[0].last_cycle_succeeded is None


def test_load_kpi_queries_includes_nova_readiness_catalog_queries() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = load_kpi_queries("acme", programs_root=programs_root)

    assert {
        "readiness_observability_coverage",
        "readiness_capacity_headroom",
        "readiness_dora_fail_rate",
    }.issubset({query.id for query in queries})


def test_load_kpi_queries_includes_live_nova_fleet_parity_delta_query() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    assert "acme-vs-fabric-p50-delta" in queries
    assert queries["acme-vs-fabric-p50-delta"].assertion_ids == ("assertion-acme-vs-fabric-p50-delta",)
    assert queries["acme-vs-fabric-p50-delta"].result_column == "DeltaMins"
    assert queries["acme-vs-fabric-p50-delta"].render_as == "metric_highlight"


def test_load_kpi_queries_includes_live_nova_node_availability_query() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    assert "acme-node-availability-7d" in queries
    assert queries["acme-node-availability-7d"].result_column == "NodeAvailabilityPct"
    assert queries["acme-node-availability-7d"].render_as == "metric_highlight"


def test_load_kpi_queries_includes_live_nova_docking_forecast_query() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    assert "acme-docking-forecast-30d" in queries
    assert queries["acme-docking-forecast-30d"].metric_id == "acme.docking_forecast_30d"
    assert queries["acme-docking-forecast-30d"].engine == "wiql"
    assert queries["acme-docking-forecast-30d"].result_column == "DockingForecast30d"
    assert queries["acme-docking-forecast-30d"].render_as == "metric_highlight"


def test_load_kpi_queries_includes_live_nova_customer_account_query() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    assert "acme-customer-account-count" in queries
    assert queries["acme-customer-account-count"].result_column == "CustomerAccountCount"
    assert queries["acme-customer-account-count"].render_as == "metric_highlight"


def test_load_kpi_queries_includes_live_nova_ready_stamp_query() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    assert "acme-ready-stamp-count" in queries
    assert queries["acme-ready-stamp-count"].result_column == "ReadyStampCount"
    assert queries["acme-ready-stamp-count"].render_as == "metric_highlight"


def test_load_kpi_queries_includes_live_nova_fleet_by_stage_query() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    assert "acme-fleet-by-stage" in queries
    assert queries["acme-fleet-by-stage"].render_as == "table"
    assert queries["acme-fleet-by-stage"].result_column is None


def test_load_kpi_queries_includes_live_nova_buildout_pipeline_query() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    assert "acme-buildout-pipeline" in queries
    assert queries["acme-buildout-pipeline"].engine == "wiql"
    assert queries["acme-buildout-pipeline"].render_as == "table"
    assert queries["acme-buildout-pipeline"].result_column is None


def test_load_kpi_queries_includes_live_nova_repair_mttr_query() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    assert "acme-repair-mttr" in queries
    assert queries["acme-repair-mttr"].metric_id == "acme.repair_mttr_p50"
    assert queries["acme-repair-mttr"].assertion_ids == ("assertion-acme-repair-mttr",)
    assert queries["acme-repair-mttr"].result_column == "P50Days"
    assert queries["acme-repair-mttr"].render_as == "metric_highlight"


def test_load_kpi_queries_includes_live_nova_specific_icm_query_linkage() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    assert "acme-specific-icm-count" in queries
    assert queries["acme-specific-icm-count"].metric_id == "acme.specific_icm_count"
    assert queries["acme-specific-icm-count"].assertion_ids == ("assertion-acme-specific-icm-count",)
    assert queries["acme-specific-icm-count"].result_column == "ActiveNovaRegressionIncidents"


def test_load_kpi_queries_includes_live_nova_stg_validation_query_linkage() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    assert "acme-stg-validation-open" in queries
    assert queries["acme-stg-validation-open"].metric_id == "acme.stg_validation_open"
    assert queries["acme-stg-validation-open"].assertion_ids == ("assertion-acme-stg-validation-open",)
    assert queries["acme-stg-validation-open"].engine == "wiql"
    assert queries["acme-stg-validation-open"].result_column == "OpenValidationItems"


def test_load_kpi_queries_includes_live_nova_fabric_parity_gap_query_linkage() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    assert "acme-fabric-parity-gap-count" in queries
    assert queries["acme-fabric-parity-gap-count"].metric_id == "acme.fabric_parity_gap_count"
    assert queries["acme-fabric-parity-gap-count"].assertion_ids == ("assertion-acme-fabric-parity-gap-count",)
    assert queries["acme-fabric-parity-gap-count"].result_column == "OpenFabricParityGaps"


def test_load_kpi_queries_includes_live_nova_deployment_p50_metric_linkage() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    assert "acme-deployment-p50-p90" in queries
    assert queries["acme-deployment-p50-p90"].metric_id == "acme.deployment_p50_mins"
    assert queries["acme-deployment-p50-p90"].result_column == "P50Mins"


def test_load_kpi_queries_includes_live_nova_buildout_slo_metric_linkage() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    assert "acme-buildout-slo" in queries
    assert queries["acme-buildout-slo"].metric_id == "acme.buildout_slo_pct"
    assert queries["acme-buildout-slo"].result_column == "BuildoutSloPct"


def test_load_kpi_queries_includes_live_nova_fleet_size_metric_linkage() -> None:
    programs_root = Path(__file__).resolve().parents[2] / "programs"
    if not (programs_root / "acme" / "knowledge" / "kpi_queries.yaml").exists():
        import pytest
        pytest.skip("Requires local acme kpi queries data")

    queries = {query.id: query for query in load_kpi_queries("acme", programs_root=programs_root)}

    assert "acme-fleet-size" in queries
    assert queries["acme-fleet-size"].metric_id == "acme.fleet_size"
    assert queries["acme-fleet-size"].result_column == "FleetTenants"


def test_load_kpi_queries_preserves_wiql_engine_fields(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "acme"
    program_dir.mkdir(parents=True)
    (program_dir / "kpis.yaml").write_text(
        """
schema_version: "1.0"
kpis:
  - id: acme-stg-validation-open
    workstream_ids: [acme]
    engine: wiql
    wiql: SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = '{current_iteration_path}'
    section: Scenarios / STG Sign-Off
    render_as: metric_highlight
    confidence: medium
    refresh_on_gather: true
    result_column: OpenValidationItems
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "src.core.kusto_query_loader.load_program_knowledge",
        lambda program_id, programs_root: KnowledgeStore(
            people_directory=(),
            people_profiles=(),
            teams=(),
            products=(),
            golden_queries=(),
        ),
    )

    queries = load_kpi_queries("acme", programs_root=programs_root)

    assert len(queries) == 1
    assert queries[0].engine == "wiql"
    assert queries[0].wiql == "SELECT [System.Id] FROM WorkItems WHERE [System.IterationPath] = '{current_iteration_path}'"
    assert queries[0].result_column == "OpenValidationItems"