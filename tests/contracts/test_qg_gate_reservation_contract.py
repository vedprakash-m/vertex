"""arch-fix.md Phase 0 (§0.4): QG-29 is formally reserved for AF-3 (Phase 2b).

Prevents a repeat of the QG-27/WS-5b collision (ai_budget.py originally
claimed QG-27 before being renamed to QG-WS5B) by asserting no gate ID is
simultaneously reserved and already implemented in code.
"""
from __future__ import annotations

from src.core.quality_gates.gate_registry import (
    RESERVED_GATE_IDS,
    assert_no_reservation_collisions,
    scan_defined_gate_ids,
)


def test_qg_29_is_reserved() -> None:
    assert "QG-29" in RESERVED_GATE_IDS
    assert "AF-3" in RESERVED_GATE_IDS["QG-29"]


def test_reserved_ids_are_not_yet_implemented() -> None:
    defined = scan_defined_gate_ids()
    assert not (defined & RESERVED_GATE_IDS.keys()), (
        "A reserved gate ID has been implemented without updating "
        "gate_registry.RESERVED_GATE_IDS — remove the reservation."
    )


def test_assert_no_reservation_collisions_passes_on_live_tree() -> None:
    assert_no_reservation_collisions()


def test_scan_finds_known_defined_gates() -> None:
    defined = scan_defined_gate_ids()
    # Spot-check a handful of gates known to exist across different
    # registration styles (inline GateEvaluation(...), gate_id=kwarg, and a
    # module-level constant) so the scanner's regex coverage doesn't silently
    # narrow over time.
    for gate_id in ("QG-1", "QG-20", "QG-27", "QG-28", "QG-WS5B"):
        assert gate_id in defined, f"scanner failed to find known gate {gate_id!r}"
