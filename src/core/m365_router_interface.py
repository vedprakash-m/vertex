from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from src.core.models_v2 import Workstream

if TYPE_CHECKING:
    from src.core.keyword_topic_router import M365RoutingDecision


@dataclass(frozen=True, slots=True)
class M365ReassignCorrection:
    prior_workstream_id: str
    corrected_workstream_id: str
    artifact_display_name: str | None = None
    reason: str | None = None


class IM365TopicRouter(Protocol):
    def route_artifact(
        self,
        *,
        display_name: str | None,
        subject_or_title: str | None,
        participant_aliases: tuple[str, ...],
        sample_text: str | None,
        workstream_profiles: tuple[Workstream, ...],
        recent_confirmed_signals: dict[str, tuple[str, ...]] | None = None,
        recent_rejected_signals: dict[str, tuple[str, ...]] | None = None,
        recent_reassign_corrections: dict[str, tuple[M365ReassignCorrection, ...]] | None = None,
    ) -> M365RoutingDecision:
        ...