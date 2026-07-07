"""WS-1 §9a P1 contract: archive integrity pre-flight wired into report + confirm.

Verifies that:
1. report.py imports verify_archive_integrity (source-level)
2. report.py imports archive_integrity_waived (source-level)
3. confirm.py imports verify_archive_integrity (source-level)
4. confirm.py imports archive_integrity_waived (source-level)
5. report.py calls verify_archive_integrity within the report_command body (AST)
6. confirm.py calls verify_archive_integrity within confirm_command body (AST)
7. archive_integrity_waived acts as the waiver guard (behavioral)
8. verify_archive_integrity + archive_integrity_waived are both in archive_store (surface lock)
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[2] / "src"
_REPORT_PY = _SRC / "commands" / "report.py"
_CONFIRM_PY = _SRC / "commands" / "confirm.py"
_ARCHIVE_STORE_PY = _SRC / "core" / "archive_store.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _imports(path: Path) -> set[str]:
    """Return the set of all imported names in a source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _call_names_in_source(path: Path) -> set[str]:
    """Return the set of all function call names (top-level id/attr) in a source file."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


# ---------------------------------------------------------------------------
# 1–4: source-level import checks
# ---------------------------------------------------------------------------


def test_report_imports_verify_archive_integrity() -> None:
    """report.py must import verify_archive_integrity from archive_store."""
    assert "verify_archive_integrity" in _imports(_REPORT_PY), (
        "WS-1 §9a P1: report.py must import verify_archive_integrity for the "
        "archive integrity pre-flight gate."
    )


def test_report_imports_archive_integrity_waived() -> None:
    """report.py must import archive_integrity_waived from archive_store."""
    assert "archive_integrity_waived" in _imports(_REPORT_PY), (
        "WS-1 §9a P1: report.py must import archive_integrity_waived for the "
        "pre-flight waiver check."
    )


def test_confirm_imports_verify_archive_integrity() -> None:
    """confirm.py must import verify_archive_integrity from archive_store."""
    assert "verify_archive_integrity" in _imports(_CONFIRM_PY), (
        "WS-1 §9a P1: confirm.py must import verify_archive_integrity for the "
        "archive integrity pre-flight gate."
    )


def test_confirm_imports_archive_integrity_waived() -> None:
    """confirm.py must import archive_integrity_waived from archive_store."""
    assert "archive_integrity_waived" in _imports(_CONFIRM_PY), (
        "WS-1 §9a P1: confirm.py must import archive_integrity_waived for the "
        "pre-flight waiver check."
    )


# ---------------------------------------------------------------------------
# 5–6: call-site checks (AST)
# ---------------------------------------------------------------------------


def test_report_calls_verify_archive_integrity() -> None:
    """report.py must call verify_archive_integrity in the command body."""
    calls = _call_names_in_source(_REPORT_PY)
    assert "verify_archive_integrity" in calls, (
        "WS-1 §9a P1: report.py must call verify_archive_integrity() as a "
        "pre-flight gate inside the report_command function."
    )


def test_confirm_calls_verify_archive_integrity() -> None:
    """confirm.py must call verify_archive_integrity in the command body."""
    calls = _call_names_in_source(_CONFIRM_PY)
    assert "verify_archive_integrity" in calls, (
        "WS-1 §9a P1: confirm.py must call verify_archive_integrity() as a "
        "pre-flight gate inside the confirm_command function."
    )


# ---------------------------------------------------------------------------
# 7: behavioral — archive_integrity_waived returns False by default
# ---------------------------------------------------------------------------


def test_archive_integrity_waived_default_false_env() -> None:
    """archive_integrity_waived returns False when env var is absent."""
    from src.core.archive_store import archive_integrity_waived
    assert archive_integrity_waived(env={}) is False


def test_archive_integrity_waived_true_when_env_set() -> None:
    """archive_integrity_waived returns True iff VERTEX_ARCHIVE_INTEGRITY_WAIVER=1."""
    from src.core.archive_store import archive_integrity_waived
    assert archive_integrity_waived(env={"VERTEX_ARCHIVE_INTEGRITY_WAIVER": "1"}) is True
    assert archive_integrity_waived(env={"VERTEX_ARCHIVE_INTEGRITY_WAIVER": "true"}) is False
    assert archive_integrity_waived(env={"VERTEX_ARCHIVE_INTEGRITY_WAIVER": "yes"}) is False


# ---------------------------------------------------------------------------
# 8: surface lock — both functions exist in archive_store public API
# ---------------------------------------------------------------------------


def test_archive_store_exposes_verify_and_waiver() -> None:
    """archive_store.py must export verify_archive_integrity and archive_integrity_waived."""
    mod = importlib.import_module("src.core.archive_store")
    assert hasattr(mod, "verify_archive_integrity"), (
        "verify_archive_integrity must be a public function in archive_store"
    )
    assert hasattr(mod, "archive_integrity_waived"), (
        "archive_integrity_waived must be a public function in archive_store"
    )
