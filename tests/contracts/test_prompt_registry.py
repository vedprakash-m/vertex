"""Contract: AI prompt version selection is registered config (D-22).

Guards the prompt registry so that ``vertex/.../prompts/registry.yaml`` stays
in lockstep with the on-disk ``*.txt`` templates and with the ``PROMPT_VERSION``
constants selected in code.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from src.ai import prompt_registry
from src.ai.prompt_registry import PROMPTS_DIR, PromptRegistryError, load_prompt, registered_versions


_AI_DIR = Path(__file__).resolve().parents[2] / "src" / "ai"
_PROMPT_VERSION_RE = re.compile(r'^PROMPT_VERSION(?:_[A-Z]+)?\s*=\s*"([^"]+)"', re.MULTILINE)


def _file_versions() -> frozenset[str]:
    return frozenset(p.stem for p in PROMPTS_DIR.glob("*.txt"))


def test_every_prompt_file_is_registered() -> None:
    unregistered = _file_versions() - registered_versions()
    assert not unregistered, (
        f"Prompt files present on disk but missing from registry.yaml: {sorted(unregistered)}"
    )


def test_every_registered_version_has_a_file() -> None:
    missing = registered_versions() - _file_versions()
    assert not missing, (
        f"Versions registered in registry.yaml with no <version>.txt on disk: {sorted(missing)}"
    )


def test_every_prompt_version_constant_in_code_is_registered() -> None:
    """Each PROMPT_VERSION / PROMPT_VERSION_STRUCTURE / _STYLE literal selected
    by an src/ai module must be a registered version."""
    versions: set[str] = set()
    for py_file in _AI_DIR.glob("*.py"):
        versions.update(_PROMPT_VERSION_RE.findall(py_file.read_text(encoding="utf-8")))
    assert versions, "expected to find PROMPT_VERSION constants in src/ai"
    unregistered = versions - registered_versions()
    assert not unregistered, (
        f"PROMPT_VERSION constants not registered in registry.yaml: {sorted(unregistered)}"
    )


@pytest.mark.parametrize("version", sorted(registered_versions()))
def test_load_prompt_returns_non_empty_text_for_registered_version(version: str) -> None:
    assert load_prompt(version).strip(), f"prompt {version} resolved to empty text"


def test_load_prompt_rejects_unregistered_version() -> None:
    with pytest.raises(PromptRegistryError, match="not registered"):
        load_prompt("does_not_exist.v9")


def test_load_prompt_uses_caller_error_factory() -> None:
    class _Boom(Exception):
        pass

    with pytest.raises(_Boom):
        load_prompt("does_not_exist.v9", error_factory=_Boom)


@pytest.mark.parametrize(
    "relpath",
    (
        "src/ai/decision_brief_advisor.py",
        "src/ai/onboard_assistant.py",
        "src/ai/setup_assistant.py",
    ),
)
def test_bespoke_prompt_modules_route_through_prompt_registry(relpath: str) -> None:
    source = (Path(__file__).resolve().parents[2] / relpath).read_text(encoding="utf-8")
    tree = ast.parse(source, filename=relpath)
    assert any(
        isinstance(node, ast.ImportFrom)
        and node.module == "src.ai.prompt_registry"
        and any(alias.name == "load_prompt" for alias in node.names)
        for node in ast.walk(tree)
    ), f"{relpath} must import load_prompt from src.ai.prompt_registry"
    assert any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "load_prompt"
        for node in ast.walk(tree)
    ), f"{relpath} must resolve prompt text via load_prompt(...)"


def test_registry_schema_version_enforced(tmp_path, monkeypatch) -> None:
    bad = tmp_path / "registry.yaml"
    bad.write_text('registry_schema_version: "999"\nprompts:\n  - x.v1\n', encoding="utf-8")
    monkeypatch.setattr(prompt_registry, "REGISTRY_PATH", bad)
    registered_versions.cache_clear()
    try:
        with pytest.raises(PromptRegistryError, match="schema mismatch"):
            registered_versions()
    finally:
        registered_versions.cache_clear()
