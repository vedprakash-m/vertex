"""Contract: Phase 4 frontier_eligible enforcement is wired at the
deployment-resolution layer (rev. 335).

The ``ai_policy.yaml`` schema has carried a ``frontier_eligible: bool``
field per feature for a while, but until rev. 335 the flag was
parsed but never enforced — any feature could call the frontier
model regardless of its policy. This contract freezes the
enforcement invariant:

  (a) `resolve_ai_deployments_for_feature` consults
      `load_ai_feature_policy(feature_name).frontier_eligible`.
  (b) If `frontier_eligible == False`, the function returns an
      empty tuple, regardless of which deployments are configured
      in the environment. The empty tuple signals to the caller
      (typically `FallbackAIClient` / `client_factory`) that no
      AI deployment is available, which forces the feature onto
      its deterministic fallback path.
  (c) The kill switch applies to every feature, including the
      `default` policy (so unknown features that fall back to
      `default` are still subject to it).
  (d) `resolve_ai_deployments` (the no-policy path) is *not*
      subject to the kill switch — it is used by callers that
      intentionally bypass the per-feature policy.

Why:** the per-feature `frontier_eligible` flag is the operator's
kill switch for "is this feature allowed to call the frontier
model?" Without enforcement, the flag is documentation, not a
control. This contract ensures the flag is honored at the
earliest possible point in the call chain (deployment
resolution), so a misconfigured or accidentally enabled feature
cannot burn frontier tokens.
**How to apply:** when adding a new feature, the
`frontier_eligible: false` default is the safe choice for
features with a deterministic fallback. If a feature is
`frontier_eligible: false` but the deployment resolver still
returns deployments, this contract test will fail and you'll
see a clear error.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.core.policy_loader import AIFeaturePolicy

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOYMENT_FALLBACK = REPO_ROOT / "src" / "ai" / "deployment_fallback.py"
POLICY_LOADER = REPO_ROOT / "src" / "core" / "policy_loader.py"
AI_POLICY_YAML = REPO_ROOT / "vertex" / "policies" / "ai_policy.yaml"


def _parse(relpath: Path) -> ast.Module:
    return ast.parse(relpath.read_text(encoding="utf-8"), filename=str(relpath))


def test_resolve_ai_deployments_for_feature_consults_frontier_eligible() -> None:
    """The function must look up the policy and branch on
    `frontier_eligible`. The simplest AST check: the function
    source must contain both `load_ai_feature_policy` (or
    `policy_loader.load_ai_feature_policy`) and `frontier_eligible`
    as an attribute access on the loaded policy."""
    source = DEPLOYMENT_FALLBACK.read_text(encoding="utf-8")
    # Function exists
    assert "def resolve_ai_deployments_for_feature" in source, (
        "resolve_ai_deployments_for_feature must be defined in "
        "src/ai/deployment_fallback.py"
    )
    # Loads the policy
    assert "load_ai_feature_policy" in source, (
        "resolve_ai_deployments_for_feature must call "
        "load_ai_feature_policy(feature_name) to read the policy"
    )
    # Branches on frontier_eligible
    assert "frontier_eligible" in source, (
        "resolve_ai_deployments_for_feature must consult "
        "policy.frontier_eligible and return () when False"
    )
    # Returns an empty tuple on the disabled branch — the
    # caller-side signal that no AI deployment is available.
    # We look for `return ()` in the disabled branch by checking
    # that there's a `not policy.frontier_eligible` (or similar
    # negation) followed by a `return ()` in the function source.
    tree = _parse(DEPLOYMENT_FALLBACK)
    func = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "resolve_ai_deployments_for_feature"
    )
    # Walk the function for the negation+return-() pattern.
    has_negation_branch_with_empty_return = False
    for sub in ast.walk(func):
        if not isinstance(sub, ast.If):
            continue
        # The test: is the test expression a `not <attr>` of frontier_eligible?
        test = sub.test
        is_frontier_negation = (
            isinstance(test, ast.UnaryOp)
            and isinstance(test.op, ast.Not)
            and isinstance(test.operand, ast.Attribute)
            and test.operand.attr == "frontier_eligible"
        )
        if not is_frontier_negation:
            continue
        # Inside the body, look for a `return ()` or `return <empty-tuple>`.
        for inner in ast.walk(sub):
            if (
                isinstance(inner, ast.Return)
                and inner.value is not None
                and isinstance(inner.value, ast.Tuple)
                and len(inner.value.elts) == 0
            ):
                has_negation_branch_with_empty_return = True
                break
    assert has_negation_branch_with_empty_return, (
        "resolve_ai_deployments_for_feature must have a "
        "`if not policy.frontier_eligible: return ()` branch. "
        "The empty-tuple return is the caller-side signal that no "
        "AI deployment is available."
    )


def test_policy_loader_parses_frontier_eligible_field() -> None:
    """The `AIFeaturePolicy` dataclass and its loader must carry
    the `frontier_eligible` field. If a refactor drops it, the
    enforcement breaks silently."""
    source = POLICY_LOADER.read_text(encoding="utf-8")
    assert "frontier_eligible" in source, (
        "AIFeaturePolicy must have a `frontier_eligible: bool` field "
        "in src/core/policy_loader.py"
    )


def test_ai_policy_yaml_lists_frontier_eligible_per_feature() -> None:
    """Every feature entry in `ai_policy.yaml` must explicitly set
    `frontier_eligible`. The default is opt-in to the frontier,
    which is a safety regression we want to avoid."""
    import yaml

    document = yaml.safe_load(AI_POLICY_YAML.read_text(encoding="utf-8"))
    ai_features = document.get("ai_features", {})
    for feature_name, feature in ai_features.items():
        assert "frontier_eligible" in feature, (
            f"ai_features.{feature_name} in ai_policy.yaml must explicitly "
            f"set `frontier_eligible`. Missing key."
        )
        assert isinstance(feature["frontier_eligible"], bool), (
            f"ai_features.{feature_name}.frontier_eligible must be a bool, "
            f"got {type(feature['frontier_eligible']).__name__}"
        )


def test_resolve_ai_deployments_for_feature_empty_when_disabled(monkeypatch) -> None:
    """Behavioral check: with `frontier_eligible=False` and real
    deployments in the env, the resolver still returns an empty
    tuple. This is the round-trip invariant."""
    from src.ai.deployment_fallback import resolve_ai_deployments_for_feature

    monkeypatch.setenv("VERTEX_AI_DEPLOYMENT", "vertex-ai-generic")
    monkeypatch.setenv("VERTEX_AI_BACKUP_DEPLOYMENT", "vertex-ai-generic-backup")
    monkeypatch.setattr(
        "src.ai.deployment_fallback.load_ai_feature_policy",
        lambda feature_name: AIFeaturePolicy(
            max_tokens=200,
            temperature=0.0,
            model_tier="standard",
            frontier_eligible=False,
        ),
    )
    deployments = resolve_ai_deployments_for_feature(
        feature_name="claim_extractor",
        primary_candidates=(),
        backup_candidates=(),
        primary_fallback_envs=("VERTEX_AI_DEPLOYMENT",),
        backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
    )
    assert deployments == (), (
        f"frontier_eligible=False must return () but returned {deployments!r}"
    )


def test_resolve_ai_deployments_does_not_enforce_frontier_eligible(monkeypatch) -> None:
    """`resolve_ai_deployments` (no policy) is the no-policy path
    — it must NOT consult `frontier_eligible` because it doesn't
    load the policy at all. This is the bypass used by callers
    that intentionally want to skip the per-feature kill switch
    (e.g. the test harness)."""
    from src.ai.deployment_fallback import resolve_ai_deployments

    monkeypatch.setenv("VERTEX_AI_DEPLOYMENT", "vertex-ai-generic")
    deployments = resolve_ai_deployments(
        primary_candidates=(),
        backup_candidates=(),
        primary_fallback_envs=("VERTEX_AI_DEPLOYMENT",),
        backup_fallback_envs=(),
    )
    assert deployments == ("vertex-ai-generic",), (
        "resolve_ai_deployments (no policy) must still resolve the "
        "deployment even when the feature's policy has "
        "frontier_eligible=False. The kill switch only applies at "
        "the *_for_feature entry point."
    )


def test_resolve_ai_deployments_for_feature_still_works_when_enabled(monkeypatch) -> None:
    """Sanity: with `frontier_eligible=True`, the resolver behaves
    identically to the pre-enforcement path."""
    from src.ai.deployment_fallback import resolve_ai_deployments_for_feature

    monkeypatch.setenv("VERTEX_AI_DEPLOYMENT", "vertex-ai-generic")
    monkeypatch.setattr(
        "src.ai.deployment_fallback.load_ai_feature_policy",
        lambda feature_name: AIFeaturePolicy(
            max_tokens=200,
            temperature=0.0,
            model_tier="standard",
            frontier_eligible=True,
        ),
    )
    deployments = resolve_ai_deployments_for_feature(
        feature_name="claim_extractor",
        primary_candidates=(),
        backup_candidates=(),
        primary_fallback_envs=("VERTEX_AI_DEPLOYMENT",),
        backup_fallback_envs=(),
    )
    assert deployments == ("vertex-ai-generic",)
