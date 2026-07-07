from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
EVENT_LOG_PATH = REPO_ROOT / "src" / "core" / "ledger" / "event_log.py"


def _find_open_violations(tree: ast.AST) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "open":
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    mode = arg.value
                    if "w" in mode and "a" not in mode:
                        violations.append(f"write-mode open at line {node.lineno}")
        if isinstance(func, ast.Name) and func.id == "open" and len(node.args) >= 2:
            mode_arg = node.args[1]
            if isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str):
                mode = mode_arg.value
                if "w" in mode and "a" not in mode:
                    violations.append(f"write-mode builtin open at line {node.lineno}")
    return violations


def test_ledger_event_log_stays_append_only() -> None:
    source = EVENT_LOG_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(EVENT_LOG_PATH))

    assert _find_open_violations(tree) == []


def test_ledger_event_log_writes_under_ledger_events_path() -> None:
    source = EVENT_LOG_PATH.read_text(encoding="utf-8")

    assert '"ledger" / "events"' in source