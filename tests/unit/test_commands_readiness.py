from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from typer.testing import CliRunner

from cli import app
from src.core.incident_journal_store import append_incident_entry
from src.core.models import Confidence
from src.core.models_v2 import IncidentEntry


runner = CliRunner()


def test_readiness_fetch_and_show_commands_render_snapshot(monkeypatch, tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    program_dir.joinpath("program.yaml").write_text(
        """
schema_version: '2.0'
id: demo
ado:
  organization: your-org
  project: One
  api_timeout_seconds: 30
""".strip(),
        encoding="utf-8",
    )
    program_dir.joinpath("readiness.yaml").write_text(
        """
schema_version: '1.0'
snapshot_max_age_days: 7
dimensions:
  slo_definition_complete:
    source:
      type: ado_query
      query_id: readiness/slo
    pass_condition:
      kind: all_work_items_in_states
      allowed_states: [Done]
  dependency_health:
    source:
      type: dependency_health
    pass_condition:
      kind: no_high_risk_first_hop
  rollback_plan:
    source:
      type: manual_attestation
      attested_at: '2099-01-01'
      attested_by: operator
    pass_condition:
      kind: attested_within_days
      days: 36500
  support_handoff_complete:
    source:
      type: workstream_risk
      workstream_id: acme
    pass_condition:
      kind: max_risk_level
      risk_level: medium
  dora_change_fail_rate:
    source:
      type: kusto_query
      query_id: readiness_dora_fail_rate
    pass_condition:
      kind: numeric_threshold
      operator: '<'
      threshold: 15
      result_column: fail_rate
custom_dimensions:
  compliance-attestation:
    source:
      type: people_directory
      alias: operator
    pass_condition:
      kind: alias_exists
""".strip(),
        encoding="utf-8",
    )
    program_dir.joinpath("risk_register.yaml").write_text(
        """
schema_version: '1.0'
risks:
  - id: acme-rollout
    program_id: demo
    title: Acme rollout coordination
    description: Handoff work remains active but within acceptable range.
    probability: possible
    impact: medium
    category: dependency
    owner_alias: operator
    mitigation_plan: Close the remaining handoff checklist.
    mitigation_due_date: 2026-05-20
    linked_workstream_ids: [acme]
    linked_work_item_ids: []
    linked_milestone_ids: []
    linked_claim_ids: []
    linked_action_ids: []
    status: open
    identified_date: 2026-05-01
    identified_in_vertex_issue: 77
    last_reviewed_date: 2026-05-18
    entity_refs: [WI:900001]
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr("src.commands.readiness.PROGRAMS_ROOT", programs_root)
    monkeypatch.setattr(
        "src.commands.readiness._load_ado_query_rows",
        lambda program_id, dimension, programs_root: [
            {"fields": {"System.Id": 101, "System.State": "Done", "System.Title": "SLO 1"}},
            {"fields": {"System.Id": 102, "System.State": "Done", "System.Title": "SLO 2"}},
        ],
    )
    monkeypatch.setattr(
        "src.commands.readiness._load_kusto_query_rows",
        lambda program_id, dimension, programs_root: [{"fail_rate": 12.5}],
    )
    monkeypatch.setattr("src.commands.readiness._alias_exists", lambda program_id, alias, programs_root: True)
    monkeypatch.setattr(
        "src.core.dependency_graph.load_dependencies",
        lambda program_id, programs_root: (),
    )

    fetch_result = runner.invoke(app, ["readiness", "fetch", "--program", "demo"])

    assert fetch_result.exit_code == 0
    assert "Launch Readiness - demo" in fetch_result.stdout
    assert "Score: 6/6 passed" in fetch_result.stdout
    assert program_dir.joinpath("runtime", "readiness_snapshot.yaml").exists()

    show_result = runner.invoke(app, ["readiness", "--program", "demo"])

    assert show_result.exit_code == 0
    assert "QG-RD1" in show_result.stdout
    assert "QG-RD7" in show_result.stdout
    assert "QG-RD-compliance-attestation" in show_result.stdout
    assert "Score: 6/6 passed" in show_result.stdout


def test_readiness_fetch_surfaces_recent_attributed_incident_learning(tmp_path: Path, monkeypatch) -> None:
    programs_root = tmp_path / "programs"
    program_dir = programs_root / "demo"
    program_dir.mkdir(parents=True)
    program_dir.joinpath("program.yaml").write_text(
        """
schema_version: '2.0'
id: demo
ado:
  organization: your-org
  project: One
  api_timeout_seconds: 30
""".strip(),
        encoding="utf-8",
    )
    program_dir.joinpath("readiness.yaml").write_text(
        """
schema_version: '1.0'
snapshot_max_age_days: 7
custom_dimensions:
  recent_incident_learning:
    name: Recent attributed incident learnings
    source:
      type: incident_journal
    pass_condition:
      kind: max_recent_incidents
      days: 14
      threshold: 0
""".strip(),
        encoding="utf-8",
    )
    append_incident_entry(
        IncidentEntry(
            schema_version="1.0",
            program_id="demo",
            incident_id="4201",
            signal_id="sig-icm-1",
            # Relative to "now" so the 14-day "recent" window (evaluated at fetch
            # time) always includes it — fixed past dates make this test brittle.
            observed_at=datetime.now(timezone.utc) - timedelta(days=5),
            recorded_at=datetime.now(timezone.utc) - timedelta(days=5) + timedelta(minutes=5),
            belief_change_summary="IcM 4201: WI:1001 launch validation regressed during failover.",
            workstream_id="acme",
            owning_team="Acme",
            severity=2,
            source_path="icm://4201",
            query_id="query-1",
            linked_work_item_ids=(1001,),
            ado_entity_refs=("WI:1001",),
            raw_ref="raw-1",
            confidence=Confidence.HIGH,
        ),
        programs_root=programs_root,
    )

    monkeypatch.setattr("src.commands.readiness.PROGRAMS_ROOT", programs_root)

    fetch_result = runner.invoke(app, ["readiness", "fetch", "--program", "demo"])

    assert fetch_result.exit_code == 0
    assert "Score: 0/1 passed" in fetch_result.stdout
    assert "QG-RD-recent_incident_learning" in fetch_result.stdout
    assert "1 attribution-backed incident learning recorded" in fetch_result.stdout
    assert "IcM:4201" in fetch_result.stdout


def test_fetch_readiness_snapshot_uses_program_fact_loaders(
    monkeypatch,
    tmp_path: Path,
) -> None:
    from src.commands import readiness
    from src.core.readiness_engine import ReadinessConfig

    programs_root = tmp_path / "programs"
    sentinel_dependency_snapshot = object()
    sentinel_risk_snapshot = object()
    sentinel_dependency = object()
    sentinel_risk = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        readiness,
        "load_readiness_config",
        lambda program_id, programs_root: ReadinessConfig(
            program_id=program_id,
            snapshot_max_age_days=7,
            dimensions=(),
        ),
    )

    def _load_program_facts(program_id: str, *, programs_root: Path, fact_types: tuple[str, ...]):
        calls = captured.setdefault("calls", [])
        assert isinstance(calls, list)
        calls.append((program_id, programs_root, fact_types))
        if fact_types == ("dependency.link",):
            return sentinel_dependency_snapshot
        if fact_types == ("risk.entry",):
            return sentinel_risk_snapshot
        raise AssertionError(f"unexpected fact_types: {fact_types}")

    monkeypatch.setattr(readiness, "load_program_facts", _load_program_facts)
    monkeypatch.setattr(
        readiness,
        "project_dependencies",
        lambda snapshot: (sentinel_dependency,) if snapshot is sentinel_dependency_snapshot else (),
    )
    monkeypatch.setattr(
        readiness,
        "project_risk_entries",
        lambda snapshot: (sentinel_risk,) if snapshot is sentinel_risk_snapshot else (),
    )

    snapshot = object()
    snapshot_path = programs_root / "acme" / "readiness_snapshot.yaml"

    def _build_readiness_snapshot(program_id, config, *, loaders):
        captured["dependencies"] = loaders.load_dependencies()
        captured["risks"] = loaders.load_risk_entries()
        return snapshot

    monkeypatch.setattr(readiness, "build_readiness_snapshot", _build_readiness_snapshot)
    monkeypatch.setattr(
        readiness,
        "write_readiness_snapshot",
        lambda program_id, snapshot, programs_root: snapshot_path,
    )

    loaded_snapshot, loaded_path = readiness.fetch_readiness_snapshot(
        "acme",
        programs_root=programs_root,
    )

    assert loaded_snapshot is snapshot
    assert loaded_path == snapshot_path
    assert captured == {
        "calls": [
            ("acme", programs_root, ("dependency.link",)),
            ("acme", programs_root, ("risk.entry",)),
        ],
        "dependencies": (sentinel_dependency,),
        "risks": (sentinel_risk,),
    }