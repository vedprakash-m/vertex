"""Contract: AI features dispatch through the centralized tiered router (D-06 / §7.6 / §10.6).

These guards freeze the OpEx posture that the debt spec promises but that was
previously only implemented inline per-feature with no observability:

1. A confident lower tier (deterministic/local) MUST avoid the frontier call.
2. Every routing decision MUST be recorded (skipped / deterministic / local /
   frontier / blocked) so OpEx can be measured and audited.
3. ``claim_extractor`` — the reference high-volume feature — MUST route through
   ``route_through_tiers`` and MUST NOT carry its own inline deterministic→frontier
   ladder anymore.
"""

from __future__ import annotations

import ast
from pathlib import Path

from src.ai.tiered_router import (
    RouteOutcome,
    TierResult,
    recorded_decisions,
    reset_recorded_decisions,
    route_through_tiers,
)
from src.core.policy_loader import AIFeaturePolicy


_AI_DIR = Path(__file__).resolve().parents[2] / "src" / "ai"


def _policy(**overrides: object) -> AIFeaturePolicy:
    base = dict(
        max_tokens=500,
        temperature=0.0,
        model_tier="standard",
        frontier_eligible=True,
        deterministic_first=True,
        tier0_confidence_threshold=0.9,
    )
    base.update(overrides)
    return AIFeaturePolicy(**base)  # type: ignore[arg-type]


def test_confident_lower_tier_never_calls_frontier():
    reset_recorded_decisions()
    called = {"frontier": 0}

    def frontier() -> str:
        called["frontier"] += 1
        return "frontier"

    result = route_through_tiers(
        "claim_extractor",
        deterministic_fn=lambda: TierResult(value="det", confidence=0.95),
        frontier_fn=frontier,
        policy=_policy(),
    )
    assert called["frontier"] == 0
    assert result.frontier_called is False
    assert result.value == "det"


def test_every_route_records_exactly_one_decision():
    reset_recorded_decisions()
    route_through_tiers(
        "claim_extractor",
        deterministic_fn=lambda: TierResult(value="det", confidence=1.0),
        frontier_fn=lambda: "frontier",
        policy=_policy(),
    )
    route_through_tiers(
        "claim_extractor",
        deterministic_fn=lambda: None,
        frontier_fn=lambda: "frontier",
        policy=_policy(),
    )
    outcomes = [d.outcome for d in recorded_decisions()]
    assert outcomes == [RouteOutcome.DETERMINISTIC_HIT, RouteOutcome.FRONTIER_CALL]


def _module_calls(module: str, func_name: str) -> bool:
    tree = ast.parse((_AI_DIR / module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name) and target.id == func_name:
                return True
            if isinstance(target, ast.Attribute) and target.attr == func_name:
                return True
    return False


def test_claim_extractor_routes_through_tiered_router():
    assert _module_calls(
        "claim_extractor.py", "route_through_tiers"
    ), "claim_extractor must dispatch through route_through_tiers (D-06 reference adoption)."


def test_claim_extractor_has_no_inline_disabled_short_circuit_before_frontier():
    """The inline ``if get_ai_mode() == AIMode.DISABLED: return empty`` ladder inside
    ``extract_claims`` must be gone — that decision now belongs to the router so it is
    recorded. (``from_program`` legitimately still checks DISABLED to pick the provider.)"""
    source = (_AI_DIR / "claim_extractor.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    extract_fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "extract_claims"
        ),
        None,
    )
    assert extract_fn is not None
    extract_src = ast.get_source_segment(source, extract_fn) or ""
    assert "AIMode.DISABLED" not in extract_src
