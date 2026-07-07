"""Phase 6 §22 Step 7: codify the ProgramEvent migration for skip-issue.

rev. 331 added the `ProgramEvent` dataclass + `append_program_event`
helper to `src/core/program_fact_store.py`. The skip-issue flow
(`src/commands/skip_issue.py`) now writes events with
`fact_type="event.issue.skip"` instead of the legacy `skip.issue` fact
type. The `project_skip_issues` projection recognizes both fact_types
and dedupes by `(edition_id, issue_number)` so a re-run of
`admin_fact_store_migrate` after rev. 331 doesn't double-count.

This file codifies the migration contract so future refactors can't
silently regress to the legacy fact type or break the dedupe invariant.

Why:** the spec's §22 Step 7 mandates the ProgramEvent shape
(`ProgramEvent(fact_type, natural_key, metadata)`) and the
`event.<noun>.<verb>` fact_type convention. Codifying the contract
ensures any new event type added to the platform follows the same
shape, and that the skip-issue flow's read projection is robust to
the migration window where both fact_types may be present.
**How to apply:** when adding a new event type:
  1. Construct `ProgramEvent(fact_type="event.<noun>.<verb>", ...)`
  2. Write via `append_program_event(program_id, event, ...)`
  3. Read via `load_program_facts(fact_types=(event.fact_type,))`
  4. Project with a dedicated helper (like `_skip_issue_from_fact`)
  5. Update the default fact_types tuple in `load_program_facts` so
     `load_program_facts()` (no arg) sees the new event.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROGRAM_FACT_STORE = REPO_ROOT / "src/core/program_fact_store.py"
SKIP_ISSUE = REPO_ROOT / "src/commands/skip_issue.py"


def _parse(relpath: Path) -> ast.Module:
    return ast.parse(relpath.read_text(encoding="utf-8"), filename=str(relpath))


def _find_class(tree: ast.Module, class_name: str) -> ast.ClassDef | None:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _find_function(tree: ast.Module, func_name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return node
    return None


def test_program_event_dataclass_shape() -> None:
    """`ProgramEvent` must be a frozen dataclass with exactly:
      - fact_type: str
      - natural_key: str
      - metadata: dict[str, Any]

    The spec mandates this shape (§22 Step 7). Any drift breaks the
    event-write helper's contract.
    """
    tree = _parse(PROGRAM_FACT_STORE)
    cls = _find_class(tree, "ProgramEvent")
    assert cls is not None, (
        "Phase 6 §22 Step 7: `ProgramEvent` dataclass not found in "
        "src/core/program_fact_store.py. Add a frozen dataclass with "
        "`fact_type: str`, `natural_key: str`, `metadata: dict[str, Any]`."
    )
    # Must be a frozen dataclass (slots allowed).
    is_frozen = False
    for decorator in cls.decorator_list:
        if isinstance(decorator, ast.Call) and getattr(decorator.func, "id", "") == "dataclass":
            for kw in decorator.keywords:
                if kw.arg == "frozen" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                    is_frozen = True
    assert is_frozen, (
        "Phase 6 §22 Step 7: `ProgramEvent` must be `@dataclass(frozen=True, slots=True)` "
        "so events are immutable log records."
    )
    # Must have the three required fields (collected from AnnAssign nodes).
    field_names: set[str] = set()
    for stmt in cls.body:
        if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            field_names.add(stmt.target.id)
    required = {"fact_type", "natural_key", "metadata"}
    assert required.issubset(field_names), (
        f"Phase 6 §22 Step 7: `ProgramEvent` is missing required fields "
        f"{required - field_names}. Spec mandates `fact_type`, `natural_key`, "
        f"`metadata`."
    )


def test_append_program_event_helper_exists() -> None:
    """`append_program_event(program_id, event, ...)` must exist as a
    module-level function in `program_fact_store.py`. It is the single
    write path for all event types."""
    tree = _parse(PROGRAM_FACT_STORE)
    func = _find_function(tree, "append_program_event")
    assert func is not None, (
        "Phase 6 §22 Step 7: `append_program_event(program_id, event, ...)` "
        "helper not found in src/core/program_fact_store.py. Add a module-"
        "level function that wraps the event in a `ProgramFactInput` and "
        "calls `ProgramFactStore.append_fact`."
    )


def test_skip_issue_uses_append_program_event() -> None:
    """`src/commands/skip_issue.py` must use `append_program_event` with
    `fact_type="event.issue.skip"`. The legacy `append_skip_issue_fact`
    shim is allowed to remain for back-compat ETL but the live flow
    must use the new event-write path."""
    tree = _parse(SKIP_ISSUE)
    found_new_write = False
    found_legacy_write = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "append_program_event":
            found_new_write = True
        if isinstance(func, ast.Name) and func.id == "append_skip_issue_fact":
            found_legacy_write = True
    assert found_new_write, (
        "Phase 6 §22 Step 7: `src/commands/skip_issue.py` does not call "
        "`append_program_event`. The skip-issue flow must migrate to the "
        "new event-write path."
    )
    assert not found_legacy_write, (
        "Phase 6 §22 Step 7: `src/commands/skip_issue.py` still calls "
        "`append_skip_issue_fact` (the legacy shim). Migrate the live "
        "flow to `append_program_event` with `fact_type='event.issue.skip'`."
    )


def test_skip_issue_fact_type_is_event_prefixed() -> None:
    """The skip-issue flow must write `fact_type="event.issue.skip"`
    (the `event.<noun>.<verb>` convention), not the bare `skip.issue`
    fact type or any other shape."""
    tree = _parse(SKIP_ISSUE)
    found_event_issue_skip = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "ProgramEvent"):
            continue
        for kw in node.keywords:
            if kw.arg == "fact_type" and isinstance(kw.value, ast.Constant):
                if kw.value.value == "event.issue.skip":
                    found_event_issue_skip = True
    assert found_event_issue_skip, (
        "Phase 6 §22 Step 7: skip_issue.py constructs a `ProgramEvent` "
        "with a fact_type other than `event.issue.skip`. The spec "
        "mandates the `event.<noun>.<verb>` shape for events."
    )


def test_project_skip_issues_dedupes_by_edition_and_issue() -> None:
    """`project_skip_issues` must dedupe by `(edition_id, issue_number)`
    so the migration window (both `skip.issue` and `event.issue.skip`
    facts may exist for the same skip) doesn't double-count."""
    tree = _parse(PROGRAM_FACT_STORE)
    func = _find_function(tree, "project_skip_issues")
    assert func is not None
    source = ast.unparse(func)
    assert "skip.issue" in source and "event.issue.skip" in source, (
        "Phase 6 §22 Step 7: `project_skip_issues` must recognize BOTH "
        "the legacy `skip.issue` fact type and the new `event.issue.skip` "
        "fact type."
    )
    assert "seen" in source or "dedupe" in source.lower(), (
        "Phase 6 §22 Step 7: `project_skip_issues` must dedupe by "
        "`(edition_id, issue_number)` to prevent double-counting during "
        "the migration window."
    )


def test_event_issue_skip_in_default_fact_types() -> None:
    """`event.issue.skip` must appear in the default fact_types tuple
    used by the dual-read shim inside `load_program_facts` so callers
    using `load_program_facts()` (no arg) see the new event fact type
    without needing to pass it explicitly. The default tuple is built
    inside `_load_current_state_shim_facts` (the function that
    constructs the legacy shim facts)."""
    tree = _parse(PROGRAM_FACT_STORE)
    func = _find_function(tree, "_load_current_state_shim_facts")
    assert func is not None
    source = ast.unparse(func)
    assert "event.issue.skip" in source, (
        "Phase 6 §22 Step 7: `event.issue.skip` is not in the default "
        "fact_types tuple of `_load_current_state_shim_facts`. Add it so "
        "callers using `load_program_facts()` (no arg) see the new event fact."
    )
