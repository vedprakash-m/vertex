"""ADF-W2.9 (specs/arch-data-fix.md Section 8.10.5): the one shared
program-synthesis contract consumed by newsletter executive summary, brief,
decision brief, LT deck, and cockpit.

This module owns the Zone-A-safe half of the feature: the request/result
types (mirroring ``context_compiler.py``'s ``ContextCompileRequest`` /
``CompiledContext`` split from ADF-W2.7), deterministic assembly of a
``ProgramSynthesisRequest`` from already-existing accessors (no new
heuristics invented here -- see ``assemble_program_synthesis_request``'s
docstring for exactly which of Section 8.10.5's nine input categories have
a real accessor today and which are out of scope), and content-addressed
persistence + release-gated reads of a ``ProgramSynthesis`` result.

The actual AI call (prompt construction, provider invocation, semantic
validation, release-decision recording) is Zone B -- see
``src/ai/program_synthesizer.py`` -- because it needs an ``LLMProvider``.
This module never imports Zone B or Zone C (INV-ADF-17): consuming a
previously-*released* synthesis is a deterministic ledger read
(``is_ai_output_released``, itself Zone A -- ADF-W2.8) plus a disk read of
the persisted JSON, so cockpit/report/etc. can safely check "is there a
released synthesis to show" without calling AI at read time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from src.core.ai_proposal_store import load_ai_proposals
from src.core.edition_resolver import PROGRAMS_ROOT, load_program
from src.core.models_v2 import AIProposalStatus
from src.core.program_reality import ProgramReality
from src.core.quality_gates.ai_release_audit import is_ai_output_released
from src.core.risk_register_engine import load_risk_register
from src.core.source_waiver_store import load_source_waivers
from src.core.store_factory import build_program_signal_store

#: Section 8.10.5's nine input categories, verbatim, for validation/coverage
#: bookkeeping -- every ``SynthesisInputItem.category`` must be one of these.
INPUT_CATEGORIES: tuple[str, ...] = (
    "verified_workstream_synthesis",
    "critical_path_milestone",
    "dependency_blast_radius",
    "strategic_risk",
    "contradiction",
    "kusto_slo_breach",
    "decision_or_overdue_ask",
    "learned_salience_or_slip_bias",
    "source_degradation",
)

#: Categories this module assembles from real, existing accessors today.
#: The remaining three (see ``assemble_program_synthesis_request``) have no
#: clean existing accessor and are deliberately left empty rather than
#: inventing an undocumented heuristic -- always reported in
#: ``ProgramSynthesisRequest.coverage_notes``.
_ASSEMBLED_CATEGORIES: frozenset[str] = frozenset(
    {
        "verified_workstream_synthesis",
        "critical_path_milestone",
        "strategic_risk",
        "contradiction",
        "kusto_slo_breach",
        "source_degradation",
    }
)


@dataclass(frozen=True, slots=True)
class SynthesisInputItem:
    """One normalized input fact, uniform across all nine categories --
    mirrors ``EvidenceSpan``'s "one shape for many sources" pattern from
    ADF-W2.7's ContextCompiler rather than importing six disparate domain
    types into the prompt-facing contract."""

    category: str
    item_id: str
    summary: str
    evidence_refs: tuple[str, ...] = ()
    severity: str | None = None


@dataclass(frozen=True, slots=True)
class ProgramSynthesisRequest:
    program_id: str
    as_of: datetime
    items: tuple[SynthesisInputItem, ...]
    coverage_notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ProgramSynthesisRecommendation:
    text: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProgramSynthesis:
    """Section 8.10.5's output: through-line, long poles, fact/inference
    split, and source-backed recommendations. ``ai_run_id`` is the QG-29
    lifecycle key (ADF-W2.8) -- a consumer must call
    ``is_released(program_id=..., programs_root=...)`` before trusting a
    freshly-loaded instance, though ``load_latest_released_program_synthesis``
    below already only returns released ones."""

    program_id: str
    ai_run_id: str
    through_line: str
    long_poles: tuple[str, ...]
    facts: tuple[str, ...]
    inferences: tuple[str, ...]
    recommendations: tuple[ProgramSynthesisRecommendation, ...]
    generated_at: datetime
    prompt_version: str
    source_item_count: int

    def is_released(self, *, program_id: str, programs_root: Path = PROGRAMS_ROOT) -> bool:
        return is_ai_output_released(self.ai_run_id, program_id=program_id, programs_root=programs_root)


def assemble_program_synthesis_request(
    program_id: str,
    *,
    programs_root: Path = PROGRAMS_ROOT,
    as_of: datetime | None = None,
) -> ProgramSynthesisRequest:
    """Deterministic, Zone-A-only assembly of Section 8.10.5's input list
    from accessors that already exist elsewhere in the codebase:

    - ``verified_workstream_synthesis`` <- ``load_ai_proposals(..., status=ACCEPTED)``
      ("verified" = the operator has already accepted the AI-drafted
      per-workstream synthesis, ADF-G synthesizer.py's existing output).
    - ``critical_path_milestone`` <- ``ProgramReality.milestones()`` filtered to
      on_track/at_risk/missed/deferred (there is no dedicated critical-path
      graph traversal in this codebase yet, so "critical path" here is
      approximated as *active, non-terminal* milestones -- documented, not
      a true dependency-graph long-pole computation).
    - ``strategic_risk`` <- ``load_risk_register``.
    - ``contradiction`` <- ``ProgramReality.conflicts(open_only=True)``.
    - ``kusto_slo_breach`` <- the program's Kusto-source signals whose
      ``metadata["is_breach"] is True`` (ADF-W2.3's real breach semantics).
    - ``source_degradation`` <- ``load_source_waivers`` (known/waived
      degraded sources -- a narrower proxy than a live source-health score,
      since no program-level "degraded right now" accessor exists yet).

    Left deliberately empty (reported in ``coverage_notes``, never silently
    dropped): ``dependency_blast_radius`` (only computable today inside a
    live report run's ``signal_context.dependency_cascades``, not as a
    standalone program_id-keyed accessor), ``decision_or_overdue_ask``
    (``freshness_engine.build_freshness_report`` needs a caller-supplied
    ``WorkItem`` tuple that this program-level assembly does not have),
    ``learned_salience_or_slip_bias`` (``contradiction_engine.py``'s slip-bias
    modifier is an inline calibration adjustment, not a queryable record).
    """
    resolved_as_of = as_of or datetime.now(timezone.utc)
    items: list[SynthesisInputItem] = []
    coverage_notes: list[str] = []

    proposals = load_ai_proposals(program_id, status=AIProposalStatus.ACCEPTED, programs_root=programs_root)
    for proposal in proposals:
        synthesis = proposal.synthesis
        items.append(
            SynthesisInputItem(
                category="verified_workstream_synthesis",
                item_id=proposal.id,
                summary=synthesis.overall_assessment,
                evidence_refs=synthesis.evidence_refs,
                severity=synthesis.proposed_risk.value if synthesis.proposed_risk is not None else None,
            )
        )
    coverage_notes.append(f"verified_workstream_synthesis: {len(proposals)} accepted proposal(s) assembled.")

    reality = ProgramReality.load(program_id, programs_root=programs_root, as_of=resolved_as_of)

    active_milestone_statuses = {"on_track", "at_risk", "missed", "deferred"}
    milestone_count = 0
    for assessment in reality.milestones():
        record = assessment.record
        status = str(getattr(record, "status", "")).lower()
        if status not in active_milestone_statuses:
            continue
        milestone_count += 1
        name = getattr(record, "name", None) or getattr(record, "id", "unknown milestone")
        target = getattr(record, "target_date", None)
        items.append(
            SynthesisInputItem(
                category="critical_path_milestone",
                item_id=assessment.fact_id or str(name),
                summary=f"{name} | status={status}" + (f" | target={target}" if target else ""),
                evidence_refs=assessment.evidence,
                severity=status,
            )
        )
    coverage_notes.append(f"critical_path_milestone: {milestone_count} active milestone(s) assembled (proxy: non-terminal status, not a dependency-graph critical path).")

    risks = load_risk_register(program_id, programs_root)
    for risk in risks:
        items.append(
            SynthesisInputItem(
                category="strategic_risk",
                item_id=risk.id,
                summary=f"{risk.title}: {risk.description}".strip(": "),
                evidence_refs=(),
                severity=f"{risk.probability.value}/{risk.impact.value}",
            )
        )
    coverage_notes.append(f"strategic_risk: {len(risks)} risk register entr(y/ies) assembled.")

    conflicts = reality.conflicts(open_only=True)
    for conflict in conflicts:
        items.append(
            SynthesisInputItem(
                category="contradiction",
                item_id=conflict.conflict_id,
                summary=conflict.description,
                evidence_refs=conflict.entity_refs,
                severity=conflict.family,
            )
        )
    coverage_notes.append(f"contradiction: {len(conflicts)} open reality conflict(s) assembled.")

    program = load_program(program_id, programs_root=programs_root)
    breach_count = 0
    if program is not None:
        signal_store = build_program_signal_store(program, programs_root=programs_root)
        for signal in signal_store.read(program_id):
            if signal.source != "kusto" or not signal.metadata:
                continue
            if signal.metadata.get("is_breach") is not True:
                continue
            breach_count += 1
            items.append(
                SynthesisInputItem(
                    category="kusto_slo_breach",
                    item_id=signal.id,
                    summary=signal.text,
                    evidence_refs=(signal.raw_ref,) if signal.raw_ref else (),
                    severity="breach",
                )
            )
    coverage_notes.append(f"kusto_slo_breach: {breach_count} breaching Kusto signal(s) assembled.")

    waivers = load_source_waivers(program_id, programs_root=programs_root)
    for waiver in waivers:
        items.append(
            SynthesisInputItem(
                category="source_degradation",
                item_id=waiver.contract_id,
                summary=f"{waiver.contract_id} ({waiver.role}) waived: {waiver.reason}",
                evidence_refs=(),
                severity=waiver.role,
            )
        )
    coverage_notes.append(f"source_degradation: {len(waivers)} source waiver(s) assembled (proxy for degradation -- known-waived, not live health).")

    deferred = tuple(sorted(frozenset(INPUT_CATEGORIES) - _ASSEMBLED_CATEGORIES))
    if deferred:
        coverage_notes.append(
            "Deferred, no existing standalone accessor (see assemble_program_synthesis_request docstring): "
            + ", ".join(deferred)
        )

    return ProgramSynthesisRequest(
        program_id=program_id,
        as_of=resolved_as_of,
        items=tuple(items),
        coverage_notes=tuple(coverage_notes),
    )


def _program_synthesis_dir(program_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return programs_root / program_id / "runtime" / "program_synthesis"


def program_synthesis_path(program_id: str, ai_run_id: str, *, programs_root: Path = PROGRAMS_ROOT) -> Path:
    return _program_synthesis_dir(program_id, programs_root=programs_root) / f"{ai_run_id}.json"


def persist_program_synthesis(synthesis: ProgramSynthesis, *, programs_root: Path = PROGRAMS_ROOT) -> Path | None:
    """Best-effort disk write, same precedent as ADF-W2.7's context-manifest
    persistence: a local disk failure never breaks synthesis generation
    itself, only the cockpit/report read-back convenience."""
    path = program_synthesis_path(synthesis.program_id, synthesis.ai_run_id, programs_root=programs_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_synthesis_to_dict(synthesis), indent=2, sort_keys=True), encoding="utf-8")
        return path
    except OSError:
        return None


def _synthesis_to_dict(synthesis: ProgramSynthesis) -> dict:
    return {
        "program_id": synthesis.program_id,
        "ai_run_id": synthesis.ai_run_id,
        "through_line": synthesis.through_line,
        "long_poles": list(synthesis.long_poles),
        "facts": list(synthesis.facts),
        "inferences": list(synthesis.inferences),
        "recommendations": [
            {"text": rec.text, "evidence_refs": list(rec.evidence_refs)} for rec in synthesis.recommendations
        ],
        "generated_at": synthesis.generated_at.isoformat(),
        "prompt_version": synthesis.prompt_version,
        "source_item_count": synthesis.source_item_count,
    }


def _synthesis_from_dict(payload: dict, *, program_id: str) -> ProgramSynthesis:
    return ProgramSynthesis(
        program_id=program_id,
        ai_run_id=payload["ai_run_id"],
        through_line=payload["through_line"],
        long_poles=tuple(payload.get("long_poles", ())),
        facts=tuple(payload.get("facts", ())),
        inferences=tuple(payload.get("inferences", ())),
        recommendations=tuple(
            ProgramSynthesisRecommendation(text=rec["text"], evidence_refs=tuple(rec.get("evidence_refs", ())))
            for rec in payload.get("recommendations", ())
        ),
        generated_at=datetime.fromisoformat(payload["generated_at"]),
        prompt_version=payload["prompt_version"],
        source_item_count=payload.get("source_item_count", 0),
    )


def load_latest_released_program_synthesis(
    program_id: str, *, programs_root: Path = PROGRAMS_ROOT
) -> ProgramSynthesis | None:
    """Zone-A-safe consumer read: the most recently *generated* persisted
    synthesis whose ``ai_run_id`` has a durable QG-29 ``released`` terminal.
    Returns ``None`` if none exists or none is released yet -- callers (e.g.
    ``cockpit_builder.py``) must treat that as "no synthesis to show", never
    fall back to an unreleased one (Section 8.9.4)."""
    directory = _program_synthesis_dir(program_id, programs_root=programs_root)
    if not directory.exists():
        return None
    candidates: list[ProgramSynthesis] = []
    for path in directory.glob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            candidates.append(_synthesis_from_dict(payload, program_id=program_id))
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            continue
    released = [
        candidate
        for candidate in candidates
        if is_ai_output_released(candidate.ai_run_id, program_id=program_id, programs_root=programs_root)
    ]
    if not released:
        return None
    return max(released, key=lambda candidate: candidate.generated_at)


def content_hash_for_synthesis(synthesis: ProgramSynthesis) -> str:
    """Content-addressed hash for the release-decision record's
    ``released_content_hash`` -- lets an auditor confirm the released
    terminal actually corresponds to this exact payload."""
    canonical = json.dumps(_synthesis_to_dict(synthesis), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


__all__ = [
    "INPUT_CATEGORIES",
    "ProgramSynthesis",
    "ProgramSynthesisRecommendation",
    "ProgramSynthesisRequest",
    "SynthesisInputItem",
    "assemble_program_synthesis_request",
    "content_hash_for_synthesis",
    "load_latest_released_program_synthesis",
    "persist_program_synthesis",
    "program_synthesis_path",
]
