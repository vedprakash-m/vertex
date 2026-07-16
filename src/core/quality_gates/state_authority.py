"""QG-37 State Authority (specs/arch-data-fix.md Section 12.1 / 8.12.3, ADF-W1.9).

Detects the same PS-14 split-brain hazard as
``src/commands/doctor_checks/storage_checks.py::_stray_fact_store_database_check``
(the two must never drift apart -- this module owns the detection logic and
the doctor check delegates to it) and exposes it as a real, gate-registry-backed
evaluation: an ambiguous canonical fact-store path (more than one
``vertex.sqlite3`` candidate on disk for a program) means mutating commands
cannot know for certain they are reading/writing the program's real state.

``assert_state_authority_or_raise`` is the mutation-blocking half of the
gate, called at ``confirm.py``'s lease-acquisition point since the Platform
DRI's live decision to activate the hard block (2026-07-13, no ``--force``
override; XPF's own stray database was archived first via
``scripts/reconcile_stray_fact_stores.py`` under that same decision).

ADF-W5.8 (Section 8.2.5's "duplicate state path" alert category):
``evaluate_state_authority_gate`` also raises an entity-scoped alert on
detection, best-effort, so the ambiguity surfaces in the cockpit from every
caller (doctor's read-only check included), not only the blocking path.
"""

from __future__ import annotations

from pathlib import Path

from src.core.alerts import append_or_suppress_alert
from src.core.exceptions import StateError
from src.core.quality_gates.models import GateEvaluation
from src.core.reality_store import get_program_reality_db_path

GATE_ID = "QG-37"


class StateAuthorityAmbiguousError(Exception):
    """Raised by ``assert_state_authority_or_raise`` when the program's
    canonical fact-store path is ambiguous. Callers should treat this the
    same as any other fail-closed mutation guard (block, surface the
    message, do not retry automatically)."""


def find_stray_fact_store_databases(
    program_id: str,
    *,
    programs_root: Path,
    db_root: Path | None = None,
) -> dict[str, Path]:
    """Same candidate set as ``storage_checks.py``'s stray-database check:
    the home-directory fallback and the programs-root-relative path, both
    reachable by a caller that omits ``db_root``/``programs_root`` before
    the PS-14 root-cause fix. Returns only candidates that exist and are
    NOT the canonical path."""
    canonical_path = get_program_reality_db_path(program_id, db_root=db_root)
    candidate_paths: dict[str, Path] = {
        "home_fallback": Path.home() / ".vertex" / program_id / "vertex.sqlite3",
        "programs_root_relative": programs_root / program_id / "vertex.sqlite3",
    }
    return {
        label: candidate_path
        for label, candidate_path in candidate_paths.items()
        if candidate_path != canonical_path and candidate_path.exists()
    }


def evaluate_state_authority_gate(
    program_id: str,
    *,
    programs_root: Path,
    db_root: Path | None = None,
) -> GateEvaluation:
    stray = find_stray_fact_store_databases(program_id, programs_root=programs_root, db_root=db_root)
    if not stray:
        return GateEvaluation(
            gate_id=GATE_ID,
            passed=True,
            message=f"fact-store path for {program_id!r} is unambiguous.",
            exit_code=0,
        )
    canonical_path = get_program_reality_db_path(program_id, db_root=db_root)
    stray_list = ", ".join(f"{label}={path}" for label, path in sorted(stray.items()))
    reconcile_command = f"python scripts/reconcile_stray_fact_stores.py --program {program_id} --dry-run"
    try:
        append_or_suppress_alert(
            program_id=program_id, category="duplicate_state_path",
            entity_type="fact_store", entity_id=program_id, severity="error",
            message=f"{len(stray)} stray fact-store database(s) besides the canonical path ({canonical_path}).",
            next_command=reconcile_command, programs_root=programs_root,
        )
    except (OSError, StateError):
        pass
    return GateEvaluation(
        gate_id=GATE_ID,
        passed=False,
        message=(
            f"ambiguous fact-store authority for {program_id!r}: {len(stray)} stray database(s) besides "
            f"the canonical path ({canonical_path}): {stray_list}. Mutating commands cannot safely proceed. "
            f"Run: {reconcile_command}"
        ),
        exit_code=1,
        forceable=False,
    )


def assert_state_authority_or_raise(
    program_id: str,
    *,
    programs_root: Path,
    db_root: Path | None = None,
) -> None:
    """The mutation-blocking half of QG-37 -- not yet wired into any live
    command's call path (see module docstring). Raises
    ``StateAuthorityAmbiguousError`` when the fact-store path is ambiguous;
    returns silently otherwise."""
    evaluation = evaluate_state_authority_gate(program_id, programs_root=programs_root, db_root=db_root)
    if not evaluation.passed:
        raise StateAuthorityAmbiguousError(evaluation.message)


__all__ = [
    "GATE_ID",
    "StateAuthorityAmbiguousError",
    "assert_state_authority_or_raise",
    "evaluate_state_authority_gate",
    "find_stray_fact_store_databases",
]
