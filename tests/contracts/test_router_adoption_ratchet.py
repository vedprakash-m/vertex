"""Contract: all named AI features dispatch through route_through_tiers (WI-4.2 + WI-4.5).

Ratchet invariant: once a feature adopts route_through_tiers, it must keep it.
Any regression (feature stops calling the router) fails this test immediately.
"""
from __future__ import annotations

import ast
from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AI_DIR = _REPO_ROOT / "src" / "ai"
_COMMANDS_DIR = _REPO_ROOT / "src" / "commands"
_POLICY_PATH = _REPO_ROOT / "vertex" / "policies" / "ai_policy.yaml"
_REQUIRED_COUNT = 26  # ratchet: 100% adoption as of specs/backlog.md BL-C2 (2026-07-22) -- never regress below this

# Features whose implementation doesn't live in a same-named src/ai/**/<feature>.py
# file -- e.g. it's a helper function inside a src/commands/ module rather than
# its own src/ai module. One entry per such feature, repo-root-relative path.
_FEATURE_MODULE_OVERRIDES = {
    "lookback_retrospective": "src/commands/report_lookback.py",
}


def _get_feature_names() -> list[str]:
    policy = yaml.safe_load(_POLICY_PATH.read_text(encoding="utf-8"))
    return sorted(name for name in policy["ai_features"] if name != "default")


def _find_feature_module(feature_name: str) -> "Path | None":
    """Search src/ai/<feature>.py first, then src/ai/**/<feature>.py (for
    submodule features), then src/commands/**/<feature>.py, then the
    explicit _FEATURE_MODULE_OVERRIDES map for a feature whose module
    filename doesn't match its feature name at all."""
    direct = _AI_DIR / f"{feature_name}.py"
    if direct.exists():
        return direct
    matches = list(_AI_DIR.rglob(f"{feature_name}.py"))
    if matches:
        return matches[0]
    matches = list(_COMMANDS_DIR.rglob(f"{feature_name}.py"))
    if matches:
        return matches[0]
    override = _FEATURE_MODULE_OVERRIDES.get(feature_name)
    if override is not None:
        override_path = _REPO_ROOT / override
        return override_path if override_path.exists() else None
    return None


def _calls_route_through_tiers(feature_name: str) -> bool:
    module_path = _find_feature_module(feature_name)
    if module_path is None:
        return False
    tree = ast.parse(module_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id == "route_through_tiers":
                return True
            if isinstance(func, ast.Attribute) and func.attr == "route_through_tiers":
                return True
    return False


# ---------------------------------------------------------------------------
# WI-4.2: ratchet — all features must use route_through_tiers
# ---------------------------------------------------------------------------

def test_all_named_features_call_route_through_tiers():
    """Every named AI feature module must call route_through_tiers (G-5)."""
    features = _get_feature_names()
    missing = [f for f in features if not _calls_route_through_tiers(f)]
    assert not missing, (
        f"These AI features do NOT call route_through_tiers: {missing}. "
        "Add route_through_tiers wrapping (WI-4.1) before merging."
    )


def test_router_adoption_ratchet_count():
    """Adoption count must be >= REQUIRED_COUNT (ratchet — never regress)."""
    features = _get_feature_names()
    adopted = [f for f in features if _calls_route_through_tiers(f)]
    assert len(adopted) >= _REQUIRED_COUNT, (
        f"Only {len(adopted)}/{_REQUIRED_COUNT} features use route_through_tiers. "
        f"Missing: {[f for f in features if not _calls_route_through_tiers(f)]}"
    )


def test_all_policy_features_have_module():
    """Every feature name in ai_policy.yaml maps to a discoverable module
    (src/ai/**/<feature>.py, src/commands/**/<feature>.py, or an explicit
    _FEATURE_MODULE_OVERRIDES entry)."""
    features = _get_feature_names()
    missing_modules = [f for f in features if _find_feature_module(f) is None]
    assert not missing_modules, (
        f"ai_policy.yaml lists features without a module: {missing_modules}"
    )


# ---------------------------------------------------------------------------
# WI-4.5: deployment_fallback single-enforcement contract
# ---------------------------------------------------------------------------

def test_no_feature_raises_inline_deployment_not_set():
    """AI feature modules must not inline 'VERTEX_AI_DEPLOYMENT not set' raises.

    That check belongs exclusively in deployment_fallback.py / route_through_tiers.
    Each feature may call resolve_ai_deployments_for_feature() and raise its own
    feature-specific error (e.g. ClaimExtractorError) — but the error *message*
    should originate from the feature's factory, which is the established pattern.
    This test verifies no feature *newly* introduces a hard-coded env-var name check
    outside of the deployment_fallback module.
    """
    # Known-legitimate files that may reference env var name checks.
    _ALLOWED_FILES = {"deployment_fallback.py", "tiered_router.py"}
    violations: list[str] = []
    for py_file in _AI_DIR.glob("*.py"):
        if py_file.name in _ALLOWED_FILES:
            continue
        text = py_file.read_text(encoding="utf-8")
        # Flag files that directly raise on the raw env var string rather than
        # delegating to deployment_fallback / resolve_ai_deployments_for_feature.
        if (
            "VERTEX_AI_DEPLOYMENT not set" in text
            and "resolve_ai_deployments_for_feature" not in text
        ):
            violations.append(py_file.name)
    assert not violations, (
        f"These modules inline 'VERTEX_AI_DEPLOYMENT not set' without calling "
        f"resolve_ai_deployments_for_feature: {violations}. "
        "Centralise deployment checking in deployment_fallback.py."
    )


def test_deployment_fallback_module_exists():
    """deployment_fallback.py must exist as the single enforcement point."""
    assert (_AI_DIR / "deployment_fallback.py").exists()


def test_tiered_router_module_exists():
    """tiered_router.py must exist as the single dispatch point."""
    assert (_AI_DIR / "tiered_router.py").exists()
