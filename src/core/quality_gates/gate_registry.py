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
"""
from __future__ import annotations

import re
from pathlib import Path

_QUALITY_GATES_DIR = Path(__file__).resolve().parent

# Gate IDs reserved for future work that has not landed yet. A reservation
# here does NOT mean the gate exists or is enforced — it means the ID is
# spoken for and must not be reused by unrelated work.
RESERVED_GATE_IDS: dict[str, str] = {
    "QG-29": (
        "Reserved for arch-fix.md AF-3 (Phase 2b, Fail-Closed AI Audit; spec "
        "archived to .archive/specs/arch-fix.md, remaining scope tracked in "
        "specs/backlog.md §7 BL-C3): "
        "blocks state-mutating AI output before any partial confirm/archive "
        "promotion when a durable `released` audit record cannot be written. "
        "Highest gate ID at time of reservation was QG-28; do not reuse QG-29 "
        "for unrelated gates."
    ),
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
