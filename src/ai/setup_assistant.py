"""AI assistant for ``vertex setup`` conversational onboarding.

Composes ``OnboardAssistant`` and extends it with setup-specific AI surfaces:
concept explanations, workstream suggestions, and program structure discovery.
Falls back to deterministic heuristics when AI is unavailable.

This module lives in Zone B (src/ai/) and may import from src/core/ but not
from src/commands/.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from src.ai.ai_mode import AIMode, get_ai_mode
from src.ai._pipeline import process_generated_text
from src.ai.prompt_registry import load_prompt
from src.ai.tiered_router import TierResult, route_through_tiers
from src.core.policy_loader import load_ai_feature_policy
from src.core.setup_state import CONCEPT_EXPLANATIONS, FieldConfidence


PROMPT_VERSION_WORKSTREAM_SUGGEST = "setup_ws_suggest.v1"


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class SuggestedWorkstream:
    """A workstream suggestion from AI or heuristic analysis."""
    name: str
    description: str
    area_paths: tuple[str, ...]
    confidence: FieldConfidence
    rationale: str


@dataclass(frozen=True, slots=True)
class WorkItemSample:
    """Lightweight work item sample for discovery (§11.1)."""
    id: int
    title: str
    work_item_type: str
    area_path: str
    assigned_to: str | None
    state: str
    target_date: str | None


# ---------------------------------------------------------------------------
# SetupAssistant — composes OnboardAssistant + setup-specific AI surfaces
# ---------------------------------------------------------------------------

class SetupAssistant:
    """AI assistant for the vertex setup conversational concierge.

    Composes ``OnboardAssistant`` for scorecard/style suggestions and adds
    setup-specific AI surfaces: concept explanations, workstream clustering,
    and program structure discovery.

    Falls back to deterministic heuristics when AI or ADO are unavailable.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        ado_client_factory: Any | None = None,
    ) -> None:
        self._client = client
        self._ado_client_factory = ado_client_factory
        self._onboard_assistant: Any | None = None

        # Lazy-init OnboardAssistant if we have a client
        if client is not None:
            try:
                from src.ai.onboard_assistant import OnboardAssistant
                self._onboard_assistant = OnboardAssistant(
                    client=client,
                    ado_client_factory=ado_client_factory,
                )
            except Exception:  # noqa: BLE001
                pass

    @classmethod
    def from_environment(cls) -> SetupAssistant:
        """Create a SetupAssistant from environment variables.

        Returns a functional assistant if AI credentials are available,
        otherwise returns a heuristic-only assistant.
        """
        if get_ai_mode() == AIMode.DISABLED:
            return cls()
        try:
            from src.ai.onboard_assistant import OnboardAssistant
            oa = OnboardAssistant.from_environment()
            return cls(client=oa._client)
        except Exception:  # noqa: BLE001
            return cls()

    # ------------------------------------------------------------------
    # Concept explanations (§10.2)
    # ------------------------------------------------------------------

    def explain_concept(self, concept: str) -> str:
        """Return a plain-English explanation for a Vertex concept.

        Uses hard-coded defaults from ``setup_state.CONCEPT_EXPLANATIONS``.
        If AI is available and the concept is not in the hard-coded list,
        attempts an AI-generated explanation.
        """
        normalized = concept.lower().strip()
        if normalized in CONCEPT_EXPLANATIONS:
            return CONCEPT_EXPLANATIONS[normalized]
        return f"No explanation available for '{concept}'."

    # ------------------------------------------------------------------
    # Workstream suggestion (§10.2, §11.1)
    # ------------------------------------------------------------------

    def suggest_workstreams_from_description(
        self,
        description: str,
    ) -> list[SuggestedWorkstream]:
        """Suggest workstreams from a program description.

        Uses AI if available, otherwise returns an empty list (caller
        falls back to manual entry).
        """
        if not description.strip():
            return []

        if get_ai_mode() == AIMode.DISABLED:
            return []

        if self._client is not None:
            try:
                return self._ai_suggest_workstreams(description)
            except Exception:  # noqa: BLE001
                pass

        return []

    def suggest_workstreams_from_samples(
        self,
        samples: tuple[WorkItemSample, ...],
    ) -> list[SuggestedWorkstream]:
        """Deterministic heuristic: cluster work items by area path prefix.

        Groups samples by the top two levels of their area path, then
        creates one workstream per unique group.
        """
        if not samples:
            return []

        groups: dict[str, list[WorkItemSample]] = {}
        for sample in samples:
            parts = sample.area_path.split("\\")
            # Use top 2-3 levels as the group key
            key = "\\".join(parts[:min(3, len(parts))])
            groups.setdefault(key, []).append(sample)

        suggestions: list[SuggestedWorkstream] = []
        for path_prefix, items in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            parts = path_prefix.split("\\")
            name = parts[-1] if parts else path_prefix
            # Clean up name: replace underscores with spaces, title case
            name = re.sub(r"[_-]+", " ", name).strip().title()
            if not name:
                continue

            suggestions.append(SuggestedWorkstream(
                name=name,
                description=f"Work items under {path_prefix} ({len(items)} items)",
                area_paths=(path_prefix,),
                confidence="inferred",
                rationale=f"Grouped {len(items)} items by area path prefix '{path_prefix}'.",
            ))

        return suggestions

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ai_suggest_workstreams(
        self,
        description: str,
    ) -> list[SuggestedWorkstream]:
        """Use AI to suggest workstreams from a description."""
        client = self._client
        if client is None:
            from src.ai.deployment_fallback import FallbackStructuredClient, resolve_ai_deployments_for_feature

            deployments = resolve_ai_deployments_for_feature(
                feature_name="onboard_assistant",
                primary_candidates=(),
                backup_candidates=(),
                primary_fallback_envs=("VERTEX_AI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT"),
                backup_fallback_envs=("VERTEX_AI_BACKUP_DEPLOYMENT",),
            )
            if not deployments:
                return []

            client = FallbackStructuredClient(
                deployments=deployments,
                temperature=0.3,
                budget_usd=0.2,
            )
        system = load_prompt(PROMPT_VERSION_WORKSTREAM_SUGGEST)
        user = f"Program description: {description}"

        outcome = route_through_tiers(
            "setup_assistant",
            deterministic_fn=lambda: None,
            frontier_fn=lambda: client.structured(
                system,
                user,
                parser=_parse_ai_workstreams,
                max_tokens=load_ai_feature_policy("setup_assistant").max_tokens,
                prompt_version=PROMPT_VERSION_WORKSTREAM_SUGGEST,
            ),
            policy=load_ai_feature_policy("setup_assistant"),
        )
        return outcome.value if outcome.value is not None else []


def _parse_ai_workstreams(payload: object) -> list[SuggestedWorkstream]:
    """Parse AI response into SuggestedWorkstream list."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("workstreams", [])
    if not isinstance(raw, list):
        return []
    result: list[SuggestedWorkstream] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name", "")
        if not isinstance(name, str) or not name.strip():
            continue
        desc = item.get("description", "")
        rationale = item.get("rationale", "")
        # Run AI-generated user-visible text through the shared safety pipeline.
        safe_name = process_generated_text(name.strip()).text
        safe_desc = process_generated_text(desc.strip() if isinstance(desc, str) else "").text
        safe_rationale = process_generated_text(rationale.strip() if isinstance(rationale, str) else "").text
        if not safe_name:
            continue
        result.append(SuggestedWorkstream(
            name=safe_name,
            description=safe_desc,
            area_paths=(),
            confidence="inferred",
            rationale=safe_rationale,
        ))
    return result
