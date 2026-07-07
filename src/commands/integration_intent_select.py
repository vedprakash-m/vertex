"""Intent selection / next-action helpers for the integration command (D-13).

Extracted from the ``integration.py`` god module (§28.4 strangler fig): pure
helpers that select the target intent for a candidate (raising on ambiguity) and
derive the human-readable "next action" guidance from intent/candidate/attempt
state. All dependencies are passed by argument; not monkeypatched.
``integration.py`` re-imports these so its attribute surface and call sites are
unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import typer

from src.core.discovery_intent import SourceCandidate, SourceCandidateStatus, SourceIntent
from src.core.source_candidate_store import SourceCandidateStore


def _resolve_selected_intent(
    candidate_store: SourceCandidateStore,
    candidate: SourceCandidate,
    *,
    intent_id: str | None,
) -> tuple[SourceIntent, tuple[SourceIntent, ...]]:
    matches = candidate_store.get_intent_matches(candidate.candidate_id)
    intents = tuple(
        intent
        for match in matches
        for intent in (candidate_store.get_intent(match.intent_id),)
        if intent is not None
    )
    if not intents:
        raise typer.BadParameter(f"Candidate '{candidate.candidate_id}' is not linked to any source intent.")
    if intent_id is not None:
        selected = next((intent for intent in intents if intent.intent_id == intent_id), None)
        if selected is None:
            raise typer.BadParameter(f"Candidate '{candidate.candidate_id}' is not linked to intent '{intent_id}'.")
        return selected, tuple(intent for intent in intents if intent.intent_id != selected.intent_id)
    if len(intents) > 1:
        raise typer.BadParameter(
            f"Candidate '{candidate.candidate_id}' matches multiple intents; pass --intent-id to accept one explicitly."
        )
    return intents[0], ()


def _next_source_action(
    *,
    intent: SourceIntent | None,
    derived_status: str,
    candidates: Sequence[SourceCandidate],
    attempts: Sequence[Any],
) -> str:
    if intent is None:
        if candidates:
            return "Review the candidate evidence and map it back to a workstream intent before acting."
        return "No source intent is linked yet."
    if derived_status == "resolved":
        return "No action needed; this intent already has an accepted durable binding."
    if any(candidate.status == SourceCandidateStatus.PENDING for candidate in candidates):
        return "Review the pending candidate evidence; accept, reject, reassign, or seed the durable identifier explicitly."
    if attempts:
        return "Discovery has already been attempted; if a candidate is wrong reject it, otherwise accept/reassign or use seed-id when you already know the durable identifier."
    return "No discovery evidence is recorded yet; use seed-id with a known durable identifier to resolve the intent."
