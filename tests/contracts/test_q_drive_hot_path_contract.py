"""WS-13 PB-40 — Q: drive hot-path contract.

Background: the Q: mapped network drive is slow when accessed under
concurrent load. The store factory exposes two variants:

  * `build_program_signal_store(program, ...)`  — uses an in-memory
    `Program` object, no Q: read.
  * `build_signal_store_for_program_id(program_id, ...)`  — re-reads the
    program from disk (Q:) every time it's called.

The anti-pattern is calling the `_for_program_id` variant inside a
**loop that runs many times per command** (per-item render loops, per-signal
loops, etc.). Each call hits the Q: drive and contributes to the hang.

This contract enumerates every call to a `_for_program_id` variant in
``src/commands/`` and verifies:

  1. The call is NOT inside a `for` / `while` loop (i.e. it's a single
     one-shot per command); OR
  2. The call site carries a ``# noqa: PB40`` annotation explaining why
     a Q: hit is acceptable at this site (e.g. the loop iterates ≤5
     times).

We exclude:
  * ``src/core/store_factory.py`` (the canonical seam).
  * Test code (``tests/``).

The ratchet baseline starts at the current count and can be reduced as
calls are migrated to the in-memory ``build_program_*`` variants.
"""
from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
COMMANDS_ROOT = REPO_ROOT / "src" / "commands"


# Variants of the store-factory API that re-read from disk.
_FOR_PROGRAM_ID_FUNCS: frozenset[str] = frozenset(
    {
        "build_signal_store_for_program_id",
        "build_trajectory_store_for_program_id",
        "read_signal_review_log_for_program_id",
        "read_signal_store_for_program_id",
        "read_trajectory_store_for_program_id",
    }
)


def _iter_for_program_id_calls() -> list[tuple[Path, int, str]]:
    """Yield ``(file, lineno, function_name)`` for every call to a
    ``*_for_program_id`` function in ``src/commands/``.
    """
    results: list[tuple[Path, int, str]] = []
    for py_file in sorted(COMMANDS_ROOT.rglob("*.py")):
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=str(py_file.relative_to(REPO_ROOT)))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_for_program_id_call(node):
                continue
            func_name = _called_name(node)
            if func_name is None:
                continue
            results.append((py_file, node.lineno, func_name))
    return results


def _is_for_program_id_call(node: ast.Call) -> bool:
    """Return True if ``node`` is a call to a `_for_program_id` factory
    function (free function or method).
    """
    func_name = _called_name(node)
    if func_name is None:
        return False
    return func_name in _FOR_PROGRAM_ID_FUNCS


def _called_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _build_parents(tree: ast.AST) -> dict[int, ast.AST]:
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _enclosing_loop_lineno(tree: ast.AST, target: ast.Call) -> tuple[int, bool] | None:
    """Walk up from ``target`` and find the first enclosing ``for`` /
    ``while`` loop.

    Returns a ``(loop_lineno, is_iterable)`` tuple where ``is_iterable`` is
    True if the target is the **iterable expression** of an outer
    ``for`` (i.e. ``for x in <target>(): ...``). When ``is_iterable`` is
    True the call is itself a single one-shot per outer-loop iteration,
    not a per-body-call Q: hit — PB-40 only fires when the call lives in
    the *body* of the loop, not when it IS the iterable.

    Returns None if no enclosing loop exists.
    """
    parents = _build_parents(tree)
    cur: ast.AST | None = target
    while cur is not None:
        parent = parents.get(id(cur))
        if parent is None:
            return None
        if isinstance(parent, ast.For) and parent.iter is target:
            # The call IS the iterable of the for-loop, not a body call.
            return (parent.lineno, True)
        if isinstance(parent, (ast.For, ast.AsyncFor, ast.While)):
            return (parent.lineno, False)
        cur = parent
    return None


def _is_within_noqa_block(source: str, lineno: int) -> bool:
    """True if any line within ±3 of ``lineno`` carries a `# noqa: PB40`
    annotation. Loop annotations are checked at the LOOP's lineno (not
    the call's) — see ``test_no_q_drive_calls_inside_loops``.
    """
    lines = source.splitlines()
    for i in range(max(0, lineno - 4), min(len(lines), lineno + 3)):
        if "# noqa: PB40" in lines[i] or "# noqa:PB40" in lines[i]:
            return True
    return False


# Ratchet baseline: zero violations.  All current call sites are at
# the top of one-shot entry functions.  As code grows, ANY new
# `_for_program_id` call inside a loop is a regression that must be
# either migrated to the in-memory variant or annotated.
_RATCHET_MAX = 0


def test_no_q_drive_calls_inside_loops() -> None:
    """No `_for_program_id` factory call may sit inside a `for` / `while`
    loop body.  This is the canonical PB-40 anti-pattern (Q: drive hang).

    Violations are `(file, call_lineno, func_name, loop_lineno)`.
    """
    violations: list[tuple[str, int, str, int]] = []
    for py_file, call_lineno, func_name in _iter_for_program_id_calls():
        rel = py_file.relative_to(REPO_ROOT).as_posix()
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError:
            continue
        # Re-locate the call node in the freshly parsed tree.
        call_node: ast.Call | None = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and node.lineno == call_lineno
                and _is_for_program_id_call(node)
            ):
                call_node = node
                break
        if call_node is None:
            continue
        loop_result = _enclosing_loop_lineno(tree, call_node)
        if loop_result is None:
            continue
        loop_lineno, is_iterable = loop_result
        if is_iterable:
            # The call IS the iterable of the outer for; it's a single
            # one-shot per outer iteration, not a per-body Q: hit.
            continue
        if _is_within_noqa_block(source, loop_lineno):
            continue
        violations.append((rel, call_lineno, func_name, loop_lineno))
    assert len(violations) <= _RATCHET_MAX, (
        f"WS-13 PB-40: {len(violations) - _RATCHET_MAX} new `_for_program_id` "
        f"call(s) inside loops (baseline={_RATCHET_MAX}, current={len(violations)}). "
        "Migrate to `build_program_signal_store(program, ...)` / "
        "`build_program_trajectory_store(program, ...)` (in-memory Program "
        "object) or annotate the enclosing loop with `# noqa: PB40`. "
        "New findings: "
        + "; ".join(
            f"{rel}:{line} ({func} inside for/while at L{loop})"
            for rel, line, func, loop in violations[_RATCHET_MAX:]
        )
    )
