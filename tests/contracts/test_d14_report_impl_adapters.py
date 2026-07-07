"""Phase 7 D-14 closure: codify the `_impl` dependency-injection adapter pattern.

rev. 323 closed 89/112 of report.py's `_impl` re-exports as redundant (pure
`X = X_impl` rebinds with no behavioral change). The remaining ~24 aliases
follow a deliberate dependency-injection adapter pattern:

    from src.commands.report_ai import _load_eta_forecasts as _load_eta_forecasts_impl

    def _load_eta_forecasts(*, edition_name, items, as_of, reports_root):
        return _load_eta_forecasts_impl(
            edition_name=edition_name, items=items, as_of=as_of,
            reports_root=reports_root,
            item_trajectory_points=_item_trajectory_points,  # <-- DI injection
        )

The wrapper exists so the extracted submodule (`report_ai`, `report_continuity`,
`report_detail`, etc.) can be tested in isolation without importing `report.py`
(circular-import avoidance). The wrapper injects report.py-local helpers
(`_item_trajectory_points`, `_load_report_signal_context`, etc.) into the
submodule's call, so the submodule never has to reach back into the
orchestrator for its dependencies.

This test freezes the design so future refactors cannot accidentally
collapse an intentional DI adapter back into a redundant re-export
(silent drift toward coupling), and so any new `_impl` import is required
to ship with a corresponding wrapper that injects ≥1 module-local helper
(bare name, computed value, or lambda).

Closure history:
- rev. 323 collapsed 89 redundant `X = X_impl` rebinds to direct imports.
- rev. 329 (this contract) collapses 3 more 1:1-pass-through wrapper
  functions to direct imports (`_ensure_review_status`,
  `_build_continuity_deltas`, `_build_detail_workstream_data`); their
  `_impl` aliases were vestigial because the wrappers had no DI. Test
  list below reflects the post-collapse steady state.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
# WI-6.2: DI adapter wrappers extracted from report.py to assemble_stage.py.
REPORT_PY = REPO_ROOT / "src/commands/report_pipeline/assemble_stage.py"

# Names that are imported as `_xxx_impl` (the dependency-injection adapter
# surface). rev. 323 + 329 deliberately retained these — they are NOT
# redundant re-exports because their wrappers do real DI work (inject
# report.py-local helpers — bare names, computed values, or lambdas —
# into the extracted submodule). This constant is the source of truth
# for the wrapper-function shape test below; if a new `_impl` is added
# here, the test will require the wrapper to inject ≥1 module-local
# value.
#
# Closure history: 24 names (rev. 323) → 21 names (rev. 329). The 3 names
# dropped this rev (`_ensure_review_status`, `_build_continuity_deltas`,
# `_build_detail_workstream_data`) had 1:1-pass-through wrappers with no
# DI, so they were collapsed to direct imports.
EXPECTED_DI_ADAPTERS: frozenset[str] = frozenset({
    "_build_newsletter_narrative_covered_item_ids",
    "_build_newsletter_scoped_items",
    "_iter_ai_generated_sections",
    "_load_draft_ai_context",
    "_load_eta_forecasts",
    "_load_guarded_review_evidence",
    "_load_report_signal_context",
    "_synthesize_v2_ai_content",
    "_write_report_adaptive_cards",
    "_apply_scorecard_trend_annotation",
    "_build_scorecard_data",
    "_skipped_review_sections",
    "_build_workstream_templates",
    "_iter_detail_sections",
    "_visible_detail_section_ids",
    "_build_continuity_exec_summary_template",
    "_build_continuity_render_data",
    "_build_continuity_workstream_data",
    "_build_exec_summary_severe_signal_seeds",
    "_load_live_work_items",
})


def _parse_report_module() -> ast.Module:
    source = REPORT_PY.read_text(encoding="utf-8")
    return ast.parse(source, filename=str(REPORT_PY))


def _impl_imports(tree: ast.Module) -> dict[str, ast.stmt]:
    """Return {public_name: import_stmt} for every `as _xxx_impl` import."""
    found: dict[str, ast.stmt] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for alias in node.names:
            if alias.asname and alias.asname.endswith("_impl"):
                # Map `_foo_impl` back to its public name `_foo`
                public_name = alias.asname[: -len("_impl")]
                found[public_name] = node
    return found


def _module_top_level_names(tree: ast.Module) -> set[str]:
    """Return the set of all top-level binding names (defs, assigns, imports).

    For DI-adapter purposes a "module-local helper" is any name bound at
    report.py's top level — whether defined inline (`def _foo(...)`) or
    imported (`from src.commands.report_ai import _item_trajectory_points`).
    Either form is a legitimate injection target.
    """
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                # `from X import a as b` → b is bound; else a is bound
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _wrapper_for(tree: ast.Module, public_name: str) -> ast.FunctionDef | None:
    """Return the top-level `def <public_name>(...)` wrapper, if any."""
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == public_name:
            return node
    return None


def _wrapper_params(wrapper: ast.FunctionDef) -> set[str]:
    """Return the set of parameter names accepted by the wrapper."""
    params: set[str] = set()
    args = wrapper.args
    for arg in args.args:
        params.add(arg.arg)
    for arg in args.kwonlyargs:
        params.add(arg.arg)
    if args.vararg:
        params.add(args.vararg.arg)
    if args.kwarg:
        params.add(args.kwarg.arg)
    return params


def _wrapper_injects_local_kwarg(
    wrapper: ast.FunctionDef, top_level_names: set[str], wrapper_params: set[str]
) -> tuple[bool, str]:
    """Return (ok, detail). The wrapper must call the _impl with at least one
    kwarg whose value is either (a) a top-level name that is NOT one of the
    wrapper's own parameters, or (b) a Call expression (computed value). The
    intent is to detect a 1:1 pass-through: if the wrapper takes N kwargs and
    passes all of them as bare names to _impl with no additional kwargs, the
    wrapper is redundant and should be collapsed to a direct import.
    """
    for call in ast.walk(wrapper):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if not isinstance(func, ast.Name) or not func.id.endswith("_impl"):
            continue
        # Count injected kwargs (kwarg values that are not bare references to
        # the wrapper's own parameters).
        for kw in call.keywords:
            value = kw.value
            if isinstance(value, ast.Name):
                if value.id in wrapper_params:
                    # Pure pass-through, not injection.
                    continue
                if value.id in top_level_names:
                    return True, f"bare-name injection of {value.id!r}"
            elif isinstance(value, ast.Call):
                # Computed value (e.g., `_build_item_urls(bundle, report.items)`)
                # is also a form of DI — the wrapper is doing real work to
                # derive the value.
                if isinstance(value.func, ast.Name) and value.func.id in top_level_names:
                    return True, f"computed-value injection of {value.func.id}(...)"
            elif isinstance(value, ast.Lambda):
                # Lambda injection (used for callbacks).
                return True, "lambda injection"
        # Positional injection (arg with computed value or bare top-level name).
        for arg in call.args:
            value = arg.value
            if isinstance(value, ast.Name):
                if value.id in wrapper_params:
                    continue
                if value.id in top_level_names:
                    return True, f"positional bare-name injection of {value.id!r}"
            elif isinstance(value, ast.Call):
                if isinstance(value.func, ast.Name) and value.func.id in top_level_names:
                    return True, f"positional computed-value injection of {value.func.id}(...)"
            elif isinstance(value, ast.Lambda):
                return True, "positional lambda injection"
    return False, "no kwarg/arg injects a top-level name (bare, computed, or lambda) that isn't a wrapper parameter"


def test_report_py_imports_only_expected_di_adapters() -> None:
    """The set of `_xxx_impl` imports in report.py must match the
    documented DI-adapter surface. Any new `_impl` import is a coupling
    smell that must either land here (and ship with a wrapper) or be
    eliminated as a redundant re-export (the rev. 323 path)."""
    tree = _parse_report_module()
    imported = set(_impl_imports(tree).keys())
    extra = imported - EXPECTED_DI_ADAPTERS
    missing = EXPECTED_DI_ADAPTERS - imported
    assert not extra, (
        f"D-14: unexpected _impl imports in report.py: {sorted(extra)}. "
        f"Either add them to EXPECTED_DI_ADAPTERS (with a DI-injecting wrapper) "
        f"or collapse them to direct imports (the rev. 323 redundant-re-export path)."
    )
    assert not missing, (
        f"D-14: expected DI adapters missing from report.py: {sorted(missing)}. "
        f"If they were genuinely redundant and collapsed, update EXPECTED_DI_ADAPTERS."
    )


@pytest.mark.parametrize("public_name", sorted(EXPECTED_DI_ADAPTERS))
def test_di_adapter_wraps_with_local_kwarg_injection(public_name: str) -> None:
    """Every retained `_impl` import must be wrapped in a same-named public
    function that injects ≥1 module-local symbol as a kwarg. This is the
    shape that makes the wrapper a legitimate DI adapter (vs a redundant
    re-export that could be collapsed)."""
    tree = _parse_report_module()
    top_level_names = _module_top_level_names(tree)
    wrapper = _wrapper_for(tree, public_name)
    assert wrapper is not None, (
        f"D-14: {public_name!r} is imported as _impl but has no same-named "
        f"top-level wrapper in report.py. Either add the wrapper (the "
        f"intended DI-adapter pattern) or remove the _impl import."
    )
    wrapper_params = _wrapper_params(wrapper)
    ok, detail = _wrapper_injects_local_kwarg(wrapper, top_level_names, wrapper_params)
    assert ok, (
        f"D-14: {public_name!r} wrapper does not inject a top-level helper "
        f"as a kwarg ({detail}). The whole point of the _impl adapter is to "
        f"inject report.py-local state into the extracted submodule; if you "
        f"don't need DI, collapse the import to a direct one (rev. 323 path)."
    )


def test_report_py_does_not_redeclare_redundant_impl_aliases() -> None:
    """No `X = X_impl` rebind lines at module level. rev. 323 removed 89
    such redundant rebinds; this test prevents regression."""
    tree = _parse_report_module()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name):
            continue
        value = node.value
        if not isinstance(value, ast.Name):
            continue
        if not target.id.endswith("_impl"):
            continue
        if not value.id.endswith("_impl"):
            continue
        pytest.fail(
            f"D-14: redundant `_impl` rebind at module level: "
            f"{target.id} = {value.id}. Collapse to direct import."
        )


def test_no_legacy_branding_in_di_adapter_names() -> None:
    """Sanity guard: the DI-adapter surface itself must not carry Acme
    coupling. If a future refactor renames a wrapper to e.g. `_nova_*_impl`,
    the contract should fail so the coupling is visible."""
    for name in EXPECTED_DI_ADAPTERS:
        assert "acme" not in name.lower(), (
            f"D-14: DI adapter {name!r} carries Acme branding. Adapters are "
            f"program-neutral by design; rename to remove the coupling."
        )
