from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
ALLOWED_FILE = SRC_ROOT / "core" / "work_item_states.py"
CANONICAL_VALUES = {"closed", "done", "resolved"}


def test_terminal_states_are_declared_only_in_work_item_states() -> None:
    violations: list[str] = []
    for file_path in SRC_ROOT.rglob("*.py"):
        if file_path == ALLOWED_FILE:
            continue
        module = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(module):
            value_node = _assigned_value_node(node)
            if value_node is None:
                continue
            values = _string_set_values(value_node)
            if values is None:
                continue
            if CANONICAL_VALUES.issubset(values):
                violations.append(f"{file_path.relative_to(REPO_ROOT)}:{getattr(node, 'lineno', '?')}")
    assert violations == []


def _assigned_value_node(node: ast.AST) -> ast.AST | None:
    if isinstance(node, ast.Assign):
        return node.value
    if isinstance(node, ast.AnnAssign):
        return node.value
    return None


def _string_set_values(node: ast.AST) -> set[str] | None:
    literal = node
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "frozenset" and node.args:
        literal = node.args[0]
    if not isinstance(literal, ast.Set):
        return None
    values: set[str] = set()
    for element in literal.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        values.add(element.value.lower())
    return values
