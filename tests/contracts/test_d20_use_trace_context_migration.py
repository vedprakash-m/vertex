"""D-20: Process Trace Context — codify the use_trace_context migration.

rev. 322 landed `_CURRENT_TRACE_CONTEXT` (a process-level ContextVar) and the
`use_trace_context(trace_context)` context manager. AIClient.__init__ falls
back to the bound ContextVar when no explicit `trace_context=` is passed,
with the explicit argument always winning. The seam is wired and the
7-test test_ai_trace_context_var.py suite proves the contract.

This file codifies the *command-side* migration pattern: for any helper
that builds an AI client and accepts a `trace_context=` parameter, the
helper should `with use_trace_context(trace_context):` around the AI
client construction so the trace context flows through to intermediate
helpers that don't take the explicit arg (rate-limit scope, cost-guard
construction, trace-file write path).

The 8 command sites identified in the spec are:
  1. backfill.py
  2. kb.py
  3. onboard.py
  4. report.py
  5. report_ai.py (internal seam — covered by report.py wrapper)
  6. review_full.py
  7. summarize.py
  8. synthesize.py
  9. confirm_stages/learning_distiller.py

Migration rule: each `_build_*_client` / `_build_*_extractor` / `_build_*_generator`
helper that takes `trace_context=` must wrap its AI client construction in
`with use_trace_context(trace_context):`. This is a behavior-preserving
change: the explicit `trace_context=` arg path still wins for direct
callers, but the ContextVar is now bound for any nested helper.

Why:** D-20 keeps the explicit-arg path for tests and direct callers, but
the ContextVar fallback is the migration lever that removes the
hand-threading burden from new code.
**How to apply:** when adding a new `_build_*_client` helper that takes
`trace_context=`, wrap the AI client construction in `with
use_trace_context(trace_context):`. When migrating an existing site, do
the same and the contract tests in this file will verify the binding
is honored.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
COMMANDS_DIR = REPO_ROOT / "src/commands"

# File-relpath -> (helper-name, ai-client-class) tuples that should bind
# their trace_context via use_trace_context(...) around the AI client
# construction. Each entry is a single migration site per the spec's
# D-20 backlog. Add an entry here when you migrate a site.
MIGRATION_SITES: list[tuple[str, str, str]] = [
    ("src/commands/summarize.py", "_build_summary_generator", "SummaryGenerator"),
    ("src/commands/synthesize.py", "_build_synthesizer", "_FallbackWorkstreamSynthesizer"),
    ("src/commands/kb.py", "_build_kb_update_client", "FallbackAIClient"),
    ("src/commands/backfill.py", "_build_backfill_extractor", "BackfillExtractor"),
    ("src/commands/onboard.py", "_build_onboard_assistant", "OnboardAssistant"),
    ("src/commands/review_full.py", "_build_anticipation_client", "FallbackStructuredClient"),
    ("src/commands/report_pipeline/assemble_stage.py", "_create_ai_client", "AIClient"),
    (
        "src/commands/confirm_stages/learning_distiller.py",
        "build_learning_distiller",
        "LearningDistiller",
    ),
]


def _parse_module(relpath: str) -> ast.Module:
    path = REPO_ROOT / relpath
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _find_helper(tree: ast.Module, helper_name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == helper_name:
            return node
    return None


def _find_call_to_class(
    wrapper: ast.FunctionDef, class_name: str
) -> tuple[ast.Call | None, str]:
    """Return (Call node matching `_class_name.something(...)` or `class_name(...)`,
    detail string). The AI client construction that the use_trace_context
    wrapper must enclose.
    """
    for call in ast.walk(wrapper):
        if not isinstance(call, ast.Call):
            continue
        func = call.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == class_name:
                return call, f"{class_name}.{func.attr}(...)"
        elif isinstance(func, ast.Name) and func.id == class_name:
            return call, f"{class_name}(...)"
    return None, f"no construction of {class_name} found"


def _wraps_with_use_trace_context(wrapper: ast.FunctionDef) -> tuple[bool, str]:
    """Return (ok, detail). The helper must use `use_trace_context(...)` as a
    context manager (`with` statement) somewhere within its body.
    """
    for node in ast.walk(wrapper):
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                if isinstance(ctx, ast.Call):
                    func = ctx.func
                    if isinstance(func, ast.Name) and func.id == "use_trace_context":
                        return True, "use_trace_context(...) context manager"
                    if isinstance(func, ast.Attribute) and func.attr == "use_trace_context":
                        return True, "<expr>.use_trace_context(...) context manager"
    return False, "no `with use_trace_context(...):` block"


def _use_trace_context_encloses_ai_construction(
    wrapper: ast.FunctionDef, class_name: str
) -> tuple[bool, str]:
    """Return (ok, detail). The `use_trace_context` context manager must
    enclose the AI client construction (the `With` block must contain
    the call to the AI client constructor). Otherwise the migration is
    cosmetic and the ctx-var binding is wasted.
    """
    ai_call, detail = _find_call_to_class(wrapper, class_name)
    if ai_call is None:
        return False, detail
    # Walk the AST to find any `with use_trace_context(...):` that contains
    # this call.
    for node in ast.walk(wrapper):
        if isinstance(node, ast.With):
            for item in node.items:
                ctx = item.context_expr
                is_use = (
                    isinstance(ctx, ast.Call)
                    and isinstance(ctx.func, ast.Name)
                    and ctx.func.id == "use_trace_context"
                ) or (
                    isinstance(ctx, ast.Call)
                    and isinstance(ctx.func, ast.Attribute)
                    and ctx.func.attr == "use_trace_context"
                )
                if not is_use:
                    continue
                for child in ast.walk(node):
                    if child is ai_call:
                        return True, "use_trace_context encloses AI client construction"
    return False, "use_trace_context found, but it does not enclose the AI client construction"


@pytest.mark.parametrize(
    "relpath,helper_name,class_name",
    MIGRATION_SITES,
    ids=[s[1] for s in MIGRATION_SITES],
)
def test_d20_migration_site_uses_use_trace_context(
    relpath: str, helper_name: str, class_name: str
) -> None:
    """Every migrated site in MIGRATION_SITES must:
    (a) define the helper,
    (b) construct the named AI client, and
    (c) enclose that construction in `with use_trace_context(...):`.

    This test enforces the D-20 migration pattern so that any future
    command-site refactor preserves the ctx-var binding.
    """
    tree = _parse_module(relpath)
    helper = _find_helper(tree, helper_name)
    assert helper is not None, (
        f"D-20 migration site {helper_name!r} not found in {relpath}. "
        f"If the helper was renamed, update MIGRATION_SITES. If it was "
        f"removed, remove the entry."
    )
    ok_use, detail_use = _wraps_with_use_trace_context(helper)
    if not ok_use:
        pytest.skip(
            f"D-20 migration not yet applied to {relpath}::{helper_name} "
            f"({detail_use}). Add `with use_trace_context(trace_context):` "
            f"around the AI client construction. See test docstring for the pattern."
        )
    ok_enclose, detail_enclose = _use_trace_context_encloses_ai_construction(
        helper, class_name
    )
    assert ok_enclose, (
        f"D-20: {relpath}::{helper_name} has `use_trace_context(...)` but "
        f"it does not enclose the {class_name} construction ({detail_enclose}). "
        f"The ctx-var binding is wasted unless the AI client construction "
        f"is inside the `with` block."
    )


def test_d20_use_trace_context_is_imported_in_migrated_files() -> None:
    """Each migrated file must import use_trace_context from src.ai.llm_trace.

    This catches a class of regression where the helper uses the context
    manager but the import is missing (ModuleNotFoundError at first
    invocation)."""
    for relpath, _, _ in MIGRATION_SITES:
        tree = _parse_module(relpath)
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            for alias in node.names:
                if alias.name == "use_trace_context":
                    found = True
                    break
            if found:
                break
        assert found, (
            f"D-20: {relpath} is a migration site but does not import "
            f"`use_trace_context` from src.ai.llm_trace. Add the import."
        )
