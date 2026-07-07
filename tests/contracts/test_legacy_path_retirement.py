"""Contract: governed retirement of transitional dual-paths (D-16 / D-23).

The debt spec repeatedly records "additive migration done, removal deferred". Left
ungoverned, such a dual-path silently accretes new call sites and the legacy path
quietly stays load-bearing forever. This contract ratchets the known dual-paths so
they may only **shrink**, and pins that the authoritative replacement remains the
primary path until removal. See `docs/legacy-retirement-ledger.md`.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COST_GUARD = REPO_ROOT / "src" / "ai" / "cost_guard.py"
# Governance is asserted against the TRACKED canonical spec (vertex-tech-spec.md),
# not the archived debt.md (in .archive/specs/ which is gitignored) or the human-facing
# docs/ ledger — both are gitignored and absent on a fresh clone, which would cause
# this must-run guardrail to fail (the exact D-29 anti-pattern).
DEBT_SPEC = REPO_ROOT / "specs" / "vertex-tech-spec.md"

# Frozen ceiling: the cost-guard JSON projection (D-16) must be written from exactly
# one site (record_actual). This may only ratchet DOWN to 0 at retirement.
_MAX_JSON_PROJECTION_WRITES = 1


def _count_calls(source: str, func_name: str) -> int:
    tree = ast.parse(source)
    count = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name) and target.id == func_name:
                count += 1
            elif isinstance(target, ast.Attribute) and target.attr == func_name:
                count += 1
    return count


def test_cost_guard_json_projection_write_is_frozen_to_one_site() -> None:
    """D-16: the legacy `cost_guard.json` projection must not proliferate. New cost
    state must go through the SQLite ledger, not additional JSON writes."""
    source = COST_GUARD.read_text(encoding="utf-8")
    writes = _count_calls(source, "_write_atomic_json")
    assert writes <= _MAX_JSON_PROJECTION_WRITES, (
        f"D-16: cost_guard.py now writes the legacy JSON projection from {writes} "
        f"sites (ceiling {_MAX_JSON_PROJECTION_WRITES}). The JSON file is a back-compat "
        "projection only — persist new cost state via the SQLite ledger (_write_sqlite_state)."
    )


def test_cost_guard_sqlite_is_authoritative_read() -> None:
    """D-16: `load_run_states` must consult the SQLite ledger before the JSON fallback,
    so SQLite remains the system of record and JSON is only a secondary read."""
    source = COST_GUARD.read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "load_run_states"
        ),
        None,
    )
    assert fn is not None, "load_run_states not found in cost_guard.py"
    fn_src = ast.get_source_segment(source, fn) or ""
    sqlite_at = fn_src.find("_load_sqlite_run_states")
    json_at = fn_src.find("_cost_guard_path")
    assert sqlite_at != -1, "D-16: load_run_states no longer reads the SQLite ledger."
    assert json_at != -1, "D-16: load_run_states no longer has the JSON fallback read."
    assert sqlite_at < json_at, (
        "D-16: load_run_states must read the SQLite ledger BEFORE the JSON fallback; "
        "SQLite is the authoritative system of record."
    )


def test_retirement_governance_is_documented_in_tracked_spec() -> None:
    """The dual-path retirement governance must be recorded in a TRACKED location so
    the contract and the documentation cannot drift apart. We assert against the spec
    (debt.md) rather than the gitignored docs/ ledger so this guard holds on a fresh
    clone. (The human-facing `docs/legacy-retirement-ledger.md` mirrors this.)"""
    assert DEBT_SPEC.is_file(), "specs/vertex-tech-spec.md is missing."
    text = DEBT_SPEC.read_text(encoding="utf-8")
    for token in ("D-16", "D-23", "cost_guard.json", "CONNECTOR_REGISTRY"):
        assert token in text, (
            f"vertex-tech-spec.md must reference {token!r} so the retirement governance stays in "
            "sync with the enforced invariants."
        )
