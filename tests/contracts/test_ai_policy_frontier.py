"""Contract: ai_policy.yaml feature inventory ratchet (WS-5a).

Pins the set of known AI features in ``vertex/policies/ai_policy.yaml``
so that any accidental rename, typo, or undeclared addition is caught in CI.

Why this matters:
  - PB-32: all features must be ``frontier_eligible: true`` with no
    tiering until WS-5a graduates ≥3 to ``false``; graduation records must
    exist BEFORE flipping the flag.
  - Without a pinning test, a new feature can be added silently with the
    unsafe ``frontier_eligible: true`` default, which bypasses the
    graduation gate.
  - Without a pinning test, a rename typo leaves the old key orphaned in
    the policy while the new key falls back to the ``default`` policy.

What this test does NOT do:
  - It does not assert that any flag value is ``true`` or ``false`` —
    those values will change as WS-5a lands.
  - It does not enforce the graduation record requirement — that is the job
    of ``deployment_fallback.py::resolve_ai_deployments_for_feature`` +
    WS-5a enforcement code.

To add a new feature legitimately: add it to KNOWN_FEATURES below AND to
``ai_policy.yaml``.  The ``default`` entry is excluded from KNOWN_FEATURES
because it is a catch-all fallback, not a named feature.
"""
from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
AI_POLICY_YAML = REPO_ROOT / "vertex" / "policies" / "ai_policy.yaml"

# Canonical list of named AI features.  Intentionally does NOT include
# "default" (the fallback policy entry).  Update this set when adding or
# retiring a feature (requires a code review, not just a YAML edit).
KNOWN_FEATURES: frozenset[str] = frozenset(
    {
        "action_extractor",
        "activation_judge",
        "anticipation_engine",
        "backfill_extractor",
        "blurb_generator",
        "claim_extractor",
        "context_synthesizer",
        "decision_brief_advisor",
        "exec_summary_drafter",
        "intent_router",
        "learning_distiller",
        "m365_topic_router",
        "onboard_assistant",
        "prose_event_extractor",
        "rev_extractor",
        "rev_judge",
        "setup_assistant",
        "summary_generator",
        "synthesizer",
    }
)


def _load_features() -> dict[str, object]:
    document = yaml.safe_load(AI_POLICY_YAML.read_text(encoding="utf-8")) or {}
    return {k: v for k, v in document.get("ai_features", {}).items() if k != "default"}


def test_ai_policy_yaml_exists() -> None:
    assert AI_POLICY_YAML.exists(), f"ai_policy.yaml not found at {AI_POLICY_YAML}"


def test_known_features_match_policy_exactly() -> None:
    """The set of named features in ai_policy.yaml (excluding 'default') must
    match KNOWN_FEATURES exactly.  Any addition or removal requires updating
    KNOWN_FEATURES in this file, which enforces a deliberate code-review
    gate for policy changes."""
    actual = frozenset(_load_features().keys())
    added = actual - KNOWN_FEATURES
    removed = KNOWN_FEATURES - actual
    assert not added, (
        f"New AI features present in ai_policy.yaml but not registered in "
        f"KNOWN_FEATURES: {sorted(added)}.  Add them to KNOWN_FEATURES in "
        f"tests/contracts/test_ai_policy_frontier.py."
    )
    assert not removed, (
        f"AI features removed from ai_policy.yaml but still in KNOWN_FEATURES: "
        f"{sorted(removed)}.  Remove them from KNOWN_FEATURES in "
        f"tests/contracts/test_ai_policy_frontier.py."
    )


def test_all_named_features_have_frontier_eligible_bool() -> None:
    """Every named feature (and 'default') must carry an explicit
    ``frontier_eligible: bool``.  Missing or non-bool values defeat the
    WS-5a graduation gate."""
    document = yaml.safe_load(AI_POLICY_YAML.read_text(encoding="utf-8")) or {}
    ai_features = document.get("ai_features", {})
    for name, entry in ai_features.items():
        assert isinstance(entry, dict), f"ai_features.{name} must be a mapping"
        assert "frontier_eligible" in entry, (
            f"ai_features.{name} missing required field 'frontier_eligible'"
        )
        assert isinstance(entry["frontier_eligible"], bool), (
            f"ai_features.{name}.frontier_eligible must be bool, "
            f"got {type(entry['frontier_eligible']).__name__}"
        )


def test_default_policy_entry_present() -> None:
    """The 'default' catch-all entry must always be present so unknown
    features are policy-gated rather than unconstrained."""
    document = yaml.safe_load(AI_POLICY_YAML.read_text(encoding="utf-8")) or {}
    ai_features = document.get("ai_features", {})
    assert "default" in ai_features, (
        "ai_policy.yaml must contain a 'default' catch-all entry under "
        "ai_features so unknown features fall back to the safe default."
    )


def test_feature_count_matches_known() -> None:
    """Quick sanity: the YAML must list exactly len(KNOWN_FEATURES) named
    features (not counting 'default')."""
    actual = _load_features()
    assert len(actual) == len(KNOWN_FEATURES), (
        f"Expected {len(KNOWN_FEATURES)} named features in ai_policy.yaml "
        f"(excluding 'default'), found {len(actual)}: {sorted(actual)}"
    )
