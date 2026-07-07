"""SC8 contract test: ``src/core/setup_state.py`` must not import from
``src/ai/`` or ``src/commands/``. The Zone A boundary is critical for
the ``vertex setup`` feature — all AI logic must stay in Zone B
(``src/ai/``) or the orchestrator layer (``src/commands/``).
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_STATE = REPO_ROOT / "src" / "core" / "setup_state.py"
FORBIDDEN_PREFIXES = ("src.ai", "src.commands", "src.m365")


def test_setup_state_has_no_ai_or_commands_imports() -> None:
    """INV-1 extension: setup_state.py stays in Zone A — no AI/commands imports."""
    assert SETUP_STATE.exists(), f"Missing: {SETUP_STATE}"

    source = SETUP_STATE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(SETUP_STATE))
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(FORBIDDEN_PREFIXES):
                    violations.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(FORBIDDEN_PREFIXES):
                violations.append(f"from {node.module} import ...")

    assert violations == [], (
        f"Zone A violation in setup_state.py: {violations}. "
        f"AI and command imports must stay in src/ai/ or src/commands/."
    )


def test_setup_assistant_does_not_import_commands() -> None:
    """setup_assistant.py (Zone B) must not import from src/commands/."""
    setup_assistant = REPO_ROOT / "src" / "ai" / "setup_assistant.py"
    if not setup_assistant.exists():
        pytest.skip("setup_assistant.py not yet created")

    source = setup_assistant.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(setup_assistant))
    violations: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("src.commands"):
                    violations.append(f"import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("src.commands"):
                violations.append(f"from {node.module} import ...")

    assert violations == [], (
        f"Zone B violation in setup_assistant.py: {violations}. "
        f"AI modules must not import from src/commands/."
    )
