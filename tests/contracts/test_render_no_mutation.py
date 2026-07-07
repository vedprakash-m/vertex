from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_COMMAND_PATH = REPO_ROOT / "src" / "commands" / "report.py"


def test_report_command_never_calls_save_overrides_directly() -> None:
    module = ast.parse(REPORT_COMMAND_PATH.read_text(encoding="utf-8"), filename=str(REPORT_COMMAND_PATH))
    violations: list[int] = []
    for node in ast.walk(module):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id == "save_overrides":
            violations.append(node.lineno)
    assert violations == []
