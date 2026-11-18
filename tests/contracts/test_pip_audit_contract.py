"""Contract tests for the WS-7 pip-audit CI step.

Ratchets the supply-chain audit surface so it cannot silently regress:
- pyproject.toml's `dev` extra MUST list pip-audit.
- The CI workflow MUST invoke pip-audit after `pip install`.
- The local-dev script `scripts/run_pip_audit.py` MUST exist + be importable.
- requirements.txt (retired by BL-C5) MUST NOT come back.

If any of these checks fail, the PR cannot land.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REQUIREMENTS = REPO_ROOT / "requirements.txt"
PYPROJECT = REPO_ROOT / "pyproject.toml"
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
RUN_PIP_AUDIT = REPO_ROOT / "scripts" / "run_pip_audit.py"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_requirements_txt_stays_retired() -> None:
    """BL-C5: requirements.txt was removed in favor of pyproject.toml's
    [project.dependencies] + [project.optional-dependencies] extras being the
    sole packaging authority. A regression guard, not a stale check -- if
    this file reappears, every consumer this migration updated (CI's two
    install steps, run_pip_audit.py, README, this test file's sibling
    packaging tests) would silently go back out of sync with it."""
    assert not REQUIREMENTS.exists(), (
        "requirements.txt has reappeared -- BL-C5 retired it in favor of pyproject.toml's "
        "extras being the sole packaging authority. If this is intentional, the CI install "
        "steps, scripts/run_pip_audit.py, and README.md all need to be updated back, not just this file restored."
    )


def test_pip_audit_in_pyproject_optional_dependencies() -> None:
    """pyproject.toml MUST list pip-audit under [project.optional-dependencies] dev."""
    assert PYPROJECT.exists(), "pyproject.toml missing"
    text = _read(PYPROJECT)
    assert "pip-audit" in text or "pip_audit" in text, (
        "pip-audit not declared in pyproject.toml — dev install path broken"
    )
    # The dev section must exist (even if pip-audit is the only entry).
    assert "optional-dependencies" in text or "optional_dependencies" in text, (
        "pyproject.toml missing [project.optional-dependencies] table"
    )


def test_pip_audit_step_in_ci_workflow() -> None:
    """The CI workflow audits the independently installed locked environment."""
    assert CI_YML.exists(), "ci.yml missing"
    text = _read(CI_YML)

    # We accept either an inline `pip-audit` shell command OR a delegation to
    # `python scripts/run_pip_audit.py` — both forms are valid.
    inline_match = "pip-audit" in text
    delegated_match = "run_pip_audit.py" in text
    assert inline_match or delegated_match, (
        "ci.yml does not invoke pip-audit or scripts/run_pip_audit.py — "
        "WS-7 supply-chain CI step missing"
    )

    workflow = __import__("yaml").safe_load(text)
    audit_steps = workflow["jobs"]["performance-and-install-smoke"]["steps"]
    assert any(step.get("uses") == "./.github/actions/setup-vertex" for step in audit_steps)
    audit_index = next(
        index
        for index, step in enumerate(audit_steps)
        if "run_pip_audit.py" in step.get("run", "")
    )
    setup_index = next(
        index
        for index, step in enumerate(audit_steps)
        if step.get("uses") == "./.github/actions/setup-vertex"
    )
    assert audit_index > setup_index


def test_run_pip_audit_script_exists() -> None:
    """`scripts/run_pip_audit.py` must exist and be importable as a module."""
    assert RUN_PIP_AUDIT.exists(), (
        f"{RUN_PIP_AUDIT} missing — local-dev parity for the CI step broken"
    )
    # AST-parse to confirm it is well-formed Python.
    text = _read(RUN_PIP_AUDIT)
    try:
        ast.parse(text, filename=str(RUN_PIP_AUDIT))
    except SyntaxError as exc:  # pragma: no cover — defensive
        pytest.fail(f"scripts/run_pip_audit.py has a syntax error: {exc}")


def test_run_pip_audit_script_uses_subprocess() -> None:
    """The runner must delegate to `python -m pip_audit` (parity with CI)."""
    text = _read(RUN_PIP_AUDIT)
    assert "pip_audit" in text, (
        "scripts/run_pip_audit.py does not invoke pip_audit — parity with CI broken"
    )
    assert "subprocess" in text, (
        "scripts/run_pip_audit.py does not use subprocess — must shell out to "
        "the canonical pip-audit module"
    )
