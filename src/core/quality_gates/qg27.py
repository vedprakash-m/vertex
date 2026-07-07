"""WI-3.9: QG-27 — Truth-level gate for ProgramReality.

Gate behavior:
- HARD block (exit_code=2): any material-disputed fact in the snapshot
  (fact.conflict + unresolved + MATERIALITY_PREDICATES match)
- ADVISORY block (exit_code=1, forceable=True): any fact with truth_level
  below a minimum threshold (default: below SOURCE_VALIDATED)

Minimum threshold: SOURCE_VALIDATED (level 3 of 5).
Rationale: publishing program state with only RAW_OBSERVED or
GOVERNANCE_LOCKED facts would mislead consumers.

Zone A module — no imports from src.ai or src.m365.
"""
from __future__ import annotations

from dataclasses import dataclass

from src.core.program_fact_store import ProgramFactRevision, ProgramFactSnapshot
from src.core.quality_gates.models import GateEvaluation
from src.core.truth_levels import TruthLevel

# ---------------------------------------------------------------------------
# Gate parameters
# ---------------------------------------------------------------------------

GATE_ID = "QG-27"

# Minimum acceptable truth level (anything below triggers advisory)
_MIN_TRUTH_LEVEL = TruthLevel.SOURCE_VALIDATED

# Exit codes
_EXIT_HARD = 2
_EXIT_ADVISORY = 1

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TRUTH_RANK: dict[TruthLevel, int] = {
    TruthLevel.RAW_OBSERVED: 0,
    TruthLevel.SOURCE_VALIDATED: 1,
    TruthLevel.CORROBORATED: 2,
    TruthLevel.HUMAN_CONFIRMED: 3,
    TruthLevel.GOVERNANCE_LOCKED: 4,
}


def _is_material_disputed(fact: ProgramFactRevision) -> bool:
    """True if this fact represents an UNRESOLVED material conflict."""
    if fact.fact_type != "fact.conflict":
        return False
    payload = fact.payload
    # An unresolved conflict has no resolution recorded
    resolved = payload.get("resolved", False)
    is_material = payload.get("is_material", False)
    return bool(is_material) and not bool(resolved)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class QG27Input:
    """Input for QG-27 evaluation."""

    snapshot: ProgramFactSnapshot
    truth_levels: dict[str, TruthLevel]  # natural_key → TruthLevel


def evaluate_qg27(qg27_input: QG27Input) -> GateEvaluation:
    """Evaluate QG-27: truth-level gate.

    Returns:
    - Passed: all material-disputed and below-min-truth checks pass
    - Hard block (exit_code=2): material-disputed conflicts exist
    - Advisory (exit_code=1, forceable=True): facts below min truth level
    """
    material_disputed = [
        f.natural_key for f in qg27_input.snapshot.facts
        if _is_material_disputed(f)
    ]
    if material_disputed:
        return GateEvaluation(
            gate_id=GATE_ID,
            passed=False,
            message=(
                f"QG-27 HARD BLOCK: {len(material_disputed)} material-disputed conflict(s) "
                f"are unresolved. Resolve or override before publishing. "
                f"Affected: {', '.join(material_disputed[:3])}"
                + (" …" if len(material_disputed) > 3 else "")
            ),
            exit_code=_EXIT_HARD,
            forceable=False,
        )

    min_rank = _TRUTH_RANK[_MIN_TRUTH_LEVEL]
    below_min = [
        nk for nk, tl in qg27_input.truth_levels.items()
        if _TRUTH_RANK.get(tl, 0) < min_rank
    ]
    if below_min:
        return GateEvaluation(
            gate_id=GATE_ID,
            passed=False,
            message=(
                f"QG-27 ADVISORY: {len(below_min)} fact(s) have truth level below "
                f"{_MIN_TRUTH_LEVEL.value}. Review before publishing. "
                f"Affected: {', '.join(sorted(below_min)[:3])}"
                + (" …" if len(below_min) > 3 else "")
            ),
            exit_code=_EXIT_ADVISORY,
            forceable=True,
        )

    return GateEvaluation(
        gate_id=GATE_ID,
        passed=True,
        message="QG-27 passed: no material-disputed conflicts; all facts meet minimum truth level.",
        exit_code=0,
        forceable=False,
    )
