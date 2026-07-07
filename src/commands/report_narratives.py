from __future__ import annotations

from src.core.models import RiskLevel
from src.core.narrative_store import REMOVED_SECTION_MARKER
from src.core.triage import StaleNarrativeFinding
from src.core.view_models import WorkstreamData


def _workstream_narrative_warnings(
    *,
    issue_number: int,
    workstream_data: tuple[WorkstreamData, ...],
    stale_narratives: tuple[StaleNarrativeFinding, ...],
    stage: str,
) -> tuple[str, ...]:
    warnings: list[str] = []
    for workstream in workstream_data:
        if workstream.risk != RiskLevel.MEDIUM or not workstream.narrative_empty:
            continue
        narrative_path = workstream.edit_path or f"narratives/issue_{issue_number:03d}/ws_{workstream.section_id}.md"
        if stage == "confirm":
            warnings.append(
                f"Warning: Narrative empty for Medium-risk section {workstream.section_id}. Proceeding with the item table only. Edit {narrative_path} to add prose."
            )
            continue
        warnings.append(
            f"Warning: Narrative empty for Medium-risk section {workstream.section_id}. Dry-run renders the item table only until you edit {narrative_path}."
        )
    for finding in stale_narratives:
        warnings.append(f"Warning: Stale narrative for {finding.section_title}. {finding.detail_with_confidence}.")
    return tuple(warnings)


def _active_workstream_blurbs(
    loaded_narratives: dict[str, str],
    visible_section_ids: set[str] | None = None,
) -> dict[str, str]:
    blurbs: dict[str, str] = {}
    for section_id, content in loaded_narratives.items():
        if not section_id.startswith("ws_") or not section_id.endswith(".md"):
            continue
        if content.startswith(REMOVED_SECTION_MARKER):
            continue
        normalized_section_id = section_id.removeprefix("ws_").removesuffix(".md")
        if visible_section_ids is not None and normalized_section_id not in visible_section_ids:
            continue
        blurbs[normalized_section_id] = content.strip()
    return blurbs