"""Meta-contract: source-only guardrails actually run on the fresh-clone CI (D-29).

The CI runs on a fresh clone with no private ``editions/`` or ``programs/``
data. Several contract suites historically carried a module-level
``pytestmark = pytest.mark.skipif(not (... / "editions").exists(), ...)`` so that
*data-dependent* tests degrade gracefully. The hazard (D-29): a purely
source/AST guardrail placed in such a module is **silently skipped** on CI and
provides zero protection — exactly the failure mode that let architecture
regressions through.

This meta-test freezes the invariant: the listed source-only guardrail modules
(which scan tracked source or build their own tmp_path fixtures) must never
carry a module-level skip, so they execute on every CI run.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest


_CONTRACTS_DIR = Path(__file__).resolve().parent

# Source-only / self-contained guardrail suites that MUST run on a fresh clone.
# Adding a governance or invariant contract that scans source code (not private
# data) belongs on this list.
_MUST_RUN_ON_FRESH_CLONE = (
    "test_architecture_fitness.py",
    "test_import_boundaries.py",
    "test_state_reader_authority.py",
    "test_ai_router_is_only_path.py",
    "test_ai_feature_policy_wiring.py",
    "test_prompt_registry.py",
    "test_d20_use_trace_context_migration.py",
    "test_ci_guardrail_execution.py",
    "test_phase4_frontier_eligible_enforcement.py",
    "test_phase4_step4_gold_corpus_scaffold.py",
    "test_d30_ai_proposal_ttl_gc.py",
    "test_ai_safety_pipeline.py",
    "test_ai_disabled_write_paths.py",
    "test_tiered_router_contract.py",
    "test_legacy_path_retirement.py",
    "test_capability_promotion.py",
    "test_readiness_gate_matrix.py",
    "test_schema_versions.py",
    "test_ws1_archive_prefly_wiring_contract.py",
)


def _module_level_skip_marks(path: Path) -> list[str]:
    """Return source for any module-level ``pytestmark`` skip/skipif assignment."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[str] = []
    for node in tree.body:  # module level only — not nested in functions/classes
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            continue
        for sub in ast.walk(node.value):
            if isinstance(sub, ast.Attribute) and sub.attr in {"skip", "skipif"}:
                found.append(ast.unparse(node.value))
    return found


@pytest.mark.parametrize("module_name", _MUST_RUN_ON_FRESH_CLONE)
def test_guardrail_module_exists(module_name: str) -> None:
    assert (_CONTRACTS_DIR / module_name).is_file(), (
        f"D-29: guardrail module {module_name} is listed as must-run but is missing "
        "(was it renamed? update _MUST_RUN_ON_FRESH_CLONE)."
    )


@pytest.mark.parametrize("module_name", _MUST_RUN_ON_FRESH_CLONE)
def test_guardrail_module_has_no_module_level_skip(module_name: str) -> None:
    path = _CONTRACTS_DIR / module_name
    skips = _module_level_skip_marks(path)
    assert not skips, (
        f"D-29: {module_name} carries a module-level pytestmark skip {skips} — it would be "
        "silently skipped on the fresh-clone CI and provide zero protection. Source-only "
        "guardrails must run unconditionally; gate only the specific data-dependent tests."
    )
