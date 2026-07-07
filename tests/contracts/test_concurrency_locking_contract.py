"""WS-13 PB-37 — Concurrency / file-locking contract for JSONL writers.

The spec (§WS-13 design, PB-37) requires that **all JSONL appends in the
governance surface route through the portalocker-guarded
``append_jsonl_line`` helper in ``src/core/jsonl_utils.py``**, OR — for
older hand-rolled sites — use the same ``portalocker.lock(handle, LOCK_EX)``
+ ``portalocker.unlock(handle)`` pattern, **OR** carry a ``# noqa: PB37``
annotation when intentionally single-process (e.g. tests, the JSONL
quarantine rewrite).

This contract test enumerates every ``Path.open(...)`` call in ``src/``
that opens a ``.jsonl`` file in append (``"a"``) mode and verifies that:

  1. The call site uses ``append_jsonl_line(...)``; OR
  2. The body wraps the ``handle.write(...)`` in
     ``portalocker.lock(handle, portalocker.LOCK_EX)`` (hand-rolled but
     equivalent); OR
  3. The call site carries a ``# noqa: PB37`` annotation AND a comment
     explaining the single-process contract.

**Ratchet baseline:** 23 pre-existing sites carry direct ``.open("a")``
appends without portalocker guarding (tracked in specs/remains.md §WS-13).
A parallel agent is migrating these to ``append_jsonl_line``. The ratchet
allows existing violations but fails immediately if the count *increases*,
preventing new unguarded appends from being merged.  As migrations land,
reduce ``_RATCHET_MAX`` in this file to lock in the improvement.
"""
from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"


def _iter_jsonl_appends() -> list[tuple[Path, int]]:
    """Yield ``(file, lineno)`` for every ``Path.open("a", ...)`` or
    ``path.open("a", ...)`` call anywhere in ``src/``. Skips
    ``jsonl_utils.py`` itself (the canonical seam).

    We return line numbers, not AST nodes, because the test re-parses the
    source itself; passing ast.Call across re-parses would break parent
    lookups (the second tree's nodes are different objects from the first).
    """
    results: list[tuple[Path, int]] = []
    for py_file in sorted(SRC_ROOT.rglob("*.py")):
        rel = py_file.relative_to(REPO_ROOT).as_posix()
        if rel == "src/core/jsonl_utils.py":
            continue
        try:
            source = py_file.read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_append_open(node):
                continue
            results.append((py_file, node.lineno))
    return results


def _is_append_open(node: ast.Call) -> bool:
    """Return True if ``node`` is a ``Path.open("a", ...)`` or
    ``path.open("a", ...)`` or ``open(path, "a", ...)`` call.
    """
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr != "open":
        return False
    # The mode is the 1st positional arg (after ``self`` for method calls).
    mode_arg = None
    if node.args:
        mode_arg = node.args[0]
    # Keyword mode.
    if mode_arg is None:
        for kw in node.keywords:
            if kw.arg == "mode":
                mode_arg = kw.value
                break
    if mode_arg is None:
        return False
    if not isinstance(mode_arg, ast.Constant) or not isinstance(mode_arg.value, str):
        return False
    mode = mode_arg.value
    # Append mode: "a", "ab", "a+", "ab+", "a+b"
    return mode.startswith("a") and ("a" in mode[:3] if len(mode) >= 1 else True)


def _build_parents(tree: ast.AST) -> dict[int, ast.AST]:
    """Return a map from child-node id → parent node, so we can walk
    upwards from an open call to its enclosing ``with`` block.
    """
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _is_within_portalocker_lock(tree: ast.AST, open_node: ast.Call) -> bool:
    """Return True iff the open call is inside a ``with`` block that also
    calls ``portalocker.lock(handle, portalocker.LOCK_EX)`` on the same
    handle before any write.
    """
    parents = _build_parents(tree)
    # Walk up from open_node: find the first enclosing ``With``.
    cur: ast.AST | None = open_node
    while cur is not None:
        parent = parents.get(id(cur))
        if parent is None:
            return False
        if isinstance(parent, ast.With):
            # Check whether the with's body contains a portalocker.lock call.
            for child in ast.walk(parent):
                if not isinstance(child, ast.Call):
                    continue
                if _is_portalocker_lock_call(child):
                    return True
            return False
        cur = parent
    return False


def _is_portalocker_lock_call(node: ast.Call) -> bool:
    """Return True if ``node`` is ``portalocker.lock(handle, portalocker.LOCK_EX)``."""
    func = node.func
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr != "lock":
        return False
    # The receiver should be ``portalocker``.
    if not isinstance(func.value, ast.Name) or func.value.id != "portalocker":
        return False
    return True


def _is_within_noqa_block(source: str, lineno: int) -> bool:
    """Return True if any line within ±3 lines of ``lineno`` carries a
    ``# noqa: PB37`` annotation.
    """
    lines = source.splitlines()
    for i in range(max(0, lineno - 4), min(len(lines), lineno + 3)):
        if "# noqa: PB37" in lines[i] or "# noqa:PB37" in lines[i]:
            return True
    return False


# Pre-existing technical debt fully migrated — all JSONL append sites now route through
# portalocker-guarded append_jsonl_line or carry explicit portalocker.lock wrappers.
# Keep at 0 to enforce the contract strictly from this point forward.
_RATCHET_MAX = 0


def test_no_unguarded_jsonl_appends() -> None:
    """The ratchet. Every JSONL append in ``src/`` must be guarded by
    ``append_jsonl_line``, ``portalocker.lock``, or a ``# noqa: PB37``
    annotation.

    New violations above ``_RATCHET_MAX`` fail immediately.  Fixes reduce
    the count — lower ``_RATCHET_MAX`` to lock in each improvement.
    """
    violations: list[tuple[str, int, str]] = []
    for py_file, open_lineno in _iter_jsonl_appends():
        rel = py_file.relative_to(REPO_ROOT).as_posix()
        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=rel)
        except SyntaxError:
            continue
        # Find the open Call at the reported lineno.
        open_node: ast.Call | None = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.lineno == open_lineno and _is_append_open(node):
                open_node = node
                break
        if open_node is None:
            continue
        if _is_within_portalocker_lock(tree, open_node):
            continue
        if _is_within_noqa_block(source, open_node.lineno):
            continue
        violations.append((rel, open_node.lineno, "unguarded .jsonl append"))
    assert len(violations) <= _RATCHET_MAX, (
        f"WS-13 PB-37: {len(violations) - _RATCHET_MAX} new unguarded JSONL "
        f"append(s) added (baseline={_RATCHET_MAX}, current={len(violations)}). "
        "Route through ``append_jsonl_line`` from ``src.core.jsonl_utils`` or "
        "wrap with portalocker.lock(...) or add ``# noqa: PB37``. "
        "New findings: "
        + "; ".join(
            f"{rel}:{line} ({reason})"
            for rel, line, reason in violations[_RATCHET_MAX:]
        )
    )
