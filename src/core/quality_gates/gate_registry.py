"""Central QG-ID reservation registry (arch-fix.md Phase 0, §0.4).

Gate IDs (``QG-1``, ``QG-27``, ...) have historically been scattered as
string literals across ``src/core/quality_gates/*.py`` with no single
source of truth — which is exactly how the QG-27/WS-5b collision happened
(``ai_budget.py`` originally claimed QG-27 before it was renamed to
QG-WS5B; see that module's docstring). This module gives future gate
authors one place to check *before* claiming a number, and reserves
QG-29 for arch-fix.md's AF-3 fail-closed AI audit gate so nothing else
claims it before AF-3 lands (Phase 2b).

This module only tracks IDs; it does not evaluate gates.

ADF-W0.9 extends the registry with the full ``specs/arch-data-fix.md``
Section 12.1 policy matrix (QG-29, redefined as the AI Release Audit gate
that supersedes the arch-fix AF-3 reservation above, plus new QG-30..QG-40).
``QG_POLICY_MATRIX`` is the single source of truth for those columns; the
governance policy doc (``governance/decisions/adf-gate-policy.md``) and
``RESERVED_GATE_IDS`` below are both derived from it so they cannot drift
apart.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_QUALITY_GATES_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True, slots=True)
class GatePolicy:
    """One row of the arch-data-fix Section 12.1 quality-gate policy matrix."""

    id: str
    name: str
    enforcement_point: str
    enforce_behavior: str
    forceable: str
    activates: str
    delegates_to: str | None = None
    #: True once a real (non-delegating) evaluator module exists for this
    #: gate -- excludes it from RESERVED_GATE_IDS the same way delegates_to
    #: does, without conflating "delegates elsewhere" with "implemented here".
    implemented: bool = False


#: Section 12.1 of specs/arch-data-fix.md, verbatim. QG-33 delegates its
#: evaluation entirely to the pre-existing QG-WS5B cost guard
#: (src/core/quality_gates/adf_economics.py) so there is exactly one
#: economics enforcement path (audit reconciliation, Section 2).
QG_POLICY_MATRIX: tuple[GatePolicy, ...] = (
    GatePolicy(
        id="QG-29",
        name="AI Release Audit",
        enforcement_point="Before AI result consumption",
        enforce_behavior="Discard AI result and use deterministic fallback; state mutation remains blocked",
        forceable="No",
        activates="Slice 2 per migrated feature",
        # ADF-W2.8: src/core/ai_release_audit.py. The lifecycle recording +
        # terminal-release gate (assert_ai_output_released_or_raise) is
        # implemented and tested; no live AI feature call site has been
        # wired to call it yet (that is per-feature migration work, "Slice 2
        # per migrated feature" above -- unchanged from this row's own
        # activation condition).
        implemented=True,
    ),
    GatePolicy(
        id="QG-30",
        name="Source Completeness",
        enforcement_point="Gather and confirm",
        enforce_behavior=(
            "Required unbound/unknown source blocks; transient failures require explicit "
            "unexpired waiver through existing source_waiver_store.py"
        ),
        forceable="Only through waiver policy",
        activates="Slice 2",
    ),
    GatePolicy(
        id="QG-31",
        name="Channel Budget",
        enforcement_point="Channel runtime and report",
        enforce_behavior="Cancel/degrade over-budget optional channel; any inline-prohibited invocation blocks that path",
        forceable="No",
        activates="Slice 1",
    ),
    GatePolicy(
        id="QG-32",
        name="Context Budget",
        enforcement_point="Before provider invocation",
        enforce_behavior="Reject compile and use bounded fallback",
        forceable="No",
        activates="Slice 2",
    ),
    GatePolicy(
        id="QG-33",
        name="AI Economics",
        enforcement_point="Before and after provider invocation",
        enforce_behavior="Spend ceiling blocks additional frontier call; avoidance miss is cockpit/advisory until certification",
        forceable="Spend: no; avoidance: n/a",
        activates="Phase 0 telemetry, Slice 5 enforcement",
        delegates_to="QG-WS5B",
    ),
    GatePolicy(
        id="QG-34",
        name="Cross-Surface Consistency",
        enforcement_point="Before artifact write and confirm",
        enforce_behavior="Material conflict blocks affected artifact; non-material conflict warns",
        forceable="Material: no",
        activates="Slice 2",
        delegates_to="QG-17",
    ),
    GatePolicy(
        id="QG-35",
        name="Actuation Intent",
        enforcement_point="Before outbox enqueue and dispatch",
        enforce_behavior="Missing/stale intent, approval, preflight, or receipt blocks mutation",
        forceable="No",
        activates="Slice 1",
    ),
    GatePolicy(
        id="QG-36",
        name="Value Evidence",
        enforcement_point="Cockpit/value render",
        enforce_behavior="Unsupported metric is hidden and marked unavailable; never blocks program publication",
        forceable="n/a",
        activates="Phase 0",
    ),
    GatePolicy(
        id="QG-37",
        name="State Authority",
        enforcement_point="Startup of mutating command and doctor",
        enforce_behavior="Ambiguous authoritative path blocks mutation",
        forceable="No",
        activates="Slice 1",
        # ADF-W1.9: src/core/quality_gates/state_authority.py. The doctor-fail
        # half is wired (vertex doctor --storage escalates to fail when
        # ambiguous); the mutation-blocking half is built and tested but not
        # yet called from any live command -- see that module's docstring.
        implemented=True,
    ),
    GatePolicy(
        id="QG-38",
        name="Cockpit Freshness",
        enforcement_point="Cockpit build/show",
        enforce_behavior="Stale snapshot displays age and warns; rebuild when safe",
        forceable="n/a",
        activates="Phase 0",
    ),
    GatePolicy(
        id="QG-39",
        name="Source Semantic Integrity",
        enforcement_point="Gather/report/confirm",
        enforce_behavior="Missing required relation/metric semantics blocks affected material section; optional source warns",
        forceable="Through existing source-waiver policy",
        activates="Slice 2",
    ),
    GatePolicy(
        id="QG-40",
        name="Extraction Certification",
        enforcement_point="Proposal authority promotion",
        enforce_behavior=(
            "Uncertified risk/inferred-dependency/entity-binding classes remain advisory and "
            "cannot earn automatic authority"
        ),
        forceable="No",
        activates="Slice 4 pilot reporting; fleet certification in ADF-W6.2",
    ),
)


def _reservation_text(policy: GatePolicy) -> str:
    delegation = f" Delegates to {policy.delegates_to} (single economics path)." if policy.delegates_to else ""
    return (
        f"specs/arch-data-fix.md Section 12.1 -- {policy.name}. "
        f"Enforcement point: {policy.enforcement_point}. "
        f"Enforce behavior: {policy.enforce_behavior}. "
        f"Forceable: {policy.forceable}. Activates: {policy.activates}.{delegation}"
    )


# Gate IDs reserved for future work that has not landed yet. A reservation
# here does NOT mean the gate exists or is enforced — it means the ID is
# spoken for and must not be reused by unrelated work. Policies with
# ``delegates_to`` set (QG-33) already have a real implementation
# (src/core/quality_gates/adf_economics.py) and are therefore excluded here;
# ``scan_defined_gate_ids`` would otherwise find them and
# ``assert_no_reservation_collisions`` would (correctly) treat a still-reserved
# but already-implemented ID as a bug.
RESERVED_GATE_IDS: dict[str, str] = {
    policy.id: _reservation_text(policy)
    for policy in QG_POLICY_MATRIX
    if policy.delegates_to is None and not policy.implemented
}

# Gate IDs matched inline, e.g. GateEvaluation("QG-12", ...) or gate_id="QG-20".
_INLINE_GATE_ID_RE = re.compile(r'(?:GateEvaluation\(\s*|gate_id\s*=\s*)"(QG-[A-Za-z0-9]+)"')
# Gate IDs assigned to a module-level constant, e.g. GATE_ID = "QG-27".
_CONST_GATE_ID_RE = re.compile(r'^_?GATE_ID\s*=\s*"(QG-[A-Za-z0-9]+)"', re.MULTILINE)


def scan_defined_gate_ids(quality_gates_dir: Path = _QUALITY_GATES_DIR) -> set[str]:
    """Scan ``src/core/quality_gates/*.py`` for gate IDs already implemented in code."""
    found: set[str] = set()
    for path in quality_gates_dir.glob("*.py"):
        if path.name in {"gate_registry.py", "models.py"}:
            continue
        text = path.read_text(encoding="utf-8")
        found.update(_INLINE_GATE_ID_RE.findall(text))
        found.update(_CONST_GATE_ID_RE.findall(text))
    return found


def assert_no_reservation_collisions(quality_gates_dir: Path = _QUALITY_GATES_DIR) -> None:
    """Raise if any reserved gate ID is already implemented in code, or vice versa."""
    defined = scan_defined_gate_ids(quality_gates_dir)
    collisions = defined & RESERVED_GATE_IDS.keys()
    if collisions:
        raise ValueError(
            f"Gate ID(s) {sorted(collisions)} are both reserved (not yet implemented) "
            "and already defined in code — resolve the collision before proceeding."
        )
