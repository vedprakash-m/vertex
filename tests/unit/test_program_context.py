"""
Unit tests for src/core/program_context.py — ProgramContext compiled knowledge graph.

Zone A only. Tests use in-memory tempdir program structures (no live ADO/Kusto).
Covers: load_program_context(), invariant detection, staleness flags, maturity levels,
        ConfigError raise, default_factory correctness, stakeholder_aliases parsing.
"""

from __future__ import annotations

import textwrap
from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from src.core.exceptions import ConfigError
from src.core.models_v2 import (
    Assumption,
    AssumptionStatus,
    DecisionEntry as FactDecisionEntry,
    DecisionStatus,
    Dependency,
    DependencyStatus,
    DependencyType,
    Milestone,
    MilestoneStatus,
    RiskCategory,
    RiskEntry as FactRiskEntry,
    RiskImpact,
    RiskProbability,
    RiskStatus,
    Workstream,
)
from src.core.program_context import (
    InvariantSeverity,
    MaturityLevel,
    RegistryLaneEntry,
    load_program_context,
    load_program_stakeholder_aliases,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_yaml(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False)


def _minimal_program(tmp_path: Path, program_id: str = "test_prog") -> Path:
    """Write the bare-minimum program files for a valid L0 context."""
    prog_dir = tmp_path / "programs" / program_id
    prog_dir.mkdir(parents=True, exist_ok=True)

    _write_yaml(prog_dir / "program.yaml", {
        "schema_version": "3.0",
        "id": program_id,
        "name": "Test Program",
        "stakeholder_register": [
            {"alias": "alice", "email": "alice@microsoft.com"},
        ],
        "sub_programs": [{"id": "sub1", "name": "Sub One"}],
    })
    _write_yaml(prog_dir / "workstreams.yaml", {
        "schema_version": "1.0",
        "workstreams": [{"id": "ws1", "name": "Workstream 1"}],
    })
    _write_yaml(prog_dir / "workstream_registry.yaml", {
        "schema_version": "1.0",
        "workstreams": [{
            "id": "ws1",
            "sub_program_id": "sub1",
            "lifecycle_state": "active",
            "deep_context": {"why": "why text", "what": "what text"},
            "last_reviewed_date": str(date.today()),
            "roles": [],
            "stakeholders": [],
        }],
    })
    return tmp_path / "programs"


# ---------------------------------------------------------------------------
# Structural correctness
# ---------------------------------------------------------------------------

def test_registry_lane_entry_default_factory() -> None:
    """RegistryLaneEntry defaults must be callable (not bare ())."""
    entry = RegistryLaneEntry(id="ws1", sub_program_id="sub1")
    assert entry.roles == ()
    assert entry.kusto_queries == ()
    assert entry.stakeholder_aliases == ()


def test_load_program_context_returns_correct_program_id(tmp_path: Path) -> None:
    programs_root = _minimal_program(tmp_path)
    ctx = load_program_context("test_prog", programs_root=programs_root)
    assert ctx.program_id == "test_prog"


def test_load_program_context_minimal_is_at_least_l1(tmp_path: Path) -> None:
    """A program with well-linked registry should reach at least L1."""
    programs_root = _minimal_program(tmp_path)
    ctx = load_program_context("test_prog", programs_root=programs_root)
    assert ctx.maturity_level.value >= 1


def test_load_program_context_frozen(tmp_path: Path) -> None:
    """ProgramContext must be immutable (frozen dataclass)."""
    programs_root = _minimal_program(tmp_path)
    ctx = load_program_context("test_prog", programs_root=programs_root)
    with pytest.raises((AttributeError, TypeError)):
        ctx.program_id = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# §5 Invariant detection
# ---------------------------------------------------------------------------

def test_ws01_detects_orphan_registry_sub_program_id(tmp_path: Path) -> None:
    """WS-01: registry entry referencing non-existent sub_program_id → ERROR."""
    programs_root = _minimal_program(tmp_path)
    prog_dir = programs_root / "test_prog"
    _write_yaml(prog_dir / "workstream_registry.yaml", {
        "schema_version": "1.0",
        "workstreams": [{
            "id": "ws1",
            "sub_program_id": "nonexistent_sub",
            "lifecycle_state": "active",
            "last_reviewed_date": str(date.today()),
        }],
    })
    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)
    errors = [v for v in ctx.invariant_violations if v.code == "WS-01"]
    assert errors, "Expected WS-01 error for orphan sub_program_id"
    assert errors[0].severity == InvariantSeverity.ERROR


def test_stk01_detects_raci_alias_not_in_register(tmp_path: Path) -> None:
    """STK-01: RACI alias not in stakeholder_register → ERROR."""
    programs_root = _minimal_program(tmp_path)
    prog_dir = programs_root / "test_prog"
    _write_yaml(prog_dir / "workstreams.yaml", {
        "schema_version": "1.0",
        "workstreams": [{
            "id": "ws1",
            "name": "WS1",
            "raci": {"accountable": "unknown_alias"},
        }],
    })
    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)
    errors = [v for v in ctx.invariant_violations if v.code == "STK-01"]
    assert errors, "Expected STK-01 error for unknown RACI alias"


def test_load_program_context_uses_fact_backed_workstreams_for_raci_validation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    programs_root = _minimal_program(tmp_path)

    monkeypatch.setattr(
        "src.core.program_context.load_current_workstreams",
        lambda program_id, *, programs_root: (
            Workstream(
                id="ws-fact",
                name="Fact-backed",
                accountable_owner="alice",
                consulted_owners=("missing_consulted",),
                informed_owners=("missing_informed",),
            ),
        ),
    )

    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)

    errors = [v for v in ctx.invariant_violations if v.code == "STK-01"]
    assert [error.entity_id for error in errors] == ["ws-fact", "ws-fact"]


def test_load_program_context_uses_fact_backed_entity_loaders(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    programs_root = _minimal_program(tmp_path)

    monkeypatch.setattr("src.core.program_context.load_current_workstreams", lambda program_id, *, programs_root: ())
    monkeypatch.setattr(
        "src.core.program_context.load_current_milestones",
        lambda program_id, *, programs_root: (
            Milestone(
                id="ms-fact",
                program_id=program_id,
                name="Fact milestone",
                target_date=date(2026, 6, 10),
                owner_alias="alice",
                status=MilestoneStatus.ON_TRACK,
                exit_criteria=("Done",),
                linked_workstream_ids=("ws-fact",),
                linked_work_item_ids=(12345,),
                last_reviewed_date=date(2026, 6, 9),
            ),
        ),
    )
    monkeypatch.setattr(
        "src.core.program_context.load_current_risk_entries",
        lambda program_id, *, programs_root: (
            FactRiskEntry(
                id="risk-fact",
                program_id=program_id,
                title="Fact risk",
                description="Risk from facts",
                probability=RiskProbability.LIKELY,
                impact=RiskImpact.HIGH,
                category=RiskCategory.SCHEDULE,
                owner_alias="alice",
                mitigation_plan="Mitigate",
                mitigation_due_date=date(2026, 6, 12),
                linked_workstream_ids=("ws-fact",),
                linked_work_item_ids=(),
                linked_milestone_ids=("ms-fact",),
                linked_claim_ids=(),
                linked_action_ids=(),
                status=RiskStatus.OPEN,
                identified_date=date(2026, 6, 1),
                identified_in_vertex_issue=None,
                last_reviewed_date=date(2026, 6, 8),
                entity_refs=(),
            ),
        ),
    )
    monkeypatch.setattr(
        "src.core.program_context.load_current_decision_entries",
        lambda program_id, *, programs_root: (
            FactDecisionEntry(
                id="decision-fact",
                program_id=program_id,
                title="Fact decision",
                context="Fact context",
                decision="Do it",
                rationale=None,
                alternatives_considered=(),
                decided_by="alice",
                decision_date=date(2026, 6, 2),
                status=DecisionStatus.DECIDED,
                superseded_by=None,
                linked_claim_id=None,
                linked_risk_id="risk-fact",
                linked_action_ids=(),
                workstream_id="ws-fact",
                entity_refs=(),
                review_by=date(2026, 6, 20),
                linked_milestone_ids=("ms-fact",),
                last_reviewed_date=date(2026, 6, 7),
            ),
        ),
    )
    monkeypatch.setattr(
        "src.core.program_context.load_current_assumptions",
        lambda program_id, *, programs_root: (
            Assumption(
                id="assumption-fact",
                program_id=program_id,
                text="Fact assumption",
                validation_method="Check plan",
                validation_due=date(2026, 6, 15),
                status=AssumptionStatus.ACTIVE,
                linked_risk_id="risk-fact",
                linked_milestone_id="ms-fact",
                owner_alias="alice",
                identified_date=date(2026, 6, 3),
                entity_refs=(),
                category="schedule",
                linked_workstream_ids=("ws-fact",),
                linked_milestone_ids=("ms-fact",),
                last_reviewed_date=date(2026, 6, 6),
            ),
        ),
    )
    monkeypatch.setattr(
        "src.core.program_context.load_current_dependencies",
        lambda program_id, *, programs_root: (
            Dependency(
                id="dep-fact",
                from_program_id=program_id,
                from_workstream_id="ws-fact",
                from_item_id=None,
                from_milestone_id="ms-fact",
                to_program_id="shared",
                to_workstream_id="shared-ws",
                to_item_id=None,
                to_milestone_id=None,
                dependency_type=DependencyType.BLOCKS,
                risk_if_broken="It slips",
                mitigation="Escalate",
                status=DependencyStatus.ACTIVE,
                owner_alias="alice",
                linked_risk_ids=("risk-fact",),
            ),
        ),
    )

    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)

    assert ctx.milestones[0].last_reviewed_date == date(2026, 6, 9)
    assert ctx.risks[0].linked_milestone_ids == ("ms-fact",)
    assert ctx.decisions[0].linked_milestone_ids == ("ms-fact",)
    assert ctx.assumptions[0].linked_workstream_ids == ("ws-fact",)
    assert ctx.dependencies[0].linked_risk_ids == ("risk-fact",)


def test_date01_detects_stub_work_item_id(tmp_path: Path) -> None:
    """DATE-01: milestone linked_work_item_ids in 900000–999999 → ERROR."""
    programs_root = _minimal_program(tmp_path)
    prog_dir = programs_root / "test_prog"
    _write_yaml(prog_dir / "milestones.yaml", {
        "schema_version": "1.0",
        "milestones": [{
            "id": "ms1",
            "name": "Milestone 1",
            "linked_work_item_ids": [900001],
            "target_date": str(date.today()),
            "owner_alias": "alice",
            "status": "on_track",
        }],
    })
    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)
    errors = [v for v in ctx.invariant_violations if v.code == "DATE-01"]
    assert errors, "Expected DATE-01 error for stub WI ID"


def test_no_violations_for_clean_program(tmp_path: Path) -> None:
    """Clean minimal program should have zero ERROR violations."""
    programs_root = _minimal_program(tmp_path)
    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)
    errors = [v for v in ctx.invariant_violations if v.severity == InvariantSeverity.ERROR]
    assert not errors, f"Unexpected errors: {errors}"


# ---------------------------------------------------------------------------
# §8 Staleness
# ---------------------------------------------------------------------------

def test_staleness_flag_for_stale_workstream_registry(tmp_path: Path) -> None:
    """Per-entry staleness: registry lane reviewed > 14 days ago → staleness flag."""
    programs_root = _minimal_program(tmp_path)
    prog_dir = programs_root / "test_prog"
    stale_date = str(date.today() - timedelta(days=20))
    _write_yaml(prog_dir / "workstream_registry.yaml", {
        "schema_version": "1.0",
        "workstreams": [{
            "id": "ws1",
            "sub_program_id": "sub1",
            "lifecycle_state": "active",
            "deep_context": {"why": "why text"},
            "last_reviewed_date": stale_date,
        }],
    })
    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)
    stale = [f for f in ctx.staleness_flags if "workstream_registry" in f.file]
    assert stale, "Expected staleness flag for 20-day stale registry lane"


def test_fresh_registry_no_staleness_flag(tmp_path: Path) -> None:
    """Registry lane reviewed today → no staleness flag."""
    programs_root = _minimal_program(tmp_path)
    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)
    stale = [f for f in ctx.staleness_flags if "workstream_registry" in f.file]
    assert not stale, "Fresh lane should have no staleness flag"


# ---------------------------------------------------------------------------
# ConfigError raise on ERROR violations
# ---------------------------------------------------------------------------

def test_raise_on_error_raises_config_error(tmp_path: Path) -> None:
    """load_program_context with raise_on_error=True must raise ConfigError on ERROR violations."""
    programs_root = _minimal_program(tmp_path)
    prog_dir = programs_root / "test_prog"
    # Introduce a DATE-01 violation (stub WI ID)
    _write_yaml(prog_dir / "milestones.yaml", {
        "schema_version": "1.0",
        "milestones": [{
            "id": "ms1",
            "name": "M1",
            "linked_work_item_ids": [999999],
            "target_date": str(date.today()),
            "owner_alias": "alice",
            "status": "on_track",
        }],
    })
    with pytest.raises(ConfigError):
        load_program_context("test_prog", programs_root=programs_root, raise_on_error=True)


def test_raise_on_error_false_does_not_raise(tmp_path: Path) -> None:
    """raise_on_error=False must not raise even with ERROR violations."""
    programs_root = _minimal_program(tmp_path)
    prog_dir = programs_root / "test_prog"
    _write_yaml(prog_dir / "milestones.yaml", {
        "schema_version": "1.0",
        "milestones": [{
            "id": "ms1",
            "name": "M1",
            "linked_work_item_ids": [999999],
            "target_date": str(date.today()),
            "owner_alias": "alice",
            "status": "on_track",
        }],
    })
    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)
    assert ctx.total_invariant_errors > 0


# ---------------------------------------------------------------------------
# §16 Maturity level
# ---------------------------------------------------------------------------

def test_maturity_l0_when_no_registry(tmp_path: Path) -> None:
    """No workstream_registry.yaml → L0."""
    prog_dir = tmp_path / "programs" / "test_prog"
    prog_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(prog_dir / "program.yaml", {
        "schema_version": "3.0", "id": "test_prog", "name": "Test",
        "stakeholder_register": [], "sub_programs": [],
    })
    _write_yaml(prog_dir / "workstreams.yaml", {
        "schema_version": "1.0", "workstreams": [],
    })
    ctx = load_program_context("test_prog", programs_root=tmp_path / "programs", raise_on_error=False)
    assert ctx.maturity_level == MaturityLevel.L0


def test_maturity_level_name(tmp_path: Path) -> None:
    """level_name must return a non-empty string."""
    programs_root = _minimal_program(tmp_path)
    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)
    assert ctx.level_name


# ---------------------------------------------------------------------------
# to_summary_dict
# ---------------------------------------------------------------------------

def test_summary_dict_has_required_keys(tmp_path: Path) -> None:
    """summary_dict() must include maturity_level, invariant_errors, staleness_warnings."""
    programs_root = _minimal_program(tmp_path)
    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)
    d = ctx.summary_dict()
    assert "maturity_level" in d
    assert "invariant_errors" in d
    assert "staleness_errors" in d


# ---------------------------------------------------------------------------
# PPL-W3.5e: load_program_stakeholder_aliases (narrow accessor)
# ---------------------------------------------------------------------------

def test_load_program_stakeholder_aliases_matches_full_context_result(tmp_path: Path) -> None:
    """The narrow accessor must return exactly the same alias set the
    full `load_program_context(...).stakeholder_aliases` would, since it
    is meant as a drop-in replacement for that one field in
    `kb_checks.py::_load_program_stakeholder_aliases`'s loop."""
    programs_root = _minimal_program(tmp_path)
    ctx = load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)

    aliases = load_program_stakeholder_aliases("test_prog", programs_root=programs_root)

    assert aliases == frozenset(ctx.stakeholder_aliases)
    assert aliases == frozenset({"alice"})


def test_load_program_stakeholder_aliases_merges_top_level_and_charter(tmp_path: Path) -> None:
    """Must go through the SAME §7.1 dual-read merge `_parse_stakeholders`
    already implements (top-level ∪ charter, charter wins on conflict),
    not a naive top-level-only read."""
    prog_dir = tmp_path / "programs" / "test_prog"
    prog_dir.mkdir(parents=True, exist_ok=True)
    _write_yaml(prog_dir / "program.yaml", {
        "schema_version": "3.0",
        "id": "test_prog",
        "name": "Test Program",
        "stakeholder_register": [{"alias": "legacy_only", "role": "owner"}],
        "charter": {"stakeholder_register": [{"alias": "charter_only", "role": "reviewer"}]},
        "sub_programs": [{"id": "sub1", "name": "Sub One"}],
    })

    aliases = load_program_stakeholder_aliases("test_prog", programs_root=tmp_path / "programs")

    assert aliases == frozenset({"legacy_only", "charter_only"})


def test_load_program_stakeholder_aliases_raises_when_program_yaml_missing(tmp_path: Path) -> None:
    programs_root = tmp_path / "programs"
    (programs_root / "test_prog").mkdir(parents=True)

    with pytest.raises(ConfigError):
        load_program_stakeholder_aliases("test_prog", programs_root=programs_root)


def test_load_program_stakeholder_aliases_unaffected_by_a_malformed_other_plane1_file(tmp_path: Path) -> None:
    """PPL-W3.5e's own documented, deliberate behavior difference from
    the full `load_program_context` this replaces in
    `_load_program_stakeholder_aliases`'s loop: a malformed OTHER
    Plane-1 file (here, workstreams.yaml with a non-mapping top level)
    must NOT affect this accessor at all, since it never reads that
    file -- unlike the old full-load-then-catch-ConfigError pattern,
    which would have silently dropped this program's real, valid
    stakeholder data entirely."""
    programs_root = _minimal_program(tmp_path)
    (programs_root / "test_prog" / "workstreams.yaml").write_text("not_a_mapping\n", encoding="utf-8")

    # Confirm the premise: the FULL load_program_context really does choke on this.
    with pytest.raises(ConfigError):
        load_program_context("test_prog", programs_root=programs_root, raise_on_error=False)

    # The narrow accessor is unaffected.
    aliases = load_program_stakeholder_aliases("test_prog", programs_root=programs_root)
    assert aliases == frozenset({"alice"})
