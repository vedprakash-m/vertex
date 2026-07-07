"""Candidate value parsing & serialization for the integration command (D-13).

Leaf helpers extracted from the ``integration.py`` god module (§28.4 strangler
fig): CLI-input parsing of candidate status / source ref-kind tokens and the
candidate-to-payload serializer (which takes its SourceCandidateStore by
argument). ``integration.py`` re-imports these so its attribute surface and call
sites are unchanged.
"""

from __future__ import annotations

from typing import Any

import typer

from src.core.discovery_intent import SourceCandidate, SourceCandidateStatus, SourceRefKind
from src.core.source_candidate_store import SourceCandidateStore


def _parse_candidate_status(value: str) -> SourceCandidateStatus:
    try:
        return SourceCandidateStatus(value.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(status.value for status in SourceCandidateStatus)
        raise typer.BadParameter(f"Unknown candidate status '{value}'. Expected one of: {allowed}.") from exc


def _parse_source_ref_kind(value: str) -> SourceRefKind:
    normalized = value.strip().lower()
    alias_map = {
        "meeting": SourceRefKind.MEETING_SERIES,
        "meeting_series": SourceRefKind.MEETING_SERIES,
        "teams_chat": SourceRefKind.TEAMS_CHAT,
        "chat": SourceRefKind.TEAMS_CHAT,
        "channel": SourceRefKind.TEAMS_CHANNEL,
        "teams_channel": SourceRefKind.TEAMS_CHANNEL,
        "email": SourceRefKind.EMAIL_THREAD,
        "email_thread": SourceRefKind.EMAIL_THREAD,
    }
    parsed = alias_map.get(normalized)
    if parsed is None:
        allowed = ", ".join(kind.value for kind in SourceRefKind)
        raise typer.BadParameter(f"Unknown source type '{value}'. Expected one of: {allowed}.")
    return parsed


def _candidate_payload(candidate_store: SourceCandidateStore, candidate: SourceCandidate) -> dict[str, Any]:
    matches = candidate_store.get_intent_matches(candidate.candidate_id)
    intents = []
    for match in matches:
        intent = candidate_store.get_intent(match.intent_id)
        if intent is None:
            continue
        intents.append(
            {
                "intent_id": intent.intent_id,
                "workstream_id": intent.workstream_id,
                "display_name": intent.display_name,
                "status": intent.status.value,
                "score": match.match_confidence,
            }
        )
    return {
        "candidate_id": candidate.candidate_id,
        "status": candidate.status.value,
        "channel": candidate.channel,
        "provider_instance_id": candidate.provider_instance_id,
        "ref_kind": candidate.ref_kind.value,
        "ref_id": candidate.ref_id,
        "display_name": candidate.display_name,
        "confidence": candidate.confidence,
        "source_provider": candidate.source_provider,
        "first_discovered_at": candidate.first_discovered_at.isoformat(),
        "last_seen_at": candidate.last_seen_at.isoformat(),
        "decision_reason": candidate.decision_reason,
        "intents": intents,
    }
