from __future__ import annotations

from collections.abc import Iterable

from src.core.decision_source_defaults import iter_legacy_decision_source_defaults
from src.core.slice_contract_loader import SliceDecisionSource, SliceDecisionSourceSelector


def build_structured_decision_source_docs(
    fallback_sources: Iterable[str],
    *,
    program_id: str = "acme",
) -> list[dict[str, object]]:
    return [
        {
            "source_id": default.source_id,
            "channels": list(default.channels),
            "blocked_artifact_selectors": [
                {
                    "workstream_id": workstream_id,
                    "artifact_type": artifact_type,
                }
                for workstream_id, artifact_type in default.blocked_artifact_selectors
            ],
            "blocked_artifact_ids": list(default.blocked_artifact_ids),
        }
        for default in iter_legacy_decision_source_defaults(fallback_sources, program_id=program_id)
    ]


def build_structured_decision_sources(
    fallback_sources: Iterable[str],
    *,
    program_id: str = "acme",
) -> tuple[SliceDecisionSource, ...]:
    structured_sources: list[SliceDecisionSource] = []
    for payload in build_structured_decision_source_docs(fallback_sources, program_id=program_id):
        structured_sources.append(
            SliceDecisionSource(
                source_id=str(payload["source_id"]),
                channels=tuple(str(channel) for channel in payload.get("channels", ())),
                blocked_artifact_selectors=tuple(
                    SliceDecisionSourceSelector(
                        workstream_id=str(selector["workstream_id"]),
                        artifact_type=str(selector["artifact_type"]),
                    )
                    for selector in payload.get("blocked_artifact_selectors", ())
                ),
                blocked_artifact_ids=tuple(str(value) for value in payload.get("blocked_artifact_ids", ())),
            )
        )
    return tuple(structured_sources)
