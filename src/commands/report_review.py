from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from src.core.models import DeltaSet, FreshnessReport, ReviewSection, ReviewState, ReviewStatus, RiskLevel, Snapshot, WorkItem
from src.core.review_status_store import load_review_status, save_review_status


def _ensure_review_status(
    edition_name: str,
    issue_number: int,
    workstream_section_ids: tuple[str, ...],
    skipped_section_ids: set[str],
    reports_root: Path,
) -> ReviewStatus:
    expected_section_ids = ("exec_summary",) + tuple(f"ws:{section_id}" for section_id in workstream_section_ids)
    existing = load_review_status(edition_name, reports_root=reports_root)
    if existing is not None and existing.issue_number == issue_number:
        existing_sections = {section.section_id: section for section in existing.sections}
        merged_sections = tuple(
            _merge_review_section(existing_sections.get(section_id), section_id=section_id, skipped_section_ids=skipped_section_ids)
            for section_id in expected_section_ids
        )
        status = ReviewStatus(issue_number=issue_number, sections=merged_sections)
        save_review_status(edition_name, status, reports_root=reports_root)
        return status

    status = ReviewStatus(
        issue_number=issue_number,
        sections=tuple(
            _default_review_section(section_id, skipped=(section_id in skipped_section_ids))
            for section_id in expected_section_ids
        ),
    )
    save_review_status(edition_name, status, reports_root=reports_root)
    return status


def _skipped_review_sections(
    *,
    bundle: Any,
    items: tuple[WorkItem, ...],
    scorecards: tuple[Any, ...],
    scorecard_packets: dict[str, dict[str, Any]],
    overrides_document: Any,
    deltas: DeltaSet,
    freshness_report: FreshnessReport,
    top_items: tuple[Any, ...],
    previous_snapshot: Snapshot | None,
    iter_detail_sections: Callable[..., tuple[tuple[str, str, Any, Any, tuple[WorkItem, ...]], ...]],
) -> set[str]:
    if previous_snapshot is None:
        return set()

    delta_item_ids = {delta.work_item_id for delta in [*deltas.new_items, *deltas.closed_items, *deltas.risk_changes, *deltas.eta_changes]}
    severe_freshness_rules = {"FR-43", "FR-44", "FR-45"}
    top_anchors = {item.anchor for item in top_items}
    skipped: set[str] = set()

    for _scorecard_name, section_id, model, packet, workstream_items in iter_detail_sections(
        bundle=bundle,
        items=items,
        scorecards=scorecards,
        scorecard_packets=scorecard_packets,
        overrides_document=overrides_document,
    ):
        item_ids = {item.id for item in workstream_items}
        if packet.total_items == 0 or not item_ids:
            continue
        if model.risk == RiskLevel.HIGH:
            continue
        if model.risk == RiskLevel.MEDIUM and any(
            finding.rule_id == "FR-22" and finding.work_item_id in item_ids
            for finding in freshness_report.items
        ):
            continue
        if any(finding.rule_id in severe_freshness_rules and finding.work_item_id in item_ids for finding in freshness_report.items):
            continue
        if section_id in top_anchors:
            continue
        if packet.prior_confirmed_risk == RiskLevel.HIGH:
            continue
        if any(item_id in delta_item_ids for item_id in item_ids):
            continue
        if packet.prior_confirmed_risk not in {None, RiskLevel.UNKNOWN} and model.risk != packet.prior_confirmed_risk:
            continue
        skipped.add(f"ws:{section_id}")

    return skipped


def _merge_review_section(
    existing_section: ReviewSection | None,
    *,
    section_id: str,
    skipped_section_ids: set[str],
) -> ReviewSection:
    if existing_section is None:
        return _default_review_section(section_id, skipped=(section_id in skipped_section_ids))
    if existing_section.state in {ReviewState.APPROVED, ReviewState.SENT, ReviewState.CHANGES_REQUESTED, ReviewState.REJECTED}:
        return existing_section
    if section_id in skipped_section_ids:
        return _default_review_section(section_id, skipped=True)
    if existing_section.state == ReviewState.PENDING:
        return existing_section
    return _default_review_section(section_id, skipped=False)


def _default_review_section(section_id: str, *, skipped: bool) -> ReviewSection:
    return ReviewSection(
        section_id=section_id,
        state=(ReviewState.SKIPPED_NO_DELTA if skipped else ReviewState.PENDING),
        reviewer=None,
        note=None,
        updated_at=None,
    )