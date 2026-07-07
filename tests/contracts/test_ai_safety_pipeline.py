from __future__ import annotations

import ast
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
AI_ROOT = REPO_ROOT / "src" / "ai"
EXCLUDED_MODULES = {
    "_pipeline.py",
    "client.py",
    "cost_guard.py",
    "deployment_fallback.py",
    "provider.py",
}


def _module_has_llm_call(module: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"chat", "structured"}
        for node in ast.walk(module)
    )


def _module_uses_pipeline(module: ast.Module) -> bool:
    return any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "process_generated_text"
        for node in ast.walk(module)
    )


def _module_has_inline_safety_reimplementation(module: ast.Module) -> bool:
    """Return True if the module calls ``scan_text(...)`` AND
    ``InjectionDetector().scan(...)`` directly.

    D-26 regression guard: a module that re-implements the PII+injection
    pair by hand (without routing through ``process_generated_text``)
    bypasses the causality-sanitization stage and the optional grounding
    stage. The canonical safety wrapper is ``process_generated_text``;
    inline re-implementations are a contract violation.
    """
    has_scan_text = False
    has_injection_scan = False
    for node in ast.walk(module):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "scan_text"
        ):
            has_scan_text = True
        # ``InjectionDetector().scan(...)`` parses to a Call whose func
        # is an Attribute ``scan`` on a Call ``InjectionDetector()``.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "scan"
            and isinstance(node.func.value, ast.Call)
            and isinstance(node.func.value.func, ast.Name)
            and node.func.value.func.id == "InjectionDetector"
        ):
            has_injection_scan = True
    return has_scan_text and has_injection_scan


def test_ai_modules_with_llm_calls_use_shared_safety_pipeline() -> None:
    violations: list[str] = []
    for file_path in sorted(AI_ROOT.glob("*.py")):
        if file_path.name in EXCLUDED_MODULES:
            continue
        module = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        if not _module_has_llm_call(module):
            continue
        if not _module_uses_pipeline(module):
            violations.append(str(file_path.relative_to(REPO_ROOT)).replace("\\", "/"))

    assert violations == []


def test_orchestrator_modules_with_direct_llm_calls_use_shared_safety_pipeline() -> None:
    """D-26: any src/commands or src/core module that calls an LLM client
    directly (``.chat``/``.structured``) — bypassing the src/ai feature modules
    that already enforce the pipeline — must route the model output through
    ``process_generated_text`` so no AI handoff escapes PII scrubbing and
    injection detection."""
    violations: list[str] = []
    for root in (REPO_ROOT / "src" / "commands", REPO_ROOT / "src" / "core"):
        for file_path in sorted(root.rglob("*.py")):
            module = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
            if not _module_has_llm_call(module):
                continue
            if not _module_uses_pipeline(module):
                violations.append(str(file_path.relative_to(REPO_ROOT)).replace("\\", "/"))

    assert violations == [], (
        "D-26: orchestrator modules make direct LLM calls without the shared safety "
        "pipeline (process_generated_text): " + ", ".join(violations)
    )


def test_no_ai_module_reimplements_safety_pipeline_inline() -> None:
    """D-26 regression guard: a module must not call ``scan_text(...)`` and
    ``InjectionDetector().scan(...)`` directly — that pair omits the
    causality-sanitization stage (and grounding) that the canonical
    ``process_generated_text`` wrapper applies. This was the bypass in
    ``src/ai/learning_distiller.py::_optional_ai_rule_string`` (closed rev. 339);
    the contract freezes the rule so the inline pair cannot return under a
    different name.

    The wrapper modules themselves (``_pipeline.py``, ``pii_scrubber.py``,
    ``injection_detector.py``) are the implementation and are excluded.
    """
    violations: list[str] = []
    excluded = {
        "_pipeline.py",
        "injection_detector.py",
        "safety/pii_scrubber.py",
        "safety/causality_sanitizer.py",
        "safety/confidence_tagger.py",
    }
    for file_path in sorted(AI_ROOT.rglob("*.py")):
        rel = file_path.relative_to(REPO_ROOT).as_posix()
        if any(rel.endswith(suffix) for suffix in excluded):
            continue
        module = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        if _module_has_inline_safety_reimplementation(module):
            violations.append(rel)

    assert violations == [], (
        "D-26: these modules re-implement the PII+injection safety pair inline "
        "and must route through process_generated_text instead "
        "(see src/ai/_pipeline.py): " + ", ".join(violations)
    )