"""LLM-as-judge activation assessment library (activation.md v1.25 §6.16).

A deep-expertise LLM judge that reads REAL activation evidence (from
``output/activation-report.json`` + program artifacts) and renders informed,
falsifiable verdicts on activation gates, tests, and authority promotions —
replacing the substring-matching scaffolds that over-claimed.

Design principles (see ADR-0009):
  - **Evidence-grounded**: every verdict cites specific data points. A gate is
    FAIL when evidence is absent, never PASS because a scaffold passed.
  - **Fail-closed**: when the judge LLM is unavailable (no
    ``VERTEX_AI_JUDGE_DEPLOYMENT``, offline, budget exceeded), verdicts are
    ``JUDGE_UNAVAILABLE`` with the deterministic evidence preserved — the judge
    never silently passes.
  - **Judge-independence**: the judge must use a deployment distinct from the
    extractor (``verify_judge_independence``); enforced at construction.
  - **Human owns authority flips**: even when the judge endorses a shadow→primary
    flip, ``auto_executable`` is False — it is the highest-blast-radius event
    (AG-4/AG-18) and requires a human decider + rollback drill.

This module orchestrates the LLM call via an injected ``LLMProvider`` (mirroring
``src/ai/rev/judge.py``) and lives in ``src/ai/`` (Zone B) — not ``src/core/`` —
precisely because it makes a direct model call and must route the raw response
through the shared safety pipeline (``process_generated_text``: PII scrub +
injection detection) before use, per the D-26 orchestrator-safety contract
(``tests/contracts/test_ai_safety_pipeline.py``); ``src/core/`` may never
import ``src.ai``, so this module cannot satisfy that contract from Zone A.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.ai._pipeline import process_generated_text

log = logging.getLogger(__name__)

JUDGE_PROMPT_VERSION = "activation_judge.v1"
_ACTIVATION_JUDGE_FEATURE = "activation_judge"

# Status values a verdict may carry.
STATUS_PASS = "PASS"
STATUS_FAIL = "FAIL"
STATUS_AMBIGUOUS = "AMBIGUOUS"
STATUS_JUDGE_UNAVAILABLE = "JUDGE_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    """One gate's deep-expertise assessment."""

    gate_id: str
    status: str  # PASS | FAIL | AMBIGUOUS | JUDGE_UNAVAILABLE
    confidence: float
    bar: str  # A | B | C
    blocker_type: str  # code | data | external | human | none
    reasoning: str
    evidence_refs: tuple[str, ...] = ()
    recommendation: str = ""
    alternatives: tuple[str, ...] = ()
    decision_context: str = ""
    auto_executable: bool = False
    flip_assessment: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "gate_id": self.gate_id,
            "status": self.status,
            "confidence": round(self.confidence, 3),
            "bar": self.bar,
            "blocker_type": self.blocker_type,
            "reasoning": self.reasoning,
            "evidence_refs": list(self.evidence_refs),
            "recommendation": self.recommendation,
            "alternatives": list(self.alternatives),
            "decision_context": self.decision_context,
            "auto_executable": self.auto_executable,
        }
        if self.flip_assessment is not None:
            d["flip_assessment"] = self.flip_assessment
        return d


@dataclass(frozen=True, slots=True)
class JudgeReport:
    """Aggregate assessment across all gates + a recommended execution order."""

    verdicts: tuple[JudgeVerdict, ...]
    sequence_recommendation: tuple[str, ...]
    human_decisions: tuple[str, ...]
    summary: str
    judge_available: bool
    judge_model: str
    prompt_version: str
    git_sha: str
    generated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdicts": [v.to_dict() for v in self.verdicts],
            "sequence_recommendation": list(self.sequence_recommendation),
            "human_decisions": list(self.human_decisions),
            "summary": self.summary,
            "judge_available": self.judge_available,
            "judge_model": self.judge_model,
            "prompt_version": self.prompt_version,
            "git_sha": self.git_sha,
            "generated_at": self.generated_at,
        }

    @property
    def failed(self) -> bool:
        """True if any FAIL verdict exists (ignoring JUDGE_UNAVAILABLE)."""
        return any(v.status == STATUS_FAIL for v in self.verdicts)

    def human_decision_packets(self) -> tuple[JudgeVerdict, ...]:
        """The AMBIGUOUS verdicts that need a human decision, with full context."""
        return tuple(v for v in self.verdicts if v.status == STATUS_AMBIGUOUS)


# ---------------------------------------------------------------------------
# Prompt assembly
# ---------------------------------------------------------------------------


def build_judge_user_prompt(
    activation_report: dict[str, Any],
    *,
    program_artifacts: dict[str, Any] | None = None,
    target_gates: tuple[str, ...] | None = None,
    flip_family: str | None = None,
) -> str:
    """Assemble the evidence payload the judge will reason over.

    The judge receives: (a) the gate definitions + acceptance bars it's assessing,
    (b) the REAL evidence data (cycle counts, corpus κ/CI, freeze hashes,
    counterfactual diff status, flip-gate state), (c) the deterministic
    verifier's finding for each gate. It cites these in its verdicts.
    """
    artifacts = program_artifacts or {}
    checks = activation_report.get("checks", [])
    if target_gates:
        checks = [c for c in checks if c.get("check_id") in set(target_gates)]
    payload = {
        "program": activation_report.get("program"),
        "keystone_family": activation_report.get("keystone_family"),
        "git_sha": activation_report.get("git_sha"),
        "dirty_worktree": activation_report.get("dirty_worktree"),
        "deterministic_findings": [
            {
                "check_id": c.get("check_id"),
                "status": c.get("status"),
                "summary": c.get("summary"),
                "details": c.get("details"),
            }
            for c in checks
        ],
        "family_matrix": activation_report.get("family_matrix", []),
        "program_artifacts": {
            k: _compact(v) for k, v in artifacts.items()
        },
        "assessment_focus": (
            f"Assess the authority-flip readiness for family '{flip_family}' "
            "(return flip_assessment in that verdict)."
            if flip_family
            else "Assess all gates against their completion bar."
        ),
    }
    return json.dumps(payload, indent=2, default=str)


def _compact(value: Any, *, max_items: int = 50) -> Any:
    """Trim large artifact payloads so the judge prompt stays within token budget."""
    if isinstance(value, list) and len(value) > max_items:
        return {"_truncated": True, "count": len(value), "sample": value[:max_items]}
    if isinstance(value, dict):
        return {k: _compact(v, max_items=max_items) for k, v in value.items()}
    return value


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def parse_judge_response(raw: dict[str, Any]) -> tuple[tuple[JudgeVerdict, ...], tuple[str, ...], tuple[str, ...], str]:
    """Parse the judge's JSON response into structured verdicts.

    Tolerant of missing/malformed fields — a verdict with no status is dropped
    (never a false PASS). Returns (verdicts, sequence, human_decisions, summary).
    """
    raw_verdicts = raw.get("verdicts", []) if isinstance(raw, dict) else []
    verdicts: list[JudgeVerdict] = []
    for rv in raw_verdicts:
        if not isinstance(rv, dict):
            continue
        status = str(rv.get("status", "")).upper()
        if status not in {STATUS_PASS, STATUS_FAIL, STATUS_AMBIGUOUS}:
            continue  # drop unrecognized status — never infer a pass
        gate_id = str(rv.get("gate_id", "unknown"))
        verdicts.append(JudgeVerdict(
            gate_id=gate_id,
            status=status,
            confidence=_clamp_float(rv.get("confidence", 0.0)),
            bar=str(rv.get("bar", "?")),
            blocker_type=str(rv.get("blocker_type", "none")),
            reasoning=str(rv.get("reasoning", "")),
            evidence_refs=tuple(str(x) for x in (rv.get("evidence_refs") or [])),
            recommendation=str(rv.get("recommendation", "")),
            alternatives=tuple(str(x) for x in (rv.get("alternatives") or [])),
            decision_context=str(rv.get("decision_context", "")),
            auto_executable=bool(rv.get("auto_executable", False)),
            flip_assessment=rv.get("flip_assessment") if isinstance(rv.get("flip_assessment"), dict) else None,
        ))
    sequence = tuple(str(x) for x in (raw.get("sequence_recommendation") or []))
    human = tuple(str(x) for x in (raw.get("human_decisions") or []))
    summary = str(raw.get("summary", ""))
    return tuple(verdicts), sequence, human, summary


def _clamp_float(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Assessment orchestration
# ---------------------------------------------------------------------------


class _JudgeResponseRejected(Exception):
    """Raised when the raw judge response fails the shared safety pipeline."""


def _scrub_judge_response(raw: dict[str, Any]) -> dict[str, Any]:
    """Route every string value in the raw judge response through the shared
    safety pipeline (D-26: PII scrub + injection detection) before it is
    parsed/used. Recurses through nested dicts/lists; non-string leaves pass
    through unchanged. Fail-closed: an injection hit anywhere in the payload
    rejects the whole response (falls back to JUDGE_UNAVAILABLE upstream).
    """
    def _scrub(value: Any) -> Any:
        if isinstance(value, str):
            if not value.strip():
                return value
            try:
                processed = process_generated_text(value)
            except Exception as exc:  # AIPipelineError on injection detection
                raise _JudgeResponseRejected(str(exc)) from exc
            return processed.text
        if isinstance(value, dict):
            return {k: _scrub(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_scrub(v) for v in value]
        return value

    return _scrub(raw)


def assess_activation(
    *,
    activation_report: dict[str, Any],
    program_artifacts: dict[str, Any] | None,
    client: Any,  # LLMProvider — injected so this module stays test-friendly
    system_prompt: str,
    judge_model: str,
    git_sha: str,
    target_gates: tuple[str, ...] | None = None,
    flip_family: str | None = None,
) -> JudgeReport:
    """Run the deep-expertise judge over the activation evidence.

    When ``client`` is ``None`` (judge unavailable), every gate receives a
    ``JUDGE_UNAVAILABLE`` verdict that preserves the deterministic finding —
    fail-closed, never a silent pass.
    """
    user_prompt = build_judge_user_prompt(
        activation_report,
        program_artifacts=program_artifacts,
        target_gates=target_gates,
        flip_family=flip_family,
    )
    checks = activation_report.get("checks", [])
    if target_gates:
        checks = [c for c in checks if c.get("check_id") in set(target_gates)]

    if client is None:
        return _unavailable_report(checks, git_sha, judge_model, flip_family)

    try:
        from src.ai.tiered_router import RouteResult, route_through_tiers
        from src.core.policy_loader import load_ai_feature_policy

        policy = load_ai_feature_policy(_ACTIVATION_JUDGE_FEATURE)
        route_result: RouteResult[dict[str, Any]] = route_through_tiers(
            _ACTIVATION_JUDGE_FEATURE,
            deterministic_fn=None,
            local_fn=None,
            frontier_fn=lambda: client.structured(
                system_prompt,
                user_prompt,
                parser=lambda p: p if isinstance(p, dict) else {},
                max_tokens=policy.max_tokens,
                prompt_version=JUDGE_PROMPT_VERSION,
            ),
        )
        raw = route_result.value if route_result.value is not None else {}
        raw = raw if isinstance(raw, dict) else {}
        raw = _scrub_judge_response(raw)
    except Exception:
        log.warning("activation judge LLM call failed — returning JUDGE_UNAVAILABLE", exc_info=True)
        return _unavailable_report(checks, git_sha, judge_model, flip_family)

    verdicts, sequence, human, summary = parse_judge_response(raw)
    # Guarantee every requested gate has a verdict; fill gaps as FAIL
    # (evidence-absent ≠ evidence-passed — a missing judge verdict is a fail,
    # never an inferred pass).
    seen = {v.gate_id for v in verdicts}
    for c in checks:
        cid = c.get("check_id")
        if cid and cid not in seen:
            verdicts = (*verdicts, _fallback_verdict(cid, c))

    return JudgeReport(
        verdicts=verdicts,
        sequence_recommendation=sequence,
        human_decisions=human,
        summary=summary or _default_summary(verdicts),
        judge_available=True,
        judge_model=judge_model,
        prompt_version=JUDGE_PROMPT_VERSION,
        git_sha=git_sha,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def _unavailable_report(
    checks: list[dict[str, Any]],
    git_sha: str,
    judge_model: str,
    flip_family: str | None,
) -> JudgeReport:
    """Fail-closed: every gate is JUDGE_UNAVAILABLE, deterministic finding preserved."""
    verdicts = tuple(
        JudgeVerdict(
            gate_id=str(c.get("check_id", "unknown")),
            status=STATUS_JUDGE_UNAVAILABLE,
            confidence=0.0,
            bar="?",
            blocker_type="none",
            reasoning=(
                f"Judge unavailable — deterministic verifier reported "
                f"{c.get('status','?')}: {c.get('summary','')}"
            ),
            evidence_refs=(str(c.get("summary", "")),),
        )
        for c in checks
    )
    return JudgeReport(
        verdicts=verdicts,
        sequence_recommendation=(),
        human_decisions=(),
        summary=(
            "JUDGE_UNAVAILABLE — the LLM judge could not run (no "
            "VERTEX_AI_JUDGE_DEPLOYMENT, offline, or budget exceeded). "
            "Deterministic findings preserved; no gate was judged by the LLM."
        ),
        judge_available=False,
        judge_model=judge_model,
        prompt_version=JUDGE_PROMPT_VERSION,
        git_sha=git_sha,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def _fallback_verdict(gate_id: str, check: dict[str, Any]) -> JudgeVerdict:
    """When the judge omits a requested gate, FAIL it (never infer a pass)."""
    return JudgeVerdict(
        gate_id=gate_id,
        status=STATUS_FAIL,
        confidence=0.5,
        bar="?",
        blocker_type="none",
        reasoning=(
            f"The judge did not return a verdict for this gate; deterministic "
            f"verifier reported {check.get('status','?')}. Treated as FAIL "
            "(evidence-absent is not evidence-passed)."
        ),
        evidence_refs=(str(check.get("summary", "")),),
    )


def _default_summary(verdicts: tuple[JudgeVerdict, ...]) -> str:
    passed = sum(1 for v in verdicts if v.status == STATUS_PASS)
    failed = sum(1 for v in verdicts if v.status == STATUS_FAIL)
    ambiguous = sum(1 for v in verdicts if v.status == STATUS_AMBIGUOUS)
    return (
        f"Judge assessed {len(verdicts)} gates: {passed} PASS, {failed} FAIL, "
        f"{ambiguous} AMBIGUOUS."
    )


# ---------------------------------------------------------------------------
# Deterministic deep-expertise assessor (no LLM required)
# ---------------------------------------------------------------------------

# Maps the activation-report check_ids to their completion bar + a human label,
# so the deterministic assessor can classify each gate's blocker type and
# sequence the optimal plan. This encodes the same expertise the LLM prompt
# encodes, applied directly to the real evidence.
_GATE_BAR: dict[str, str] = {
    "AG-1-COUNTERFACTUAL-DIFF": "A",
    "AG-3-CLEAN-CYCLE": "B",
    "AG-3-CLEAN-CYCLE-STREAK": "B",
    "AG-2-CORPUS-CERTIFICATION": "B",
    "P2-CORPUS-FREEZE-MANIFEST": "B",
    "O-0-DATA-SUFFICIENCY": "A",
    "P-1-RAW-DATA": "A",
    "PS-5-CORPUS-SNAPSHOT": "B",
    "AG-9-CONFLICT-WIRED": "B",
}

# Blocker type per known red gate: what's actually blocking realization.
_GATE_BLOCKER: dict[str, str] = {
    "AG-1-COUNTERFACTUAL-DIFF": "data",      # needs real render artifacts
    "AG-3-CLEAN-CYCLE": "external",           # needs Azure CS + a real EML
    "AG-3-CLEAN-CYCLE-STREAK": "data",        # needs 5 real clean cycles
    "AG-2-CORPUS-CERTIFICATION": "human",     # needs a 2nd annotator + κ≥0.7
    "P2-CORPUS-FREEZE-MANIFEST": "human",     # operator must freeze the corpus
    "O-0-DATA-SUFFICIENCY": "human",          # greenfield annotation
    "P-1-RAW-DATA": "data",                   # 29/30 reachable EMLs
    "PS-5-CORPUS-SNAPSHOT": "human",          # single-annotator corpus
}


def assess_activation_deterministic(
    *,
    activation_report: dict[str, Any],
    program_artifacts: dict[str, Any] | None,
    git_sha: str,
    flip_family: str | None = None,
) -> JudgeReport:
    """Deep-expertise assessment WITHOUT an LLM — applies the bedrock rules
    directly to the real evidence.

    This is the fallback that runs when no judge deployment is provisioned, so
    the optimal-sequence recommendation + human-decision packets are always
    available. The LLM judge (``assess_activation`` with a live client) enhances
    this with free-form reasoning; both consume the same evidence and honor the
    same falsifiability rules.
    """
    checks = activation_report.get("checks", [])
    verdicts: list[JudgeVerdict] = []
    for c in checks:
        cid = str(c.get("check_id", "unknown"))
        det_status = str(c.get("status", ""))
        summary = str(c.get("summary", ""))
        bar = _GATE_BAR.get(cid, "?")
        blocker = _GATE_BLOCKER.get(cid, "none")
        if det_status == "fail":
            status = STATUS_FAIL
            reasoning = (
                f"Deterministic verifier FAILs: {summary}. "
                f"Blocker type: {blocker}."
            )
        elif det_status == "pass":
            status = STATUS_PASS
            reasoning = f"Deterministic verifier PASSes: {summary}."
        else:  # info / unknown
            status = STATUS_PASS
            reasoning = f"Deterministic verifier {det_status.upper()}: {summary}."
        verdicts.append(JudgeVerdict(
            gate_id=cid,
            status=status,
            confidence=0.95 if det_status in ("pass", "fail") else 0.5,
            bar=bar,
            blocker_type=blocker,
            reasoning=reasoning,
            evidence_refs=(summary,),
        ))

    sequence, human_decisions = _derive_sequence_and_decisions(verdicts, program_artifacts or {})

    # The keystone flip readiness assessment (AG-4/AG-18) — always a human action.
    if flip_family:
        flip_v = _assess_flip_readiness(flip_family, program_artifacts or {})
        verdicts.append(flip_v)
        if flip_v.status == STATUS_AMBIGUOUS:
            human_decisions = (*human_decisions, flip_v.gate_id)

    failed = [v for v in verdicts if v.status == STATUS_FAIL]
    return JudgeReport(
        verdicts=tuple(verdicts),
        sequence_recommendation=sequence,
        human_decisions=human_decisions,
        summary=_deterministic_summary(verdicts, failed),
        judge_available=False,
        judge_model="deterministic-expertise-v1",
        prompt_version=f"{JUDGE_PROMPT_VERSION} (deterministic fallback)",
        git_sha=git_sha,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def _derive_sequence_and_decisions(
    verdicts: list[JudgeVerdict], artifacts: dict[str, Any]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Produce the optimal execution order + the human decisions required.

    Ordering principle: (1) clear code gaps first (already zero now), (2) the
    keystone data-sufficiency + raw-data acquisition (P-1/O-0 — everything blocks
    on this), (3) the external provisioning that unblocks clean cycles, (4) the
    corpus annotation labor, (5) the activation proof, (6) promotion/hardening.
    """
    failed_ids = {v.gate_id for v in verdicts if v.status == STATUS_FAIL}
    sequence: list[str] = []

    # 1. Raw-data acquisition is the absolute keystone — everything blocks on it.
    if "P-1-RAW-DATA" in failed_ids or "O-0-DATA-SUFFICIENCY" in failed_ids:
        sequence.append(
            "P-1/O-0: acquire ≥30 reachable keystone EMLs + greenfield-annotate "
            "≥25–30 milestone.completed instances (everything else blocks on this)"
        )
    # 2. External provisioning (parallel to annotation).
    if "AG-3-CLEAN-CYCLE" in failed_ids:
        sequence.append(
            "P1: provision Azure Content Safety + a distinct judge LLM deployment "
            "(IT ticket — schedule-critical, not an operator step)"
        )
    # 3. Corpus annotation labor (the trust blocker).
    if "AG-2-CORPUS-CERTIFICATION" in failed_ids or "P2-CORPUS-FREEZE-MANIFEST" in failed_ids:
        sequence.append(
            "P2: name + ramp a 2nd annotator; dual-label ≥20 keystone docs; compute "
            "κ (≥0.7) + Wilson CIs; freeze the corpus manifest"
        )
    # 4. The activation proof.
    if "AG-1-COUNTERFACTUAL-DIFF" in failed_ids:
        sequence.append(
            "P6: turn the fact bridge ON for the keystone family; run the first real "
            "approve→project→report; supply the with/without counterfactual render diff"
        )
    # 5. Clean-cycle ladder.
    if "AG-3-CLEAN-CYCLE-STREAK" in failed_ids:
        sequence.append(
            "P9: run ≥5 consecutive is_clean_cycle() cycles on real keystone EMLs; "
            "then flip shadow→primary (human action + rollback drill)"
        )

    # Human decisions: gates that are genuinely ambiguous / require a judgment call.
    human: list[str] = []
    # The pilot-degrade exception (RK-1) is the canonical ambiguous decision:
    # run the Bar-A proof under Azure-CS-absent degrade, or wait for provisioning?
    cycle = artifacts.get("last_cycle") or {}
    if isinstance(cycle, dict) and cycle.get("shield_degrade"):
        human.append(
            "RK-1: run the Bar-A activation proof under the pilot-degrade exception "
            "(Azure CS absent), or wait for provisioning? The ADR permits proof-only "
            "degrade; authority cycles still require real clean cycles."
        )
    if not sequence:
        sequence.append("No red gates remain — activation is realized. Run the LLM judge for final sign-off.")
    return tuple(sequence), tuple(human)


def _assess_flip_readiness(
    family: str, artifacts: dict[str, Any]
) -> JudgeVerdict:
    """Assess whether a family is ready to flip shadow→primary.

    Always ``auto_executable=False`` — the flip is the highest-blast-radius event
    and requires a human decider + rollback drill (AG-4/AG-18).
    """
    metrics = artifacts.get("quality_metrics") or {}
    sor_state = artifacts.get("fact_sor_state") or {}
    kappa = metrics.get("kappa") if isinstance(metrics, dict) else None
    ci_low = metrics.get("g_xtract_prec_ci_low") if isinstance(metrics, dict) else None
    family_mode = ""
    if isinstance(sor_state, dict):
        family_modes = sor_state.get("family_modes") or {}
        family_mode = str(family_modes.get(family, "")) if isinstance(family_modes, dict) else ""

    reasons: list[str] = []
    if kappa is None or kappa < 0.7:
        reasons.append(f"kappa {kappa!r} < 0.7 (corpus not certified)")
    if ci_low is None or ci_low < 0.80:
        reasons.append(f"g_xtract_prec_ci_low {ci_low!r} < 0.80 (Wilson lower bound)")
    ready = not reasons
    return JudgeVerdict(
        gate_id=f"{family}-FLIP-READINESS",
        status=STATUS_PASS if ready else STATUS_FAIL,
        confidence=0.9,
        bar="B",
        blocker_type="human" if not ready else "none",
        reasoning=(
            f"Flip readiness for {family}: " + ("READY (human action + rollback drill required). "
            if ready else "; ".join(reasons) + ".")
        ),
        evidence_refs=(f"kappa={kappa}", f"ci_low={ci_low}", f"family_mode={family_mode!r}"),
        recommendation=(
            "Human: execute the rollback drill, then flip via evaluate_family_flip_gate "
            "(the judge endorses readiness but the flip is a human action — AG-4/AG-18)."
            if ready else "Do not flip — corpus not certified."
        ),
        auto_executable=False,  # NEVER auto-flip
        flip_assessment={
            "flip_safe": ready,
            "corpus_certification_met": ready,
            "clean_cycle_streak_met": None,  # not assessable without cycle history here
            "human_action_required": True,
        },
    )


def _deterministic_summary(verdicts: list[JudgeVerdict], failed: list[JudgeVerdict]) -> str:
    passed = sum(1 for v in verdicts if v.status == STATUS_PASS)
    failed_n = len(failed)
    code_gaps = sum(1 for v in failed if v.blocker_type == "code")
    data_blockers = sum(1 for v in failed if v.blocker_type == "data")
    external = sum(1 for v in failed if v.blocker_type == "external")
    human = sum(1 for v in failed if v.blocker_type == "human")
    return (
        f"Deep-expertise assessment: {passed} PASS, {failed_n} FAIL "
        f"(code gaps: {code_gaps}, data: {data_blockers}, external: {external}, human: {human}). "
        f"{'Zero code gaps remain — all blockers are operator/external.' if code_gaps == 0 else 'Code gaps remain.'} "
        f"Highest-leverage action: acquire ≥30 reachable keystone EMLs + greenfield annotation "
        f"(P-1/O-0) — everything else is its precondition or follow-on."
    )


__all__ = [
    "JUDGE_PROMPT_VERSION",
    "STATUS_PASS",
    "STATUS_FAIL",
    "STATUS_AMBIGUOUS",
    "STATUS_JUDGE_UNAVAILABLE",
    "JudgeVerdict",
    "JudgeReport",
    "build_judge_user_prompt",
    "parse_judge_response",
    "assess_activation",
    "assess_activation_deterministic",
]
