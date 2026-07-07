from __future__ import annotations

from src.core.cascade_detector import DependencyCascade


def _cascade_messages_for_section(
    scorecard_name: str,
    dimension_name: str,
    cascades: tuple[DependencyCascade, ...],
) -> tuple[str, ...]:
    messages = [
        _format_dependency_cascade(cascade)
        for cascade in cascades
        if (scorecard_name, dimension_name) in cascade.target_sections
    ]
    return tuple(dict.fromkeys(messages))


def _format_dependency_cascade(cascade: DependencyCascade) -> str:
    trigger = f"Trigger: {cascade.trigger_kind}"
    if cascade.work_item_id is not None:
        trigger = f"Trigger: {cascade.trigger_kind} on WI {cascade.work_item_id}"
    confidence = ""
    if cascade.confidence.value != "none":
        confidence = f" Confidence: {cascade.confidence.value}."
    prefix = ""
    if (cascade.resolution_path or "").startswith("cross_org"):
        prefix = "[Cross-org] "
    return f"{prefix}{cascade.source_item} can impact {cascade.target_item}: {cascade.impact} {trigger}.{confidence}"


def _build_cascade_exec_summary_text(cascades: tuple[DependencyCascade, ...]) -> str:
    if not cascades:
        return ""
    unique_summaries = tuple(dict.fromkeys(_cascade_exec_summary_entry(cascade) for cascade in cascades))
    preview = "; ".join(unique_summaries[:2])
    return f"Dependency cascades: {preview}."


def _cascade_exec_summary_entry(cascade: DependencyCascade) -> str:
    impact = cascade.impact.strip().rstrip(".")
    if impact:
        return f"{cascade.source_item} -> {cascade.target_item} ({impact})"
    return f"{cascade.source_item} -> {cascade.target_item}"