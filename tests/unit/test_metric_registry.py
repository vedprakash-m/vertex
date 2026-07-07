from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from src.core.edition_resolver import load_program
from src.core.metric_registry import load_metric_definition_map


def test_load_metric_definition_map_reads_active_definition(tmp_path: Path) -> None:
    metrics_root = tmp_path / "knowledge" / "metrics"
    metrics_root.mkdir(parents=True)
    (metrics_root / "acme.yaml").write_text(
        """
metrics:
  - id: acme.cluster_count
    title: Cluster count
    unit: count
    aggregation: last
    freshness_tier: cold
    valid_from: 2026-01-01T00:00:00Z
    valid_until: 2026-05-01T00:00:00Z
  - id: acme.cluster_count
    title: Cluster count
    unit: count
    aggregation: last
    freshness_tier: hot
    expected_pipeline_lag_minutes: 30
    valid_from: 2026-05-01T00:00:00Z
""".strip(),
        encoding="utf-8",
    )

    definitions = load_metric_definition_map(
        metrics_root=metrics_root,
        as_of=datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc),
    )

    definition = definitions["acme.cluster_count"]
    assert definition.freshness_tier == "hot"
    assert definition.expected_pipeline_lag_minutes == 30


def test_load_program_parses_reality_expected_gather_cadence_hours(tmp_path: Path) -> None:
    program_dir = tmp_path / "demo"
    program_dir.mkdir(parents=True)
    (program_dir / "program.yaml").write_text(
        """
schema_version: '3.0'
id: demo
name: Demo Program
reality:
  expected_gather_cadence_hours: 24
""".strip(),
        encoding="utf-8",
    )

    program = load_program("demo", programs_root=tmp_path)

    assert program is not None
    assert program.expected_gather_cadence_hours == 24.0


def test_load_metric_definition_map_includes_live_nova_fleet_health_definitions() -> None:
    metrics_root = Path(__file__).resolve().parents[2] / "knowledge" / "metrics"
    if not (metrics_root / "acme.yaml").exists():
        import pytest
        pytest.skip("Requires local acme metrics data")

    definitions = load_metric_definition_map(metrics_root=metrics_root)

    assert "acme.vs_fabric_p50_delta" in definitions
    assert "acme.node_availability_7d" in definitions
    assert "acme.customer_account_count" in definitions
    assert "acme.ready_stamp_count" in definitions
    assert "acme.repair_mttr_p50" in definitions
    assert "acme.docking_forecast_30d" in definitions
    assert "acme.specific_icm_count" in definitions
    assert "acme.stg_validation_open" in definitions
    assert "acme.fabric_parity_gap_count" in definitions
    assert "acme.deployment_p50_mins" in definitions
    assert "acme.buildout_slo_pct" in definitions
    assert "acme.fleet_size" in definitions