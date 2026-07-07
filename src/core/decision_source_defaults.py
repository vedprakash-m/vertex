from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LegacyDecisionSourceDefault:
    source_id: str
    channels: tuple[str, ...]
    blocked_artifact_selectors: tuple[tuple[str, str], ...]
    blocked_artifact_ids: tuple[str, ...]


def get_legacy_decision_source_default(
    source_id: str,
    *,
    program_id: str,
) -> LegacyDecisionSourceDefault | None:
    resolved_program_id = str(program_id).strip()
    if source_id == "lt_deck":
        return LegacyDecisionSourceDefault(
            source_id="lt_deck",
            channels=("workiq",),
            blocked_artifact_selectors=((resolved_program_id, "meeting_series"),),
            blocked_artifact_ids=(
                f"meet:{resolved_program_id}-{resolved_program_id}-weekly-ops-review",
                f"meet:{resolved_program_id}-program-b-ramp-weekly-sync",
            ),
        )
    if source_id == "program_b_daily":
        return LegacyDecisionSourceDefault(
            source_id="program_b_daily",
            channels=("transcript", "workiq"),
            blocked_artifact_selectors=(("program_b", "meeting_series"),),
            blocked_artifact_ids=(f"meet:{resolved_program_id}-program-b-weekly-review",),
        )
    return None

def iter_legacy_decision_source_defaults(
    fallback_sources: Iterable[str],
    *,
    program_id: str,
) -> tuple[LegacyDecisionSourceDefault, ...]:
    return tuple(
        default
        for source_id in tuple(fallback_sources)
        if (default := get_legacy_decision_source_default(source_id, program_id=program_id)) is not None
    )
