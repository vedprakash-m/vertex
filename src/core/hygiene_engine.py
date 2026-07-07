from __future__ import annotations

from src.core.models import Confidence, DimensionRisk
from src.core.view_models import Citation


def evaluate_hygiene(
    workstream_blurbs: dict[str, str],
    workstream_citations: dict[str, tuple[Citation, ...]],
    exec_summary_text: str,
    exec_summary_citations: tuple[Citation, ...],
    scorecard: tuple[DimensionRisk, ...],
) -> tuple[str, ...]:
    warnings: list[str] = []
    for section_id, blurb in workstream_blurbs.items():
        if blurb.strip() and not workstream_citations.get(section_id):
            warnings.append(f"Workstream '{section_id}' has prose but no citations.")

    if exec_summary_text.strip() and not exec_summary_citations:
        warnings.append("Executive summary has prose but no citations.")

    for dimension in scorecard:
        if not dimension.summary.strip():
            warnings.append(f"Scorecard dimension '{dimension.name}' is missing a summary.")
        if (
            dimension.evidence.confidence == Confidence.NONE
            and dimension.summary.strip()
            and dimension.override_risk is None
            and dimension.display_name is None
        ):
            warnings.append(
                f"Scorecard dimension '{dimension.name}' has confidence NONE and should be omitted instead of rendered."
            )

    return tuple(warnings)
