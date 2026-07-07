from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

from src.core import readiness_engine
from src.core.readiness_engine import (
  ReadinessConfig,
  ReadinessDimensionConfig,
  ReadinessFetchLoaders,
  ReadinessPassCondition,
  ReadinessSourceConfig,
  build_readiness_snapshot,
  load_readiness_config,
  load_readiness_snapshot,
  write_readiness_snapshot,
)
from src.core.models_v2 import Dependency, DependencyScheduleStatus, DependencyStatus, DependencyType, RiskCategory, RiskEntry, RiskImpact, RiskProbability, RiskStatus


def test_build_readiness_snapshot_writes_signed_snapshot_and_rejects_tampering(tmp_path: Path) -> None:
  programs_root = tmp_path / "programs"
  snapshot = build_readiness_snapshot(
    "demo",
    ReadinessConfig(
      program_id="demo",
      snapshot_max_age_days=7,
      dimensions=(
        ReadinessDimensionConfig(
          id="rollback_plan",
          name="Rollback plan",
          gate_id="QG-RD4",
          source=ReadinessSourceConfig(
            type="manual_attestation",
            attested_at=date(2026, 5, 18),
            attested_by="operator",
          ),
          pass_condition=ReadinessPassCondition(kind="attested_within_days", days=30),
        ),
        ReadinessDimensionConfig(
          id="compliance-attestation",
          name="Compliance attestation",
          gate_id="QG-RD-compliance-attestation",
          source=ReadinessSourceConfig(
            type="manual_attestation",
            attested_at=date(2026, 3, 1),
            attested_by="owner",
          ),
          pass_condition=ReadinessPassCondition(kind="attested_within_days", days=30),
        ),
      ),
    ),
    loaders=ReadinessFetchLoaders(),
    fetched_at=datetime(2026, 5, 20, 15, 30, tzinfo=timezone.utc),
  )

  snapshot_path = write_readiness_snapshot("demo", snapshot, programs_root=programs_root)
  loaded = load_readiness_snapshot("demo", programs_root=programs_root)

  assert loaded.snapshot is not None
  assert loaded.snapshot.passed_count == 1
  assert loaded.snapshot.total_count == 2
  assert loaded.snapshot.dimensions[1].gate_id == "QG-RD-compliance-attestation"
  assert not loaded.warnings

  tampered = snapshot_path.read_text(encoding="utf-8").replace("2d old", "3d old")
  snapshot_path.write_text(tampered, encoding="utf-8")

  tampered_load = load_readiness_snapshot("demo", programs_root=programs_root)

  assert tampered_load.snapshot is None
  assert tampered_load.warnings
  assert "hash mismatch" in tampered_load.warnings[0].lower()


def test_build_readiness_snapshot_default_fact_loaders_use_program_facts(monkeypatch) -> None:
  dependency_snapshot = object()
  risk_snapshot = object()
  dependency = Dependency(
    id="dep-1",
    from_program_id="demo",
    from_workstream_id="acme",
    from_item_id=None,
    from_milestone_id=None,
    to_program_id="partner",
    to_workstream_id="buildouts",
    to_item_id=None,
    to_milestone_id=None,
    dependency_type=DependencyType.BLOCKS,
    risk_if_broken="Partner rollout slips.",
    mitigation=None,
    status=DependencyStatus.ACTIVE,
    owner_alias="owner",
    resolution_path="cross_org_compute_pf",
    planned_resolution_date=None,
    schedule_status=DependencyScheduleStatus.OK,
  )
  risk = RiskEntry(
    id="risk-1",
    program_id="demo",
    title="Launch blocker",
    description="Readiness blocker remains open.",
    probability=RiskProbability.LIKELY,
    impact=RiskImpact.HIGH,
    category=RiskCategory.SCHEDULE,
    owner_alias="operator",
    mitigation_plan=None,
    mitigation_due_date=None,
    linked_workstream_ids=("acme",),
    linked_work_item_ids=(),
    linked_milestone_ids=(),
    linked_claim_ids=(),
    linked_action_ids=(),
    status=RiskStatus.OPEN,
    identified_date=date(2026, 5, 1),
    identified_in_vertex_issue=77,
    last_reviewed_date=date(2026, 5, 19),
    entity_refs=("WI:900001",),
  )
  captured: list[tuple[str, tuple[str, ...]]] = []

  def _load_program_facts(program_id: str, *, programs_root: Path, fact_types: tuple[str, ...]):
    captured.append((program_id, fact_types))
    if fact_types == ("dependency.link",):
      return dependency_snapshot
    if fact_types == ("risk.entry",):
      return risk_snapshot
    raise AssertionError(f"unexpected fact_types: {fact_types}")

  monkeypatch.setattr(readiness_engine, "load_program_facts", _load_program_facts)
  monkeypatch.setattr(
    readiness_engine,
    "project_dependencies",
    lambda snapshot: (dependency,) if snapshot is dependency_snapshot else (),
  )
  monkeypatch.setattr(
    readiness_engine,
    "project_risk_entries",
    lambda snapshot: (risk,) if snapshot is risk_snapshot else (),
  )

  snapshot = build_readiness_snapshot(
    "demo",
    ReadinessConfig(
      program_id="demo",
      snapshot_max_age_days=7,
      dimensions=(
        ReadinessDimensionConfig(
          id="dependency_health",
          name="Dependency health",
          gate_id="QG-RD2",
          source=ReadinessSourceConfig(type="dependency_health"),
          pass_condition=ReadinessPassCondition(kind="no_high_risk_first_hop"),
        ),
        ReadinessDimensionConfig(
          id="support_handoff_complete",
          name="Support handoff complete",
          gate_id="QG-RD7",
          source=ReadinessSourceConfig(type="workstream_risk", workstream_id="acme"),
          pass_condition=ReadinessPassCondition(kind="max_risk_level", risk_level="medium"),
        ),
      ),
    ),
    loaders=ReadinessFetchLoaders(),
    fetched_at=datetime(2026, 5, 20, 15, 30, tzinfo=timezone.utc),
  )

  assert [result.source_type for result in snapshot.dimensions] == ["dependency_health", "workstream_risk"]
  assert captured == [
    ("demo", ("dependency.link",)),
    ("demo", ("risk.entry",)),
  ]


def test_load_readiness_config_and_build_snapshot_supports_workstream_risk(tmp_path: Path) -> None:
  programs_root = tmp_path / "programs"
  program_dir = programs_root / "demo"
  program_dir.mkdir(parents=True)
  (program_dir / "readiness.yaml").write_text(
    """schema_version: \"1.0\"
snapshot_max_age_days: 7
dimensions:
  support_handoff_complete:
    source:
      type: workstream_risk
      workstream_id: acme
    pass_condition:
      kind: max_risk_level
      risk_level: medium
""",
    encoding="utf-8",
  )

  config = load_readiness_config("demo", programs_root=programs_root)
  snapshot = build_readiness_snapshot(
    "demo",
    config,
    loaders=ReadinessFetchLoaders(
      load_risk_entries=lambda: (
        RiskEntry(
          id="risk-1",
          program_id="demo",
          title="Launch blocker",
          description="Readiness blocker remains open.",
          probability=RiskProbability.LIKELY,
          impact=RiskImpact.HIGH,
          category=RiskCategory.SCHEDULE,
          owner_alias="operator",
          mitigation_plan=None,
          mitigation_due_date=None,
          linked_workstream_ids=("acme",),
          linked_work_item_ids=(),
          linked_milestone_ids=(),
          linked_claim_ids=(),
          linked_action_ids=(),
          status=RiskStatus.OPEN,
          identified_date=date(2026, 5, 1),
          identified_in_vertex_issue=77,
          last_reviewed_date=date(2026, 5, 19),
          entity_refs=("WI:900001",),
        ),
      ),
    ),
    fetched_at=datetime(2026, 5, 20, 15, 30, tzinfo=timezone.utc),
  )

  assert config.dimensions[0].source.workstream_id == "acme"
  assert config.dimensions[0].pass_condition.risk_level == "medium"
  assert snapshot.dimensions[0].source_type == "workstream_risk"
  assert not snapshot.dimensions[0].passed
  assert snapshot.dimensions[0].observed_value == "high"
  assert snapshot.dimensions[0].details == {
    "workstream_id": "acme",
    "risk_level": "high",
    "threshold_risk_level": "medium",
    "risk_ids": ["risk-1"],
  }


def test_nova_readiness_config_covers_full_default_rd_gate_series() -> None:
  programs_root = Path(__file__).resolve().parents[2] / "programs"
  if not (programs_root / "acme" / "readiness.yaml").exists():
      import pytest
      pytest.skip("Requires local acme readiness config data")

  config = load_readiness_config("acme", programs_root=programs_root)

  assert [dimension.id for dimension in config.dimensions[:8]] == [
    "slo_definition_complete",
    "dependency_health",
    "observability_coverage",
    "rollback_plan",
    "capacity_validation",
    "incident_response_owner",
    "support_handoff_complete",
    "dora_change_fail_rate",
  ]
  assert [dimension.gate_id for dimension in config.dimensions[:8]] == [
    "QG-RD1",
    "QG-RD2",
    "QG-RD3",
    "QG-RD4",
    "QG-RD5",
    "QG-RD6",
    "QG-RD7",
    "QG-RD8",
  ]
  assert [dimension.id for dimension in config.dimensions[8:]] == [
    "recent_incident_learning",
    "xstore_launch_risk",
    "dd_pilot_readiness_risk",
  ]
  assert {
    dimension.id: dimension.source.query_id
    for dimension in config.dimensions
    if dimension.source.type == "kusto_query"
  } == {
    "observability_coverage": "readiness_observability_coverage",
    "capacity_validation": "readiness_capacity_headroom",
    "dora_change_fail_rate": "readiness_dora_fail_rate",
  }


def test_build_readiness_snapshot_dependency_health_highlights_cross_org_failures() -> None:
  snapshot = build_readiness_snapshot(
    "demo",
    ReadinessConfig(
      program_id="demo",
      snapshot_max_age_days=7,
      dimensions=(
        ReadinessDimensionConfig(
          id="dependency_health",
          name="Dependency health",
          gate_id="QG-RD2",
          source=ReadinessSourceConfig(type="dependency_health"),
          pass_condition=ReadinessPassCondition(kind="no_high_risk_first_hop"),
        ),
      ),
    ),
    loaders=ReadinessFetchLoaders(
      load_dependencies=lambda: (
        Dependency(
          id="dep-cross-org",
          from_program_id="demo",
          from_workstream_id="acme",
          from_item_id=None,
          from_milestone_id=None,
          to_program_id="partner",
          to_workstream_id="buildouts",
          to_item_id=None,
          to_milestone_id=None,
          dependency_type=DependencyType.BLOCKS,
          risk_if_broken="Partner rollout slips.",
          mitigation=None,
          status=DependencyStatus.ACTIVE,
          owner_alias="owner",
          resolution_path="cross_org_compute_pf",
          planned_resolution_date=None,
          schedule_status=DependencyScheduleStatus.BLOCKED,
        ),
      ),
    ),
    fetched_at=datetime(2026, 5, 20, 15, 30, tzinfo=timezone.utc),
  )

  result = snapshot.dimensions[0]

  assert result.source_type == "dependency_health"
  assert not result.passed
  assert result.observed_value == "1 at-risk (1 cross-org)"
  assert "including 1 cross-org dependency" in result.summary
  assert result.details == {
    "failing_dependency_ids": ["dep-cross-org"],
    "cross_org_failing_dependency_ids": ["dep-cross-org"],
  }