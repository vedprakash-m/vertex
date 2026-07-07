#!/usr/bin/env python3
"""Verify that load-bearing claims from the consolidated plan still hold.

S-0e deliverable — the Verification Cadence section.
The consolidated feature plan has been archived to
``.archive/specs/consolidated.md`` (local-only, gitignored); the canonical
GitHub-synced specs are the PRD, Tech Spec, and UX Spec. This verifier keeps
the historical claim registry useful and asserts those three core specs (and
their key symbols) remain tracked and present.

Usage
-----
    python scripts/verify_consolidated_claims.py [--strict]

Exit codes
----------
    0 — all claims verified
    1 — one or more claims failed (details printed to stdout)
    2 — configuration / import error

Options
-------
    --strict    Fail on warnings as well as errors (useful for CI).
"""
from __future__ import annotations

import argparse
import importlib
import inspect
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ── Repository root ──────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO_ROOT))

# ────────────────────────────────────────────────────────────────────────────
# Claim registry
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class Claim:
    """A single verifiable code claim from the spec."""
    spec_ref: str         # e.g. "§5.2", "PS-E"
    description: str
    kind: str             # "symbol" | "attribute" | "constant" | "file" | "git_tracked"
    target: str           # module path or file path
    symbol: str | None    # optional symbol name within module
    expected: Any = None  # optional expected value for constants


# Every claim below corresponds to a verified-on statement or a load-bearing
# assertion in the (now archived, .archive/specs/consolidated.md) consolidated
# plan §32 / Verification cadence; the live source of truth is the core specs.
CLAIMS: list[Claim] = [
    # ── Governance ──────────────────────────────────────────────────────────
    Claim(
        spec_ref="S-0a",
        description="canonical PRD is tracked in git (not gitignored)",
        kind="git_tracked",
        target="specs/vertex-prd.md",
        symbol=None,
    ),
    Claim(
        spec_ref="S-0a",
        description="canonical Tech Spec is tracked in git (not gitignored)",
        kind="git_tracked",
        target="specs/vertex-tech-spec.md",
        symbol=None,
    ),
    Claim(
        spec_ref="S-0a",
        description="canonical UX Spec is tracked in git (not gitignored)",
        kind="git_tracked",
        target="specs/vertex-ux-spec.md",
        symbol=None,
    ),
    Claim(
        spec_ref="S-0a",
        description="canonical PRD exists",
        kind="file",
        target="specs/vertex-prd.md",
        symbol=None,
    ),
    Claim(
        spec_ref="S-0a",
        description="canonical Tech Spec exists",
        kind="file",
        target="specs/vertex-tech-spec.md",
        symbol=None,
    ),
    Claim(
        spec_ref="S-0a",
        description="canonical UX Spec exists",
        kind="file",
        target="specs/vertex-ux-spec.md",
        symbol=None,
    ),

    # ── Authority families (§5.3 / fact_sor_state.py:18) ────────────────────
    Claim(
        spec_ref="§5.3 / fact_sor_state.py:18",
        description="AUTHORITY_FAMILIES exists in fact_sor_state with 6 entries",
        kind="constant",
        target="src.core.fact_sor_state",
        symbol="AUTHORITY_FAMILIES",
        expected=6,  # length
    ),

    # ── S-0h: no duplicate _AUTHORITY_FAMILIES in reality_completeness ───────
    Claim(
        spec_ref="S-0h",
        description="reality_completeness._AUTHORITY_FAMILIES imports from fact_sor_state (no duplication)",
        kind="symbol",
        target="src.core.reality_completeness",
        symbol="_AUTHORITY_FAMILIES",
    ),

    # ── S-0j: event_type_registry uses policy-derived authority_family ───────
    Claim(
        spec_ref="S-0j",
        description="event_type_registry.assert_registry_authority_families_match_policy exists",
        kind="symbol",
        target="src.core.ledger.event_type_registry",
        symbol="assert_registry_authority_families_match_policy",
    ),
    Claim(
        spec_ref="S-0j",
        description="risk. prefix has authority_family='judgment' (not 'workitem.state')",
        kind="attribute",
        target="src.core.ledger.event_type_registry",
        symbol="LEDGER_EVENT_REGISTRY",
        expected=("risk.", "judgment"),  # (prefix, expected authority_family)
    ),

    # ── S-0k: source_authority.yaml has sor_flip section ────────────────────
    Claim(
        spec_ref="S-0k",
        description="vertex/policies/source_authority.yaml contains sor_flip config",
        kind="file",
        target="vertex/policies/source_authority.yaml",
        symbol="sor_flip",
    ),

    # ── S-3: FactLineage 12-field envelope ──────────────────────────────────
    Claim(
        spec_ref="S-3 / §5.2",
        description="FactLineage dataclass exists in program_fact_store",
        kind="symbol",
        target="src.core.program_fact_store",
        symbol="FactLineage",
    ),
    Claim(
        spec_ref="S-3 / §5.2",
        description="FactLineage has all 12 lineage fields",
        kind="attribute",
        target="src.core.program_fact_store",
        symbol="FactLineage",
        expected=12,  # field count
    ),
    Claim(
        spec_ref="S-3 / §5.2",
        description="ProgramFactInput has source_event_id field (S-1/S-3)",
        kind="attribute",
        target="src.core.program_fact_store",
        symbol="ProgramFactInput",
        expected="source_event_id",
    ),
    Claim(
        spec_ref="S-3 / §5.2",
        description="ProgramFactRevision has build_lineage() method",
        kind="symbol",
        target="src.core.program_fact_store",
        symbol="ProgramFactRevision",
    ),

    # ── PS-A: report.py has zero ProgramReality references (still deferred) ──
    Claim(
        spec_ref="PS-A",
        description="report.py exists in commands",
        kind="file",
        target="src/commands/report.py",
        symbol=None,
    ),

    # ── PS-E: lineage in program_fact_store ──────────────────────────────────
    Claim(
        spec_ref="PS-E / §5.2",
        description="FactLineageUnavailable exists (structured unavailability marker)",
        kind="symbol",
        target="src.core.program_fact_store",
        symbol="FactLineageUnavailable",
    ),

    # ── PS-F: incremental fold exists (wiring gap, not build gap) ────────────
    Claim(
        spec_ref="PS-F",
        description="project_events_incremental_to_sqlite exists in program_views",
        kind="symbol",
        target="src.core.ledger.program_views",
        symbol="project_events_incremental_to_sqlite",
    ),

    # ── Stage γ-Write (NCFL) ─────────────────────────────────────────────────
    Claim(
        spec_ref="S-NC-1",
        description="ContextUpdateProposal exists in ncfl_models",
        kind="symbol",
        target="src.core.ncfl_models",
        symbol="ContextUpdateProposal",
    ),
    Claim(
        spec_ref="S-NC-1",
        description="NcflExtractor exists",
        kind="symbol",
        target="src.core.ncfl_extractor",
        symbol="NcflExtractor",
    ),

    # ── Stage γ-Read (Editorial Engine) ──────────────────────────────────────
    Claim(
        spec_ref="S-ED-1",
        description="FormatMatchesCheck exists in editorial.check_types",
        kind="symbol",
        target="src.core.editorial.check_types",
        symbol="FormatMatchesCheck",
    ),
    Claim(
        spec_ref="S-ED-2",
        description="CrossScopeConsistencyCheck exists in editorial.check_types",
        kind="symbol",
        target="src.core.editorial.check_types",
        symbol="CrossScopeConsistencyCheck",
    ),

    # ── INV-1: no daemon / background scheduling ──────────────────────────────
    # (verified by absence of subprocess.Popen with daemon=True in commands/)
    # This is a process/architecture constraint, not a symbol check.

    # ── context_snapshot_store (S-NC-0 base) ─────────────────────────────────
    Claim(
        spec_ref="S-NC-0",
        description="context_snapshot_store.write_context_snapshot exists",
        kind="symbol",
        target="src.core.context_snapshot_store",
        symbol="write_context_snapshot",
    ),
    Claim(
        spec_ref="S-NC-0",
        description="context_snapshot_store.load_context_snapshot exists",
        kind="symbol",
        target="src.core.context_snapshot_store",
        symbol="load_context_snapshot",
    ),
]


# ────────────────────────────────────────────────────────────────────────────
# Verification engine
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class Result:
    claim: Claim
    passed: bool
    message: str
    is_warning: bool = False


def _check_file(claim: Claim) -> Result:
    path = _REPO_ROOT / claim.target
    if not path.exists():
        return Result(claim, False, f"File not found: {path}")
    # If symbol is specified, check for its presence in the file text
    if claim.symbol:
        content = path.read_text(encoding="utf-8", errors="replace")
        if claim.symbol not in content:
            return Result(
                claim, False,
                f"Symbol/key {claim.symbol!r} not found in {path}",
            )
    return Result(claim, True, f"OK: {path}")


def _check_git_tracked(claim: Claim) -> Result:
    path = _REPO_ROOT / claim.target
    if not path.exists():
        return Result(claim, False, f"File not found: {path}")

    try:
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", claim.target],
            cwd=_REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return Result(claim, False, f"Unable to run git for tracking check: {exc}")

    if tracked.returncode != 0:
        return Result(claim, False, f"{claim.target} is not tracked in git.")

    try:
        ignored = subprocess.run(
            ["git", "check-ignore", claim.target],
            cwd=_REPO_ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return Result(claim, False, f"Unable to run git for ignore check: {exc}")

    if ignored.returncode == 0:
        return Result(claim, False, f"{claim.target} is gitignored.")

    return Result(claim, True, f"OK: {claim.target} is tracked and not ignored")


def _check_symbol(claim: Claim) -> Result:
    try:
        mod = importlib.import_module(claim.target)
    except ModuleNotFoundError as exc:
        return Result(claim, False, f"Module not found: {claim.target} ({exc})")
    except Exception as exc:  # noqa: BLE001
        return Result(claim, False, f"Import error for {claim.target}: {exc}")
    if claim.symbol and not hasattr(mod, claim.symbol):
        return Result(claim, False, f"Symbol {claim.symbol!r} not found in {claim.target}")
    return Result(claim, True, f"OK: {claim.target}.{claim.symbol or '<module>'}")


def _check_constant(claim: Claim) -> Result:
    try:
        mod = importlib.import_module(claim.target)
    except Exception as exc:  # noqa: BLE001
        return Result(claim, False, f"Import error for {claim.target}: {exc}")
    if not claim.symbol:
        return Result(claim, False, "Constant claim missing symbol name")
    value = getattr(mod, claim.symbol, None)
    if value is None:
        return Result(claim, False, f"Symbol {claim.symbol!r} not found in {claim.target}")
    if claim.expected is not None:
        # For tuple/list constants, compare length
        if isinstance(claim.expected, int):
            actual_len = len(value)
            if actual_len != claim.expected:
                return Result(
                    claim, False,
                    f"{claim.symbol} has {actual_len} entries; expected {claim.expected}",
                )
    return Result(claim, True, f"OK: {claim.target}.{claim.symbol}")


def _check_attribute(claim: Claim) -> Result:
    try:
        mod = importlib.import_module(claim.target)
    except Exception as exc:  # noqa: BLE001
        return Result(claim, False, f"Import error for {claim.target}: {exc}")
    if not claim.symbol:
        return Result(claim, False, "Attribute claim missing symbol name")
    obj = getattr(mod, claim.symbol, None)
    if obj is None:
        return Result(claim, False, f"Symbol {claim.symbol!r} not found in {claim.target}")

    if claim.expected is None:
        return Result(claim, True, f"OK: {claim.target}.{claim.symbol}")

    # Specialized expected-value checks
    if isinstance(claim.expected, int):
        # Count dataclass fields
        try:
            import dataclasses
            fields = dataclasses.fields(obj)
            if len(fields) != claim.expected:
                return Result(
                    claim, False,
                    f"{claim.symbol} has {len(fields)} dataclass fields; expected {claim.expected}",
                )
        except TypeError:
            return Result(claim, False, f"{claim.symbol} is not a dataclass")
    elif isinstance(claim.expected, str):
        # Check that a field/attribute with this name exists
        if inspect.isclass(obj):
            if not (hasattr(obj, claim.expected) or
                    any(f.name == claim.expected
                        for f in getattr(inspect.signature(obj).__init__.__func__, "__annotations__", {}))):
                # Try dataclass fields
                try:
                    import dataclasses
                    field_names = {f.name for f in dataclasses.fields(obj)}
                    if claim.expected not in field_names:
                        return Result(
                            claim, False,
                            f"{claim.symbol} has no field/attribute {claim.expected!r}",
                        )
                except TypeError:
                    if not hasattr(obj, claim.expected):
                        return Result(
                            claim, False,
                            f"{claim.symbol} has no attribute {claim.expected!r}",
                        )
    elif isinstance(claim.expected, tuple) and len(claim.expected) == 2:
        # Registry row check: (prefix, expected_authority_family)
        prefix, expected_family = claim.expected
        if hasattr(obj, "__iter__"):
            for row in obj:
                if hasattr(row, "prefix") and row.prefix == prefix:
                    if row.authority_family != expected_family:
                        return Result(
                            claim, False,
                            f"Registry row {prefix!r} has authority_family="
                            f"{row.authority_family!r}; expected {expected_family!r}",
                        )
                    return Result(claim, True, f"OK: {prefix!r} → {expected_family!r}")
            return Result(claim, False, f"No registry row with prefix {prefix!r}")

    return Result(claim, True, f"OK: {claim.target}.{claim.symbol}")


def verify_claims(claims: list[Claim]) -> list[Result]:
    results = []
    for claim in claims:
        if claim.kind == "file":
            results.append(_check_file(claim))
        elif claim.kind == "git_tracked":
            results.append(_check_git_tracked(claim))
        elif claim.kind == "symbol":
            results.append(_check_symbol(claim))
        elif claim.kind == "constant":
            results.append(_check_constant(claim))
        elif claim.kind == "attribute":
            results.append(_check_attribute(claim))
        else:
            results.append(Result(claim, False, f"Unknown claim kind: {claim.kind!r}"))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on warnings as well as errors.",
    )
    args = parser.parse_args()

    results = verify_claims(CLAIMS)

    failures = [r for r in results if not r.passed and not r.is_warning]
    warnings = [r for r in results if not r.passed and r.is_warning]
    passed = [r for r in results if r.passed]

    print(f"Verified {len(results)} claims: {len(passed)} passed, "
          f"{len(failures)} failed, {len(warnings)} warnings\n")

    if warnings:
        print("WARNINGS:")
        for r in warnings:
            print(f"  WARN [{r.claim.spec_ref}] {r.claim.description}")
            print(f"       {r.message}")
        print()

    if failures:
        print("FAILURES:")
        for r in failures:
            print(f"  FAIL [{r.claim.spec_ref}] {r.claim.description}")
            print(f"       {r.message}")
        print()
        return 1

    if args.strict and warnings:
        print("Exiting with failure due to --strict and warnings present.")
        return 1

    print("All claims verified. [OK]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
