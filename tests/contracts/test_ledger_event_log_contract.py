from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
EVENT_LOG_PATH = REPO_ROOT / "src" / "core" / "ledger" / "event_log.py"


def test_event_log_routes_appends_through_jsonl_utils() -> None:
    source = EVENT_LOG_PATH.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(EVENT_LOG_PATH))

    append_calls = 0
    direct_jsonl_open_writes = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "append_jsonl_line":
            append_calls += 1
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "open":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                mode = node.args[0].value
                if "w" in mode and "a" not in mode:
                    direct_jsonl_open_writes.append(node.lineno)

    assert append_calls >= 1
    assert direct_jsonl_open_writes == []


def test_event_log_uses_numbered_month_files_not_rotated_dir() -> None:
    source = EVENT_LOG_PATH.read_text(encoding="utf-8")

    assert ".events.jsonl" in source
    assert ' / "rotated"' not in source