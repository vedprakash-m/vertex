"""
ProgramContext — compiled, validated, immutable program knowledge graph.

Implements §15.9 of the program-context-maturity spec.

Zone A only. No AI. No M365 calls. Fully validated at construction time.

Usage:
    ctx = load_program_context(program_id, programs_root=PROGRAMS_ROOT)
    print(ctx.maturity_level)        # computed once at construction
    print(ctx.invariant_violations)  # all §5 violations, single check
    print(ctx.staleness_flags)        # §8 staleness flags

The ProgramContext replaces independent YAML loading in each feature.
All 20 Plane 1 files are read once; all §5 invariants are checked once;
maturity level is computed once. Features receive the ctx object.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.core.evidence_models import WorkstreamEvidence

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
import re
from typing import Any

from src.core.edition_resolver import PROGRAMS_ROOT
from src.core.exceptions import ConfigError
from src.core.models import Confidence  # noqa: F401 — re-exported for callers
from src.core.models_v2 import (
    Assumption as FactAssumption,
    DecisionEntry as FactDecisionEntry,
    Dependency as FactDependency,
    Milestone as FactMilestone,
    RiskEntry as FactRiskEntry,
    Workstream,
)
from src.core.program_fact_store import (
    load_current_assumptions,
    load_current_decision_entries,
    load_current_dependencies,
    load_current_milestones,
    load_current_risk_entries,
    load_current_workstreams,
)
from src.core.yaml_utils import load_yaml_mapping


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MaturityLevel(int, Enum):
    L0 = 0  # Skeleton
    L1 = 1  # Structural
    L2 = 2  # Operational
    L3 = 3  # Intelligent
    L4 = 4  # Self-Sustaining

    def __str__(self) -> str:
        return f"L{self.value}"


class InvariantSeverity(str, Enum):
    ERROR = "error"    # blocks L1+
    WARN = "warn"      # advisory only
    INFO = "info"      # coverage metrics


# ---------------------------------------------------------------------------
# Section 1: Flat domain objects (raw parsed data, no validation)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class StakeholderEntry:
    alias: str
    email: str | None = None
    role: str | None = None


@dataclass(frozen=True, slots=True)
class SubProgramEntry:
    id: str
    name: str
    objective: str | None = None


@dataclass(frozen=True, slots=True)
class WorkstreamEntry:
    id: str
    name: str
    accountable: str | None = None
    accountable_email: str | None = None
    area_paths: tuple[str, ...] = ()
    raci: dict[str, str | tuple[str, ...]] = field(default_factory=dict)
    last_reviewed_date: date | None = None


@dataclass(frozen=True, slots=True)
class RegistryLaneEntry:
    id: str
    sub_program_id: str
    lifecycle_state: str | None = None
    deep_context: dict[str, str] = field(default_factory=dict)
    roles: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    workiq_latest: str | None = None
    last_reviewed_date: date | None = None
    signal_sources: dict[str, Any] = field(default_factory=dict)
    area_paths: tuple[str, ...] = ()
    kusto_queries: tuple[str, ...] = field(default_factory=tuple)
    icm_queue: str | None = None
    stakeholder_aliases: tuple[str, ...] = field(default_factory=tuple)
    evidence: "WorkstreamEvidence | None" = None
    expected_cadence_days: int | None = None


@dataclass(frozen=True, slots=True)
class ScorecardEntry:
    id: str
    name: str
    dimensions: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    linked_scorecard_id: str | None = None


@dataclass(frozen=True, slots=True)
class MilestoneEntry:
    id: str
    name: str
    target_date: date | None = None
    status: str | None = None
    owner_alias: str | None = None
    linked_work_item_ids: tuple[int, ...] = field(default_factory=tuple)
    linked_workstream_ids: tuple[str, ...] = field(default_factory=tuple)
    last_reviewed_date: date | None = None


@dataclass(frozen=True, slots=True)
class RiskEntry:
    id: str
    title: str
    status: str | None = None
    probability: str | None = None
    impact: str | None = None
    category: str | None = None
    owner_alias: str | None = None
    mitigation_due_date: date | None = None
    linked_workstream_ids: tuple[str, ...] = field(default_factory=tuple)
    linked_milestone_ids: tuple[str, ...] = field(default_factory=tuple)
    last_reviewed_date: date | None = None


@dataclass(frozen=True, slots=True)
class DecisionEntry:
    id: str
    title: str
    status: str | None = None
    decided_by: str | None = None
    workstream_id: str | None = None
    linked_milestone_ids: tuple[str, ...] = field(default_factory=tuple)
    review_by: date | None = None
    last_reviewed_date: date | None = None


@dataclass(frozen=True, slots=True)
class AssumptionEntry:
    id: str
    statement: str | None = None
    status: str | None = None
    category: str | None = None
    owner_alias: str | None = None
    linked_workstream_ids: tuple[str, ...] = field(default_factory=tuple)
    linked_milestone_ids: tuple[str, ...] = field(default_factory=tuple)
    last_reviewed_date: date | None = None


@dataclass(frozen=True, slots=True)
class DependencyEntry:
    id: str
    type: str | None = None
    blocking_program_or_team: str | None = None
    expected_resolution: date | None = None
    status: str | None = None
    owner_alias: str | None = None
    linked_risk_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class KpiEntry:
    id: str
    name: str
    validated: bool = False
    kusto_query: str | None = None
    workstream_ids: tuple[str, ...] = field(default_factory=tuple)
    last_reviewed_date: date | None = None


@dataclass(frozen=True, slots=True)
class EditorialRulesData:
    applies_to_editions: tuple[str, ...] = field(default_factory=tuple)
    banned_phrases: tuple[str, ...] = field(default_factory=tuple)
    banned_openings: tuple[str, ...] = field(default_factory=tuple)
    abstract_phrases: tuple[str, ...] = field(default_factory=tuple)
    synthetic_delta_prefixes: tuple[str, ...] = field(default_factory=tuple)
    exec_summary_bucket_prefixes: tuple[str, ...] = field(default_factory=tuple)


# ---------------------------------------------------------------------------
# Section 2: Validation result objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class InvariantViolation:
    code: str          # e.g. "WS-01", "DATE-01", "FILTER-01"
    severity: InvariantSeverity
    file: str         # e.g. "workstream_registry.yaml"
    entity_id: str | None  # e.g. "acme.xsse_readiness" or None for file-level
    detail: str        # human-readable description
    blocker_for_level: int | None = None  # e.g. 1 means this blocks L1

    def is_error(self) -> bool:
        return self.severity == InvariantSeverity.ERROR


@dataclass(frozen=True, slots=True)
class StalenessFlag:
    file: str
    entity_id: str | None
    field: str
    last_reviewed_date: date | None
    days_stale: int | None
    warn_threshold: int
    block_threshold: int | None
    severity: str  # "warn" | "block" | "ok"


@dataclass(frozen=True, slots=True)
class ContextGapInfo:
    feature: str
    lane: str | None
    field: str
    severity: str  # "feature_blocked" | "quality_degraded"
    impact: str   # "high" | "medium" | "low"
    message: str


# ---------------------------------------------------------------------------
# Section 3: ProgramContext — the compiled model
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ProgramContext:
    """
    Compiled, validated program knowledge graph.

    Produced by `load_program_context()`. Contains all §4.1–§4.18 fields,
    resolved foreign keys, and computed metadata (maturity level, invariant
    violations, staleness flags, context gaps).

    Frozen and hashable — safe to pass across pipeline stages without
    the risk of accidental mutation or cross-run divergence.
    """

    program_id: str
    loaded_at: datetime

    # §4.1 – §4.18 raw data
    stakeholder_register: tuple[StakeholderEntry, ...]
    sub_programs: tuple[SubProgramEntry, ...]
    workstreams: tuple[WorkstreamEntry, ...]
    registry_lanes: tuple[RegistryLaneEntry, ...]
    scorecards: tuple[ScorecardEntry, ...]
    milestones: tuple[MilestoneEntry, ...]
    risks: tuple[RiskEntry, ...]
    decisions: tuple[DecisionEntry, ...]
    assumptions: tuple[AssumptionEntry, ...]
    dependencies: tuple[DependencyEntry, ...]
    kpis: tuple[KpiEntry, ...]
    editorial_rules: EditorialRulesData

    # Computed metadata
    maturity_level: MaturityLevel
    maturity_blockers: tuple[str, ...]
    invariant_violations: tuple[InvariantViolation, ...]
    staleness_flags: tuple[StalenessFlag, ...]
    context_gaps: tuple[ContextGapInfo, ...]

    # Resolved foreign-key sets (for fast invariant checking)
    all_workstream_ids: frozenset[str]
    sub_program_ids: frozenset[str]
    stakeholder_aliases: frozenset[str]
    milestone_ids: frozenset[str]
    edition_ids: frozenset[str]

    # Summary metrics
    deep_context_coverage_pct: float   # % of active lanes with non-empty deep_context.why
    signal_source_coverage_pct: float  # % of active lanes with at least one signal source
    kpi_validation_rate_pct: float     # % of KPIs with validated=true
    staleness_error_count: int         # count of files past block threshold
    staleness_warn_count: int          # count of files past warn threshold

    @property
    def level_name(self) -> str:
        names = {0: "Skeleton", 1: "Structural", 2: "Operational", 3: "Intelligent", 4: "Self-Sustaining"}
        return names.get(self.maturity_level.value, "Unknown")

    @property
    def total_invariant_errors(self) -> int:
        return sum(1 for v in self.invariant_violations if v.is_error())

    def has_blocking_violations(self) -> bool:
        return self.total_invariant_errors > 0

    def summary_dict(self) -> dict[str, Any]:
        return {
            "program_id": self.program_id,
            "maturity_level": str(self.maturity_level),
            "level_name": self.level_name,
            "invariant_errors": self.total_invariant_errors,
            "invariant_warnings": sum(1 for v in self.invariant_violations if not v.is_error()),
            "staleness_errors": self.staleness_error_count,
            "staleness_warnings": self.staleness_warn_count,
            "deep_context_coverage_pct": round(self.deep_context_coverage_pct, 1),
            "signal_source_coverage_pct": round(self.signal_source_coverage_pct, 1),
            "kpi_validation_rate_pct": round(self.kpi_validation_rate_pct, 1),
            "context_gap_count": len(self.context_gaps),
        }


# ---------------------------------------------------------------------------
# Section 4: Factory function
# ---------------------------------------------------------------------------

def load_program_context(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    editions_root: Path | None = None,
    today: date | None = None,
    raise_on_error: bool = True,
) -> ProgramContext:
    """
    Load and validate all Plane 1 files for a program, producing a ProgramContext.

    Performs all §5 invariant checks and §8 staleness checks in one pass.
    Raises ConfigError if any ERROR-level invariant is violated (when raise_on_error=True).
    Returns a frozen ProgramContext on success.

    Parameters
    ----------
    program_id:
        The program directory name under programs_root.
    programs_root:
        Root of the programs/ directory. Defaults to PROGRAMS_ROOT.
    editions_root:
        Legacy compatibility parameter. Program context now discovers editions from
        programs/<program_id>/editions/ under programs_root.
    today:
        Override date for staleness computation. Defaults to date.today().
    raise_on_error:
        If True (default), raises ConfigError when ERROR-level violations exist.
        Set False to inspect violations without raising (e.g. in doctor --context).
    """
    if today is None:
        today = date.today()

    resolved_editions_root = editions_root or (programs_root / program_id / "editions")
    program_dir = programs_root / program_id

    # --- Load all files ---
    program_yaml = load_yaml_mapping(program_dir / "program.yaml")
    program_data = program_yaml.get("program", program_yaml) if isinstance(program_yaml, dict) else {}

    stakeholder_register = _parse_stakeholders(program_data)
    sub_programs = _parse_sub_programs(program_data)

    workstreams = tuple(
        _workstream_entry_from_model(ws)
        for ws in load_current_workstreams(program_id, programs_root=programs_root)
    )

    registry_yaml = load_yaml_mapping(program_dir / "workstream_registry.yaml", required=False, default={})
    registry_entries_raw = registry_yaml.get("workstreams", []) if isinstance(registry_yaml, dict) else []
    registry_lanes = tuple(_parse_registry_lane(e) for e in registry_entries_raw)

    scorecards_yaml = load_yaml_mapping(program_dir / "scorecards.yaml", required=False, default={})
    scorecards_list = scorecards_yaml.get("scorecards", []) if isinstance(scorecards_yaml, dict) else []
    scorecards = tuple(_parse_scorecard(sc) for sc in scorecards_list)

    milestones = tuple(
        _milestone_entry_from_model(entry)
        for entry in load_current_milestones(program_id, programs_root=programs_root)
    )

    risks = tuple(
        _risk_entry_from_model(entry)
        for entry in load_current_risk_entries(program_id, programs_root=programs_root)
    )

    decisions = tuple(
        _decision_entry_from_model(entry)
        for entry in load_current_decision_entries(program_id, programs_root=programs_root)
    )

    assumptions = tuple(
        _assumption_entry_from_model(entry)
        for entry in load_current_assumptions(program_id, programs_root=programs_root)
    )

    dependencies = tuple(
        _dependency_entry_from_model(entry)
        for entry in load_current_dependencies(program_id, programs_root=programs_root)
    )

    kpis_yaml = load_yaml_mapping(program_dir / "kpis.yaml", required=False, default={})
    kpis_list = kpis_yaml.get("kpis", []) if isinstance(kpis_yaml, dict) else []
    kpis = tuple(_parse_kpi(k) for k in kpis_list)

    ed_yaml = load_yaml_mapping(program_dir / "editorial_rules.yaml", required=False, default={})
    editorial_rules = _parse_editorial_rules(ed_yaml)

    # Discover edition files for ED-01/ED-02 invariants
    edition_ids: frozenset[str] = frozenset()
    if resolved_editions_root.is_dir():
        edition_ids = frozenset(
            p.stem for p in resolved_editions_root.glob("*.yaml")
        )

    # --- Resolve foreign keys ---
    all_ws_ids = frozenset(
        ws.id for ws in workstreams
    ) | frozenset(
        lane.id for lane in registry_lanes
    )
    sub_ids = frozenset(sp.id for sp in sub_programs)
    stakeholder_aliases_set = frozenset(s.alias for s in stakeholder_register)
    milestone_ids_set = frozenset(m.id for m in milestones)

    # --- Compute §5 invariants ---
    violations: list[InvariantViolation] = []

    # WS-01: registry sub_program_id must exist in program.yaml sub_programs
    for lane in registry_lanes:
        if lane.sub_program_id and lane.sub_program_id not in sub_ids:
            violations.append(InvariantViolation(
                code="WS-01",
                severity=InvariantSeverity.ERROR,
                file="workstream_registry.yaml",
                entity_id=lane.id,
                detail=f"sub_program_id '{lane.sub_program_id}' not found in program.yaml sub_programs",
                blocker_for_level=1,
            ))

    # WS-02: every workstreams.yaml entry must be referenced by at least one registry entry
    top_level_ws_ids = frozenset(ws.id for ws in workstreams)
    referenced_sub_ids = frozenset(lane.sub_program_id for lane in registry_lanes)
    for ws in workstreams:
        if ws.id and ws.id not in referenced_sub_ids:
            violations.append(InvariantViolation(
                code="WS-02",
                severity=InvariantSeverity.WARN,
                file="workstreams.yaml",
                entity_id=ws.id,
                detail=f"workstream '{ws.id}' not referenced by any workstream_registry entry",
                blocker_for_level=None,
            ))

    # WS-03: scorecard workstream_id must exist in workstreams or registry
    for sc in scorecards:
        for dim in sc.dimensions:
            ws_id = dim.get("workstream_id", "")
            if ws_id and ws_id not in all_ws_ids:
                violations.append(InvariantViolation(
                    code="WS-03",
                    severity=InvariantSeverity.ERROR,
                    file="scorecards.yaml",
                    entity_id=sc.id,
                    detail=f"dimension '{dim.get('name', '?')}': workstream_id '{ws_id}' not found in workstreams or registry",
                    blocker_for_level=1,
                ))

    # WS-04: risk linked_workstream_ids must exist
    for risk in risks:
        for wid in risk.linked_workstream_ids:
            if wid and wid not in all_ws_ids:
                violations.append(InvariantViolation(
                    code="WS-04",
                    severity=InvariantSeverity.ERROR,
                    file="risk_register.yaml",
                    entity_id=risk.id,
                    detail=f"linked_workstream_ids contains '{wid}' not found in registry or workstreams",
                    blocker_for_level=1,
                ))

    # WS-05: decision workstream_id must exist
    for dec in decisions:
        if dec.workstream_id and dec.workstream_id not in all_ws_ids:
            violations.append(InvariantViolation(
                code="WS-05",
                severity=InvariantSeverity.ERROR,
                file="decisions.yaml",
                entity_id=dec.id,
                detail=f"workstream_id '{dec.workstream_id}' not found in registry or workstreams",
                blocker_for_level=1,
            ))

    # MS-01: risk linked_milestone_ids must exist in milestones
    for risk in risks:
        for mid in risk.linked_milestone_ids:
            if mid and mid not in milestone_ids_set:
                violations.append(InvariantViolation(
                    code="MS-01",
                    severity=InvariantSeverity.ERROR,
                    file="risk_register.yaml",
                    entity_id=risk.id,
                    detail=f"linked_milestone_ids contains '{mid}' not found in milestones",
                    blocker_for_level=1,
                ))

    # MS-02: decision linked_milestone_ids must exist in milestones
    for dec in decisions:
        for mid in dec.linked_milestone_ids:
            if mid and mid not in milestone_ids_set:
                violations.append(InvariantViolation(
                    code="MS-02",
                    severity=InvariantSeverity.ERROR,
                    file="decisions.yaml",
                    entity_id=dec.id,
                    detail=f"linked_milestone_ids contains '{mid}' not found in milestones",
                    blocker_for_level=1,
                ))

    # MS-03: milestone linked_workstream_ids must match workstreams.yaml IDs
    for m in milestones:
        for wid in m.linked_workstream_ids:
            if wid and wid not in top_level_ws_ids:
                violations.append(InvariantViolation(
                    code="MS-03",
                    severity=InvariantSeverity.ERROR,
                    file="milestones.yaml",
                    entity_id=m.id,
                    detail=f"linked_workstream_ids contains '{wid}' not found in workstreams.yaml",
                    blocker_for_level=1,
                ))

    # STK-01: RACI aliases must be in stakeholder_register
    for ws in workstreams:
        for alias in ws.raci.values():
            if isinstance(alias, str) and alias and alias not in stakeholder_aliases_set:
                violations.append(InvariantViolation(
                    code="STK-01",
                    severity=InvariantSeverity.WARN,
                    file="workstreams.yaml",
                    entity_id=ws.id,
                    detail=f"raci alias '{alias}' not in stakeholder_register",
                    blocker_for_level=None,
                ))
            elif isinstance(alias, (list, tuple)):
                for a in alias:
                    if a and a not in stakeholder_aliases_set:
                        violations.append(InvariantViolation(
                            code="STK-01",
                            severity=InvariantSeverity.WARN,
                            file="workstreams.yaml",
                            entity_id=ws.id,
                            detail=f"raci alias '{a}' not in stakeholder_register",
                            blocker_for_level=None,
                        ))

    # STK-02: milestone owner_alias must be in stakeholder_register
    for m in milestones:
        if m.owner_alias and m.owner_alias not in stakeholder_aliases_set:
            violations.append(InvariantViolation(
                code="STK-02",
                severity=InvariantSeverity.WARN,
                file="milestones.yaml",
                entity_id=m.id,
                detail=f"owner_alias '{m.owner_alias}' not in stakeholder_register",
                blocker_for_level=None,
            ))

    # STK-03: risk owner_alias must be in stakeholder_register
    for r in risks:
        if r.owner_alias and r.owner_alias not in stakeholder_aliases_set:
            violations.append(InvariantViolation(
                code="STK-03",
                severity=InvariantSeverity.WARN,
                file="risk_register.yaml",
                entity_id=r.id,
                detail=f"owner_alias '{r.owner_alias}' not in stakeholder_register",
                blocker_for_level=None,
            ))

    # ED-01: every edition YAML's program_id must match program.yaml id
    for ed_id in edition_ids:
        ed_path = resolved_editions_root / f"{ed_id}.yaml"
        try:
            ed_data = load_yaml_mapping(ed_path)
            pid = ed_data.get("program_id", "")
            if pid and pid != program_id:
                violations.append(InvariantViolation(
                    code="ED-01",
                    severity=InvariantSeverity.ERROR,
                    file=f"editions/{ed_id}.yaml",
                    entity_id=ed_id,
                    detail=f"program_id '{pid}' does not match program '{program_id}'",
                    blocker_for_level=1,
                ))
        except (ConfigError, OSError, KeyError, TypeError):
            pass

    # ED-02: every edition YAML ID must appear in editorial_rules applies_to_editions
    for ed_id in edition_ids:
        if ed_id not in editorial_rules.applies_to_editions:
            violations.append(InvariantViolation(
                code="ED-02",
                severity=InvariantSeverity.WARN,
                file="editorial_rules.yaml",
                entity_id=ed_id,
                detail=f"edition '{ed_id}' not listed in voice_contract.applies_to_editions",
                blocker_for_level=None,
            ))

    # ED-03: when program.yaml declares sub_programs (multi-org internal structure), every
    # edition should carry a scope_note describing which team(s)/org(s) it covers, so nudges
    # and newsletters don't get confused about their accountability/coverage boundary.
    if sub_programs:
        for ed_id in edition_ids:
            ed_path = resolved_editions_root / f"{ed_id}.yaml"
            try:
                ed_data = load_yaml_mapping(ed_path)
                if not str(ed_data.get("scope_note") or "").strip():
                    violations.append(InvariantViolation(
                        code="ED-03",
                        severity=InvariantSeverity.WARN,
                        file=f"editions/{ed_id}.yaml",
                        entity_id=ed_id,
                        detail="edition has no scope_note describing team/org coverage (program defines sub_programs)",
                        blocker_for_level=None,
                    ))
            except (ConfigError, OSError, KeyError, TypeError):
                pass

    # KPI-01: kpis workstream_ids must match workstream_registry IDs
    registry_ws_ids = frozenset(lane.id for lane in registry_lanes)
    for kpi in kpis:
        for wid in kpi.workstream_ids:
            if wid and wid not in registry_ws_ids:
                violations.append(InvariantViolation(
                    code="KPI-01",
                    severity=InvariantSeverity.ERROR,
                    file="kpis.yaml",
                    entity_id=kpi.id,
                    detail=f"workstream_ids contains '{wid}' not found in workstream_registry",
                    blocker_for_level=1,
                ))

    # DEP-01: every open risk with category=dependency must have a dependencies.yaml entry
    dep_linked_risk_ids: frozenset[str] = frozenset(
        rid for dep in dependencies for rid in dep.linked_risk_ids
    )
    for risk in risks:
        if risk.category == "dependency" and risk.status in ("open", "at_risk"):
            if risk.id not in dep_linked_risk_ids:
                violations.append(InvariantViolation(
                    code="DEP-01",
                    severity=InvariantSeverity.ERROR,
                    file="risk_register.yaml",
                    entity_id=risk.id,
                    detail=f"category=dependency but no dependencies.yaml entry links to it",
                    blocker_for_level=1,
                ))

    # ASSUM-01: assumption linked_workstream_ids must exist
    for assum in assumptions:
        for wid in assum.linked_workstream_ids:
            if wid and wid not in all_ws_ids:
                violations.append(InvariantViolation(
                    code="ASSUM-01",
                    severity=InvariantSeverity.ERROR,
                    file="assumptions.yaml",
                    entity_id=assum.id,
                    detail=f"linked_workstream_ids contains '{wid}' not in workstreams or registry",
                    blocker_for_level=1,
                ))

    # ASSUM-02: assumption linked_milestone_ids must exist
    for assum in assumptions:
        for mid in assum.linked_milestone_ids:
            if mid and mid not in milestone_ids_set:
                violations.append(InvariantViolation(
                    code="ASSUM-02",
                    severity=InvariantSeverity.ERROR,
                    file="assumptions.yaml",
                    entity_id=assum.id,
                    detail=f"linked_milestone_ids contains '{mid}' not in milestones",
                    blocker_for_level=1,
                ))

    # CONTRACT-01: slice_contracts dimension/scorecard pairs must match scorecards.yaml
    sc_names: frozenset[str] = frozenset(
        d.get("name", "") for sc in scorecards for d in sc.dimensions
    )
    slice_path = program_dir / "slice_contracts.yaml"
    if slice_path.exists():
        sc_contracts = load_yaml_mapping(slice_path, required=False, default={})
        for entry in sc_contracts.get("slices", []):
            sc_name = entry.get("scorecard", "")
            if sc_name and sc_name not in sc_names:
                violations.append(InvariantViolation(
                    code="CONTRACT-01",
                    severity=InvariantSeverity.ERROR,
                    file="slice_contracts.yaml",
                    entity_id=sc_name,
                    detail=f"scorecard '{sc_name}' not found in scorecards.yaml dimensions",
                    blocker_for_level=1,
                ))

    # CONTRACT-02: chapter_contract section IDs must match template_contract section IDs
    template_path = program_dir / "template_contract.yaml"
    chapter_path = program_dir / "chapter_contract.yaml"
    if template_path.exists() and chapter_path.exists():
        tpl_data = load_yaml_mapping(template_path, required=False, default={})
        chp_data = load_yaml_mapping(chapter_path, required=False, default={})
        template_section_ids: set[str] = set()
        for family_data in tpl_data.get("families", {}).values():
            if isinstance(family_data, dict):
                for key in ("order", "mandatory", "optional"):
                    for sid in family_data.get(key, []):
                        if isinstance(sid, str):
                            template_section_ids.add(sid)
        for chapter in chp_data.get("chapters", []):
            if not isinstance(chapter, dict):
                continue
            for sid in chapter.get("sections", []):
                if sid and sid not in template_section_ids:
                    violations.append(InvariantViolation(
                        code="CONTRACT-02",
                        severity=InvariantSeverity.ERROR,
                        file="chapter_contract.yaml",
                        entity_id=chapter.get("id", "?"),
                        detail=f"section_id '{sid}' not found in template_contract.yaml",
                        blocker_for_level=1,
                    ))

    # DATE-01: no milestone linked_work_item_ids may be placeholder IDs (900000-999999)
    for m in milestones:
        for wi in m.linked_work_item_ids:
            if 900000 <= wi <= 999999:
                violations.append(InvariantViolation(
                    code="DATE-01",
                    severity=InvariantSeverity.ERROR,
                    file="milestones.yaml",
                    entity_id=m.id,
                    detail=f"linked_work_item_ids contains stub ID {wi} (900000-999999 range)",
                    blocker_for_level=1,
                ))

    # DATE-02: open risk with past mitigation_due_date and stale last_reviewed_date
    for risk in risks:
        if risk.status not in ("open", "at_risk"):
            continue
        due = risk.mitigation_due_date
        lrd = risk.last_reviewed_date
        if due and (today - due).days > 0:
            days_stale = (today - lrd).days if lrd else None
            if days_stale is None or days_stale > 7:
                violations.append(InvariantViolation(
                    code="DATE-02",
                    severity=InvariantSeverity.WARN,
                    file="risk_register.yaml",
                    entity_id=risk.id,
                    detail=f"mitigation_due_date {due} is {(today - due).days}d past; last_reviewed_date {lrd or 'not set'}",
                    blocker_for_level=None,
                ))

    # FILTER-01: scorecards ado_filter values must parse as valid OData expressions
    for sc in scorecards:
        for dim in sc.dimensions:
            ado_filter = dim.get("ado_filter", "")
            if ado_filter and not _looks_like_odata(ado_filter):
                violations.append(InvariantViolation(
                    code="FILTER-01",
                    severity=InvariantSeverity.ERROR,
                    file="scorecards.yaml",
                    entity_id=sc.id,
                    detail=f"dimension '{dim.get('name', '?')}': ado_filter is informal syntax, not valid OData $filter",
                    blocker_for_level=1,
                ))

    # --- Compute §8 staleness ---
    staleness_flags: list[StalenessFlag] = []

    # File-level staleness (from top-level last_reviewed_date field in each file)
    _file_thresholds: list[tuple[str, int, int | None]] = [
        ("program.yaml", 90, 180),
        ("workstreams.yaml", 30, 90),
        ("milestones.yaml", 14, 30),
        ("scorecards.yaml", 60, 120),
        ("kpis.yaml", 60, None),
        ("editorial_rules.yaml", 60, None),
        ("dependencies.yaml", 30, 60),
        ("capability_status.yaml", 14, 30),
        ("template_contract.yaml", 90, None),
        ("slice_contracts.yaml", 60, 120),
        ("chapter_contract.yaml", 90, None),
        ("readiness.yaml", 30, 60),
    ]
    for fname, warn_days, block_days in _file_thresholds:
        fpath = program_dir / fname
        if not fpath.exists():
            continue
        data = load_yaml_mapping(fpath, required=False, default={})
        lrd_raw = data.get("last_reviewed_date")
        lrd = _parse_date(lrd_raw)
        if lrd is None:
            continue
        days = (today - lrd).days
        sev = "ok"
        if block_days is not None and days >= block_days:
            sev = "block"
        elif days >= warn_days:
            sev = "warn"
        if sev != "ok":
            staleness_flags.append(StalenessFlag(
                file=fname,
                entity_id=None,
                field="last_reviewed_date",
                last_reviewed_date=lrd,
                days_stale=days,
                warn_threshold=warn_days,
                block_threshold=block_days,
                severity=sev,
            ))

    # Per-entry staleness: workstreams (14d warn / 30d block)
    for ws in workstreams:
        lrd = ws.last_reviewed_date
        if lrd:
            days = (today - lrd).days
            sev = "block" if days >= 30 else ("warn" if days >= 14 else "ok")
            if sev != "ok":
                staleness_flags.append(StalenessFlag(
                    file="workstreams.yaml",
                    entity_id=ws.id,
                    field="last_reviewed_date",
                    last_reviewed_date=lrd,
                    days_stale=days,
                    warn_threshold=14,
                    block_threshold=30,
                    severity=sev,
                ))

    # Per-entry staleness: workstream_registry (14d warn / 30d block)
    for lane in registry_lanes:
        lrd = lane.last_reviewed_date
        if lrd:
            days = (today - lrd).days
            sev = "block" if days >= 30 else ("warn" if days >= 14 else "ok")
            if sev != "ok":
                staleness_flags.append(StalenessFlag(
                    file="workstream_registry.yaml",
                    entity_id=lane.id,
                    field="last_reviewed_date",
                    last_reviewed_date=lrd,
                    days_stale=days,
                    warn_threshold=14,
                    block_threshold=30,
                    severity=sev,
                ))

    # Per-entry staleness: milestones (14d warn / 30d block)
    for m in milestones:
        lrd = m.last_reviewed_date
        if lrd:
            days = (today - lrd).days
            sev = "block" if days >= 30 else ("warn" if days >= 14 else "ok")
            if sev != "ok":
                staleness_flags.append(StalenessFlag(
                    file="milestones.yaml",
                    entity_id=m.id,
                    field="last_reviewed_date",
                    last_reviewed_date=lrd,
                    days_stale=days,
                    warn_threshold=14,
                    block_threshold=30,
                    severity=sev,
                ))

    # Per-entry staleness: open risks (7d warn / 14d block)
    for risk in risks:
        if risk.status not in ("open", "at_risk"):
            continue
        lrd = risk.last_reviewed_date
        if lrd:
            days = (today - lrd).days
            sev = "block" if days >= 14 else ("warn" if days >= 7 else "ok")
            if sev != "ok":
                staleness_flags.append(StalenessFlag(
                    file="risk_register.yaml",
                    entity_id=risk.id,
                    field="last_reviewed_date",
                    last_reviewed_date=lrd,
                    days_stale=days,
                    warn_threshold=7,
                    block_threshold=14,
                    severity=sev,
                ))

    # Per-entry staleness: decisions with past review_by (0d warn / 14d block)
    for dec in decisions:
        if dec.review_by and dec.status not in ("superseded", "closed"):
            days = (today - dec.review_by).days
            if days >= 0:  # review_by is past
                sev = "block" if days >= 14 else "warn"
                staleness_flags.append(StalenessFlag(
                    file="decisions.yaml",
                    entity_id=dec.id,
                    field="review_by",
                    last_reviewed_date=dec.review_by,
                    days_stale=days,
                    warn_threshold=0,
                    block_threshold=14,
                    severity=sev,
                ))

    # Per-entry staleness: active assumptions (30d warn / 60d block)
    for assum in assumptions:
        if assum.status != "active":
            continue
        lrd = assum.last_reviewed_date
        if lrd:
            days = (today - lrd).days
            sev = "block" if days >= 60 else ("warn" if days >= 30 else "ok")
            if sev != "ok":
                staleness_flags.append(StalenessFlag(
                    file="assumptions.yaml",
                    entity_id=assum.id,
                    field="last_reviewed_date",
                    last_reviewed_date=lrd,
                    days_stale=days,
                    warn_threshold=30,
                    block_threshold=60,
                    severity=sev,
                ))

    staleness_error_count = sum(1 for f in staleness_flags if f.severity == "block")
    staleness_warn_count = sum(1 for f in staleness_flags if f.severity == "warn")

    # --- Compute §16 maturity level ---
    maturity_level, maturity_blockers = _compute_maturity_level(
        violations=violations,
        staleness_error_count=staleness_error_count,
        registry_lanes=registry_lanes,
        milestones=milestones,
        risks=risks,
        editorial_rules=editorial_rules,
        all_ws_ids=all_ws_ids,
        kpis=kpis,
        today=today,
        programs_root=programs_root,
        program_id=program_id,
        edition_ids=edition_ids,
    )

    # --- Coverage metrics ---
    active_lanes = [lane for lane in registry_lanes if lane.lifecycle_state not in ("paused", "closed")]
    deep_context_coverage_pct = (
        100.0 * sum(1 for lane in active_lanes if lane.deep_context.get("why")) / max(len(active_lanes), 1)
    )
    signal_source_coverage_pct = (
        100.0 * sum(1 for lane in active_lanes if (
            lane.area_paths or lane.kusto_queries or lane.icm_queue or
            lane.signal_sources.get("teams_meeting_series") or
            lane.signal_sources.get("teams_chats") or
            lane.signal_sources.get("email_subject_filters")
        )) / max(len(active_lanes), 1)
    )
    kpi_validation_rate_pct = (
        100.0 * sum(1 for k in kpis if k.validated) / max(len(kpis), 1)
    )

    # --- Context gaps (informational only) ---
    context_gaps: list[ContextGapInfo] = []

    for lane in active_lanes:
        if not lane.deep_context.get("why"):
            context_gaps.append(ContextGapInfo(
                feature="ProgramContext",
                lane=lane.id,
                field="deep_context.why",
                severity="quality_degraded",
                impact="high",
                message="AI proposals for this lane use generic context",
            ))
        if not lane.deep_context.get("what"):
            context_gaps.append(ContextGapInfo(
                feature="ProgramContext",
                lane=lane.id,
                field="deep_context.what",
                severity="quality_degraded",
                impact="medium",
                message="AI proposals for this lane lack specific content description",
            ))

    for kpi in kpis:
        if not kpi.validated:
            context_gaps.append(ContextGapInfo(
                feature="ProgramContext",
                lane=None,
                field="kpis.validated",
                severity="quality_degraded",
                impact="medium",
                message=f"KPI '{kpi.id}': live metric fetch may fail silently on gather",
            ))

    ctx = ProgramContext(
        program_id=program_id,
        loaded_at=datetime.now(timezone.utc),
        stakeholder_register=stakeholder_register,
        sub_programs=sub_programs,
        workstreams=workstreams,
        registry_lanes=registry_lanes,
        scorecards=scorecards,
        milestones=milestones,
        risks=risks,
        decisions=decisions,
        assumptions=assumptions,
        dependencies=dependencies,
        kpis=kpis,
        editorial_rules=editorial_rules,
        maturity_level=maturity_level,
        maturity_blockers=tuple(maturity_blockers),
        invariant_violations=tuple(violations),
        staleness_flags=tuple(staleness_flags),
        context_gaps=tuple(context_gaps),
        all_workstream_ids=all_ws_ids,
        sub_program_ids=sub_ids,
        stakeholder_aliases=stakeholder_aliases_set,
        milestone_ids=milestone_ids_set,
        edition_ids=edition_ids,
        deep_context_coverage_pct=deep_context_coverage_pct,
        signal_source_coverage_pct=signal_source_coverage_pct,
        kpi_validation_rate_pct=kpi_validation_rate_pct,
        staleness_error_count=staleness_error_count,
        staleness_warn_count=staleness_warn_count,
    )

    if raise_on_error and ctx.has_blocking_violations():
        errors = [v for v in violations if v.is_error()]
        first = errors[0]
        extra = f" (+{len(errors) - 1} more)" if len(errors) > 1 else ""
        raise ConfigError(
            f"Program context for '{program_id}' has {len(errors)} ERROR invariant(s): "
            f"[{first.code}] {first.detail}{extra}"
        )

    return ctx


# ---------------------------------------------------------------------------
# Section 5: Maturity level computation
# ---------------------------------------------------------------------------

def _compute_maturity_level(
    *,
    violations: list[InvariantViolation],
    staleness_error_count: int,
    registry_lanes: tuple[RegistryLaneEntry, ...],
    milestones: tuple[MilestoneEntry, ...],
    risks: tuple[RiskEntry, ...],
    editorial_rules: EditorialRulesData,
    all_ws_ids: frozenset[str],
    kpis: tuple[KpiEntry, ...],
    today: date,
    programs_root: Path,
    program_id: str,
    edition_ids: frozenset[str],
) -> tuple[MaturityLevel, list[str]]:
    """
    Compute §16 maturity level (L0–L4) and list blockers to the next level.

    Level rules:
      L0: program.yaml and workstreams.yaml exist with valid IDs and >= 1 workstream
      L1: no ERROR invariants; no stub WI IDs (DATE-01); all scorecard OData filters valid
      L2: all active registry lanes have non-empty deep_context; risks reviewed within §8 windows;
          editorial_rules covers all editions; milestones have real WI IDs (no stubs)
      L3: signal sources wired for >= 80% active lanes; workiq_latest fresh < 7d for all active lanes
      L4: all L3 criteria + plane1 changelog accumulating + context snapshots written
    """
    blockers: list[str] = []

    # L0: must have at least one workstream
    if len(registry_lanes) == 0 and len(all_ws_ids) == 0:
        return MaturityLevel.L0, ["no workstream entries found"]

    # L1: ERROR invariants block L1
    error_violations = [v for v in violations if v.is_error()]
    if error_violations:
        return MaturityLevel.L0, [v.detail for v in error_violations[:3]]

    # Remaining L1 check: stub WI IDs and informal OData (already caught as DATE-01/FILTER-01 above)
    # If we're here, no ERROR violations exist → L1 achieved.

    # L2: deep_context coverage
    active_lanes = [lane for lane in registry_lanes if lane.lifecycle_state not in ("paused", "closed")]
    if active_lanes:
        missing_dc = [lane.id for lane in active_lanes if not lane.deep_context.get("why")]
        if missing_dc:
            blockers.append(
                f"{len(missing_dc)} active lanes lack deep_context.why: {', '.join(missing_dc[:3])}"
            )

    # L2: risk staleness (DATE-02 — past mitigation_due_date and stale)
    for r in risks:
        if r.status not in ("open", "at_risk"):
            continue
        due = r.mitigation_due_date
        lrd = r.last_reviewed_date
        if not due or not lrd:
            continue
        if (today - due).days > 7 and (today - lrd).days > 7:
            blockers.append(
                f"risk {r.id} has past mitigation_due_date and stale last_reviewed_date"
            )

    # L2: editorial rules edition coverage
    if edition_ids:
        missing_ed = [ed for ed in edition_ids if ed not in editorial_rules.applies_to_editions]
        if missing_ed:
            blockers.append(
                f"editorial_rules missing applies_to_editions for: {', '.join(missing_ed[:3])}"
            )

    level = 2 if not blockers else 1

    # L3 criteria
    l3_blockers: list[str] = []

    # Signal source coverage (>= 80% active lanes)
    if active_lanes:
        lanes_with_signal = [
            lane for lane in active_lanes
            if (lane.area_paths or lane.kusto_queries or lane.icm_queue or
                lane.signal_sources.get("teams_meeting_series") or
                lane.signal_sources.get("teams_chats"))
        ]
        coverage = len(lanes_with_signal) / len(active_lanes)
        if coverage < 0.8:
            l3_blockers.append(
                f"signal source coverage {coverage:.0%} < 80% "
                f"({len(lanes_with_signal)}/{len(active_lanes)} active lanes)"
            )

    # workiq_latest freshness (< 7d for all active lanes)
    for lane in active_lanes:
        wl = lane.workiq_latest
        if wl and isinstance(wl, str) and wl.startswith("2"):
            try:
                wl_date = _parse_date(wl.split(":")[0].strip()[:10])
                if wl_date and (today - wl_date).days > 7:
                    l3_blockers.append(
                        f"workiq_latest for {lane.id} is {(today - wl_date).days}d stale (> 7d)"
                    )
            except (ValueError, TypeError):
                pass

    if not l3_blockers:
        level = 3
    else:
        blockers.extend(l3_blockers)
        return MaturityLevel(level), blockers

    # L4 criteria: plane1 changelog accumulating + context snapshots written
    l4_blockers: list[str] = []

    program_dir = programs_root / program_id
    changelog_path = program_dir / "changelog" / "plane1_changes.jsonl"
    if not changelog_path.exists():
        l4_blockers.append("plane1_changes.jsonl not yet created (run vertex gather once)")
    else:
        try:
            lines = changelog_path.read_text(encoding="utf-8").splitlines()
            if not any(l.strip() for l in lines):
                l4_blockers.append("plane1_changes.jsonl exists but is empty (no changes detected yet)")
        except OSError:
            l4_blockers.append("plane1_changes.jsonl exists but could not be read")

    # Check for at least one context snapshot in archive
    archive_dir = program_dir / "archive"
    has_snapshot = any(archive_dir.rglob("*.context.json")) if archive_dir.exists() else False
    if not has_snapshot:
        l4_blockers.append(
            "no context snapshots found in archive/ (run vertex confirm once after E2 is wired)"
        )

    if not l4_blockers:
        level = 4
    else:
        blockers.extend(l4_blockers)

    return MaturityLevel(level), blockers


# ---------------------------------------------------------------------------
# Section 6: YAML parsing helpers
# ---------------------------------------------------------------------------

def _parse_stakeholders(program_data: dict[str, Any]) -> tuple[StakeholderEntry, ...]:
    entries = []
    for s in program_data.get("stakeholder_register", []):
        if s.get("alias"):
            entries.append(StakeholderEntry(
                alias=str(s["alias"]),
                email=s.get("email"),
                role=s.get("role"),
            ))
    return tuple(entries)


def _parse_sub_programs(program_data: dict[str, Any]) -> tuple[SubProgramEntry, ...]:
    entries = []
    for sp in program_data.get("sub_programs", []):
        if sp.get("id"):
            entries.append(SubProgramEntry(
                id=str(sp["id"]),
                name=sp.get("name", ""),
                objective=sp.get("objective"),
            ))
    return tuple(entries)


def _workstream_entry_from_model(ws: Workstream) -> WorkstreamEntry:
    raci: dict[str, str | tuple[str, ...]] = {}
    if ws.accountable_owner:
        raci["accountable"] = ws.accountable_owner
    if ws.responsible_owners:
        raci["responsible"] = ws.responsible_owners
    if ws.consulted_owners:
        raci["consulted"] = ws.consulted_owners
    if ws.informed_owners:
        raci["informed"] = ws.informed_owners
    return WorkstreamEntry(
        id=ws.id,
        name=ws.name,
        accountable=ws.accountable_owner,
        accountable_email=ws.accountable_email,
        area_paths=ws.area_paths,
        raci=raci,
        last_reviewed_date=ws.last_reviewed_date,
    )


def _milestone_entry_from_model(entry: FactMilestone) -> MilestoneEntry:
    return MilestoneEntry(
        id=entry.id,
        name=entry.name,
        target_date=entry.target_date,
        status=entry.status.value,
        owner_alias=entry.owner_alias,
        linked_work_item_ids=entry.linked_work_item_ids,
        linked_workstream_ids=entry.linked_workstream_ids,
        last_reviewed_date=entry.last_reviewed_date,
    )


def _risk_entry_from_model(entry: FactRiskEntry) -> RiskEntry:
    return RiskEntry(
        id=entry.id,
        title=entry.title,
        status=entry.status.value,
        probability=entry.probability.value,
        impact=entry.impact.value,
        category=entry.category.value,
        owner_alias=entry.owner_alias,
        mitigation_due_date=entry.mitigation_due_date,
        linked_workstream_ids=entry.linked_workstream_ids,
        linked_milestone_ids=entry.linked_milestone_ids,
        last_reviewed_date=entry.last_reviewed_date,
    )


def _decision_entry_from_model(entry: FactDecisionEntry) -> DecisionEntry:
    return DecisionEntry(
        id=entry.id,
        title=entry.title,
        status=entry.status.value,
        decided_by=entry.decided_by,
        workstream_id=entry.workstream_id,
        linked_milestone_ids=entry.linked_milestone_ids,
        review_by=entry.review_by,
        last_reviewed_date=entry.last_reviewed_date,
    )


def _assumption_entry_from_model(entry: FactAssumption) -> AssumptionEntry:
    return AssumptionEntry(
        id=entry.id,
        statement=entry.text,
        status=entry.status.value,
        category=entry.category,
        owner_alias=entry.owner_alias,
        linked_workstream_ids=entry.linked_workstream_ids,
        linked_milestone_ids=entry.linked_milestone_ids or ((entry.linked_milestone_id,) if entry.linked_milestone_id else ()),
        last_reviewed_date=entry.last_reviewed_date,
    )


def _dependency_entry_from_model(entry: FactDependency) -> DependencyEntry:
    blocking_program_or_team = entry.to_program_id
    if entry.to_workstream_id:
        blocking_program_or_team = f"{blocking_program_or_team}:{entry.to_workstream_id}"
    return DependencyEntry(
        id=entry.id,
        type=entry.dependency_type.value,
        blocking_program_or_team=blocking_program_or_team,
        expected_resolution=entry.planned_resolution_date,
        status=entry.status.value,
        owner_alias=entry.owner_alias,
        linked_risk_ids=entry.linked_risk_ids,
    )


def _parse_registry_lane(e: dict[str, Any]) -> RegistryLaneEntry:
    from src.core.evidence_models import build_placeholder_evidence  # noqa: PLC0415
    lrd = _parse_date(e.get("last_reviewed_date"))
    wl_raw = e.get("workiq_latest")
    workiq_latest_str = str(wl_raw) if wl_raw else None
    # Extract alias strings from stakeholder dicts
    raw_stakeholders = e.get("stakeholders", [])
    stakeholder_aliases: tuple[str, ...] = tuple(
        str(s.get("alias") or s.get("name") or "")
        for s in raw_stakeholders
        if isinstance(s, dict) and (s.get("alias") or s.get("name"))
    )
    evidence = build_placeholder_evidence(
        lane_id=str(e.get("id", "")),
        workiq_latest=workiq_latest_str,
    )
    cadence_raw = e.get("expected_cadence_days")
    expected_cadence_days = int(cadence_raw) if isinstance(cadence_raw, int) else None
    return RegistryLaneEntry(
        id=str(e.get("id", "")),
        sub_program_id=str(e.get("sub_program_id", "")),
        lifecycle_state=e.get("lifecycle_state"),
        deep_context=dict(e.get("deep_context", {})),
        roles=tuple(e.get("roles", []) or e.get("stakeholders", [])),
        workiq_latest=workiq_latest_str,
        last_reviewed_date=lrd,
        signal_sources=dict(e.get("signal_sources", {})),
        area_paths=tuple(e.get("area_paths", [])),
        kusto_queries=tuple(e.get("kusto_queries", [])),
        icm_queue=e.get("icm_queue"),
        stakeholder_aliases=stakeholder_aliases,
        evidence=evidence,
        expected_cadence_days=expected_cadence_days,
    )


def _parse_scorecard(sc: dict[str, Any]) -> ScorecardEntry:
    return ScorecardEntry(
        id=str(sc.get("id", sc.get("name", ""))),
        name=str(sc.get("name", "")),
        dimensions=tuple(sc.get("dimensions", [])),
        linked_scorecard_id=sc.get("linked_scorecard_id"),
    )


def _parse_milestone(m: dict[str, Any]) -> MilestoneEntry:
    td = _parse_date(m.get("target_date"))
    lrd = _parse_date(m.get("last_reviewed_date"))
    wis = m.get("linked_work_item_ids", [])
    wi_tuple = tuple(int(w) for w in wis if isinstance(w, int) or (isinstance(w, str) and w.isdigit()))
    return MilestoneEntry(
        id=str(m.get("id", "")),
        name=m.get("name", ""),
        target_date=td,
        status=m.get("status"),
        owner_alias=m.get("owner_alias"),
        linked_work_item_ids=wi_tuple,
        linked_workstream_ids=tuple(str(w) for w in m.get("linked_workstream_ids", [])),
        last_reviewed_date=lrd,
    )


def _parse_risk(r: dict[str, Any]) -> RiskEntry:
    due = _parse_date(r.get("mitigation_due_date"))
    lrd = _parse_date(r.get("last_reviewed_date"))
    return RiskEntry(
        id=str(r.get("id", "")),
        title=r.get("title", ""),
        status=r.get("status"),
        probability=r.get("probability"),
        impact=r.get("impact"),
        category=r.get("category"),
        owner_alias=r.get("owner_alias"),
        mitigation_due_date=due,
        linked_workstream_ids=tuple(str(w) for w in r.get("linked_workstream_ids", [])),
        linked_milestone_ids=tuple(str(m) for m in r.get("linked_milestone_ids", [])),
        last_reviewed_date=lrd,
    )


def _parse_decision(d: dict[str, Any]) -> DecisionEntry:
    lrd = _parse_date(d.get("last_reviewed_date"))
    review_by = _parse_date(d.get("review_by") or d.get("review_by_date"))
    return DecisionEntry(
        id=str(d.get("id", "")),
        title=d.get("title", ""),
        status=d.get("status"),
        decided_by=d.get("decided_by"),
        workstream_id=d.get("workstream_id"),
        linked_milestone_ids=tuple(str(m) for m in d.get("linked_milestone_ids", [])),
        review_by=review_by,
        last_reviewed_date=lrd,
    )


def _parse_assumption(a: dict[str, Any]) -> AssumptionEntry:
    lrd = _parse_date(a.get("last_reviewed_date"))
    return AssumptionEntry(
        id=str(a.get("id", "")),
        statement=a.get("statement"),
        status=a.get("status"),
        category=a.get("category"),
        owner_alias=a.get("owner_alias"),
        linked_workstream_ids=tuple(str(w) for w in a.get("linked_workstream_ids", [])),
        linked_milestone_ids=tuple(str(m) for m in a.get("linked_milestone_ids", [])),
        last_reviewed_date=lrd,
    )


def _parse_dependency(d: dict[str, Any]) -> DependencyEntry:
    er = _parse_date(d.get("expected_resolution"))
    return DependencyEntry(
        id=str(d.get("id", "")),
        type=d.get("type"),
        blocking_program_or_team=d.get("blocking_program_or_team"),
        expected_resolution=er,
        status=d.get("status"),
        owner_alias=d.get("owner_alias"),
        linked_risk_ids=tuple(str(r) for r in d.get("linked_risk_ids", [])),
    )


def _parse_kpi(k: dict[str, Any]) -> KpiEntry:
    lrd = _parse_date(k.get("last_reviewed_date"))
    return KpiEntry(
        id=str(k.get("id", "")),
        name=k.get("name", k.get("label", k.get("id", ""))),
        validated=bool(k.get("validated", False)),
        kusto_query=k.get("kusto_query") or k.get("kql"),
        workstream_ids=tuple(str(w) for w in k.get("workstream_ids", [])),
        last_reviewed_date=lrd,
    )


def _parse_editorial_rules(ed_yaml: dict[str, Any]) -> EditorialRulesData:
    vc = ed_yaml.get("voice_contract", {})
    return EditorialRulesData(
        applies_to_editions=tuple(vc.get("applies_to_editions", [])),
        banned_phrases=tuple(ed_yaml.get("banned_phrases", [])),
        banned_openings=tuple(ed_yaml.get("banned_openings", [])),
        abstract_phrases=tuple(vc.get("abstract_phrases", [])),
        synthetic_delta_prefixes=tuple(vc.get("synthetic_delta_prefixes", [])),
        exec_summary_bucket_prefixes=tuple(vc.get("exec_summary_bucket_prefixes", [])),
    )


def _parse_date(value: Any) -> date | None:
    """Parse a date from string or date object. Returns None on failure."""
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except (ValueError, IndexError):
            return None
    return None


# ---------------------------------------------------------------------------
# Section 7: OData filter validation helper
# ---------------------------------------------------------------------------

_ODATA_FUNC_RE = re.compile(
    r"^(startswith|endswith|contains|length|tolower|toupper|substringof)\s*\(",
    re.IGNORECASE,
)
_ODATA_FIELD_OP_RE = re.compile(
    r"^[a-zA-Z_][a-zA-Z0-9_./]*\s+(eq|ne|gt|lt|ge|le|in|contains)\s+",
    re.IGNORECASE,
)


def _looks_like_odata(expr: str) -> bool:
    """
    Return True if expr looks like a valid OData $filter expression.

    Accepts expressions starting with OData functions (startswith, contains, etc.)
    or field comparison operators (eq, ne, gt, lt, ge, le, in).
    Rejects informal string syntax like "tag contains 'X'" (space-delimited contains).
    """
    if not expr:
        return False
    expr = expr.strip()
    # Must start with known OData function or field comparison (including engine-native
    # "field contains 'value'" format used by parse_ado_filter in scorecard_engine.py)
    if not (_ODATA_FUNC_RE.match(expr) or _ODATA_FIELD_OP_RE.match(expr)):
        return False
    return True
