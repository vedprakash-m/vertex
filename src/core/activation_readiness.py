"""Activation readiness evaluators for operator-paced gates."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Iterable, Mapping

import yaml


@dataclass(frozen=True, slots=True)
class ReadinessVerdict:
    passed: bool
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RawDataFamilyObservation:
    claim_event_type: str
    reachable_document_count: int
    acquisition_owner: str = ""
    acquisition_path: str = ""


@dataclass(frozen=True, slots=True)
class RawDataFeasibilityVerdict(ReadinessVerdict):
    best_family: str | None
    best_reachable_document_count: int
    required_reachable_documents: int


@dataclass(frozen=True, slots=True)
class AnnotationStaffingPlan:
    primary_annotator: str
    second_annotator: str
    adjudicator: str
    target_dual_labeled: int
    guideline_uri: str
    due_date: str


@dataclass(frozen=True, slots=True)
class PilotDegradeExceptionPlan:
    adr_id: str
    owner: str
    expires_on: str
    proof_only: bool
    blocks_authority_cycles: bool


@dataclass(frozen=True, slots=True)
class ProgramSchemaSample:
    program_id: str
    has_program_config: bool
    workstream_count: int
    has_entity_registry: bool
    hardcoded_xpf_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CrossSourceReconciliationPlan:
    source_nodes: tuple[str, ...]
    has_materiality_policy: bool
    carries_as_of: bool
    has_disputed_output: bool
    has_operator_adjudication_queue: bool


@dataclass(frozen=True, slots=True)
class ExplainDrilldownPlan:
    has_source_excerpt: bool
    has_counter_source_context: bool
    has_lineage_keys: bool
    has_operator_action: bool
    has_accessibility_review: bool


@dataclass(frozen=True, slots=True)
class BindingReleaseReadiness:
    dirty_worktree: bool
    branch_local_evidence_count: int
    committed_canonical_evidence: bool
    verifier_self_test_passed: bool
    generated_evidence_current: bool
    required_green_checks: tuple[str, ...]
    passing_check_ids: tuple[str, ...]
    failing_check_ids: tuple[str, ...]
    allowed_red_blockers: tuple[str, ...]
    release_owner: str


def evaluate_raw_data_feasibility(
    observations: Iterable[RawDataFamilyObservation],
    *,
    required_reachable_documents: int = 30,
) -> RawDataFeasibilityVerdict:
    rows = tuple(observations)
    best = max(rows, key=lambda row: row.reachable_document_count, default=None)
    reasons: list[str] = []
    if best is None:
        reasons.append("no family observations supplied")
    elif best.reachable_document_count < required_reachable_documents:
        reasons.append(
            f"best_family {best.claim_event_type} reachable_document_count "
            f"{best.reachable_document_count} < {required_reachable_documents}"
        )
    if not any(row.acquisition_owner.strip() and row.acquisition_path.strip() for row in rows):
        reasons.append("raw-data acquisition owner/path missing")
    return RawDataFeasibilityVerdict(
        passed=not reasons,
        reasons=tuple(reasons),
        best_family=best.claim_event_type if best is not None else None,
        best_reachable_document_count=best.reachable_document_count if best is not None else 0,
        required_reachable_documents=required_reachable_documents,
    )


def evaluate_annotation_staffing(plan: AnnotationStaffingPlan) -> ReadinessVerdict:
    reasons: list[str] = []
    if not plan.primary_annotator.strip():
        reasons.append("primary annotator missing")
    if not plan.second_annotator.strip():
        reasons.append("second annotator missing")
    if plan.primary_annotator.strip() and plan.primary_annotator == plan.second_annotator:
        reasons.append("second annotator must differ from primary")
    if not plan.adjudicator.strip():
        reasons.append("adjudicator missing")
    if plan.target_dual_labeled < 20:
        reasons.append(f"target_dual_labeled {plan.target_dual_labeled} < 20")
    if not plan.guideline_uri.strip():
        reasons.append("guideline_uri missing")
    if not plan.due_date.strip():
        reasons.append("due_date missing")
    return ReadinessVerdict(passed=not reasons, reasons=tuple(reasons))


def evaluate_pilot_degrade_exception(plan: PilotDegradeExceptionPlan) -> ReadinessVerdict:
    reasons: list[str] = []
    if not plan.adr_id.strip():
        reasons.append("adr_id missing")
    if not plan.owner.strip():
        reasons.append("owner missing")
    if not plan.expires_on.strip():
        reasons.append("expires_on missing")
    if not plan.proof_only:
        reasons.append("exception must be proof_only")
    if not plan.blocks_authority_cycles:
        reasons.append("exception must block authority cycles")
    return ReadinessVerdict(passed=not reasons, reasons=tuple(reasons))


def evaluate_base_schema_cross_program(
    samples: Iterable[ProgramSchemaSample],
    *,
    min_programs: int = 2,
) -> ReadinessVerdict:
    rows = tuple(samples)
    usable = [row for row in rows if row.has_program_config and row.workstream_count > 0 and row.has_entity_registry]
    reasons: list[str] = []
    if len(usable) < min_programs:
        reasons.append(f"usable_program_count {len(usable)} < {min_programs}")
    for row in rows:
        if not row.has_program_config:
            reasons.append(f"{row.program_id}: program.yaml missing")
        if row.workstream_count <= 0:
            reasons.append(f"{row.program_id}: workstream registry empty")
        if not row.has_entity_registry:
            reasons.append(f"{row.program_id}: entity registry missing")
        if row.hardcoded_xpf_refs:
            reasons.append(f"{row.program_id}: hardcoded xpf refs {','.join(row.hardcoded_xpf_refs)}")
    return ReadinessVerdict(passed=not reasons, reasons=tuple(reasons))


def evaluate_cross_source_reconciliation(
    plan: CrossSourceReconciliationPlan,
    *,
    required_sources: tuple[str, ...] = ("ado", "eml", "kusto", "icm"),
) -> ReadinessVerdict:
    sources = {source.lower() for source in plan.source_nodes}
    reasons: list[str] = []
    for source in required_sources:
        if source not in sources:
            reasons.append(f"source {source} missing")
    if not plan.has_materiality_policy:
        reasons.append("materiality policy missing")
    if not plan.carries_as_of:
        reasons.append("as_of timestamp missing")
    if not plan.has_disputed_output:
        reasons.append("disputed output missing")
    if not plan.has_operator_adjudication_queue:
        reasons.append("operator adjudication queue missing")
    return ReadinessVerdict(passed=not reasons, reasons=tuple(reasons))


def evaluate_explain_drilldown(plan: ExplainDrilldownPlan) -> ReadinessVerdict:
    reasons: list[str] = []
    if not plan.has_source_excerpt:
        reasons.append("source excerpt missing")
    if not plan.has_counter_source_context:
        reasons.append("counter-source context missing")
    if not plan.has_lineage_keys:
        reasons.append("lineage keys missing")
    if not plan.has_operator_action:
        reasons.append("operator action missing")
    if not plan.has_accessibility_review:
        reasons.append("accessibility review missing")
    return ReadinessVerdict(passed=not reasons, reasons=tuple(reasons))


def evaluate_binding_release_readiness(plan: BindingReleaseReadiness) -> ReadinessVerdict:
    passing = set(plan.passing_check_ids)
    failing = set(plan.failing_check_ids)
    allowed_red = set(plan.allowed_red_blockers)
    reasons: list[str] = []
    if plan.dirty_worktree:
        reasons.append("dirty worktree cannot be canonical release evidence")
    if plan.branch_local_evidence_count > 0:
        reasons.append(f"branch-local evidence count {plan.branch_local_evidence_count} must be 0")
    if not plan.committed_canonical_evidence:
        reasons.append("canonical evidence is not committed")
    if not plan.verifier_self_test_passed:
        reasons.append("verifier self-test did not pass")
    if not plan.generated_evidence_current:
        reasons.append("generated evidence snapshot is not current")
    if not plan.release_owner.strip():
        reasons.append("release owner missing")
    missing_required = tuple(check_id for check_id in plan.required_green_checks if check_id not in passing)
    if missing_required:
        reasons.append(f"required green checks missing: {','.join(missing_required)}")
    unexpected_red = tuple(sorted(failing - allowed_red))
    if unexpected_red:
        reasons.append(f"unexpected red checks: {','.join(unexpected_red)}")
    return ReadinessVerdict(passed=not reasons, reasons=tuple(reasons))


def load_program_schema_sample(program_id: str, *, programs_root: Path) -> ProgramSchemaSample:
    program_dir = programs_root / program_id
    program_yaml = program_dir / "program.yaml"
    registry_yaml = program_dir / "workstream_registry.yaml"
    workstreams = _load_workstream_rows(registry_yaml)
    return ProgramSchemaSample(
        program_id=program_id,
        has_program_config=program_yaml.exists(),
        workstream_count=len(workstreams),
        has_entity_registry=registry_yaml.exists(),
        hardcoded_xpf_refs=_hardcoded_xpf_refs(program_dir, program_id=program_id),
    )


def _load_workstream_rows(path: Path) -> tuple[Mapping[str, object], ...]:
    if not path.exists():
        return ()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return ()
    rows = raw.get("workstreams") if isinstance(raw, Mapping) else None
    if not isinstance(rows, list):
        return ()
    return tuple(row for row in rows if isinstance(row, Mapping))


def _hardcoded_xpf_refs(program_dir: Path, *, program_id: str) -> tuple[str, ...]:
    if program_id == "xpf" or not program_dir.exists():
        return ()
    structural_patterns = (
        re.compile(r"\bprogram_id\s*:\s*['\"]?xpf['\"]?\b", re.IGNORECASE),
        re.compile(r"^\s*id\s*:\s*['\"]?xpf['\"]?\b", re.IGNORECASE | re.MULTILINE),
        re.compile(r"\bprograms[/\\]xpf\b", re.IGNORECASE),
        re.compile(r"\bxpf_weekly\b", re.IGNORECASE),
    )
    refs: list[str] = []
    for filename in ("program.yaml", "workstream_registry.yaml", "workstreams.yaml", "slice_contracts.yaml"):
        path = program_dir / filename
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8").lower()
        except OSError:
            continue
        if any(pattern.search(text) for pattern in structural_patterns):
            refs.append(filename)
    return tuple(refs)
