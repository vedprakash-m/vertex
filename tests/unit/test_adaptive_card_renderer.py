from __future__ import annotations

from datetime import date, datetime, timezone

from src.core.models import DRISummary, FreshnessItem, ReviewState, RiskLevel, WorkItem
from src.core.reviewer_renderer import ReviewerOverrideRow, ReviewerSectionData
from src.m365.adaptive_card_renderer import AdaptiveCardRenderer, NudgeCardItem


def test_render_freshness_alert_card_includes_actions() -> None:
    renderer = AdaptiveCardRenderer()
    summary = DRISummary(
        dri_email="isaiah@example.com",
        dri_name="Isaiah Gregory",
        open_count=2,
        overdue_count=1,
        stale_count=1,
        items=(
            FreshnessItem(
                work_item_id=901001,
                rule_id="FR-21",
                severity="block",
                message="Item is overdue and needs an update.",
                suggested_fix="Update the target date and status.",
                action_label="Overdue",
                action_message="Update the target date and status.",
            ),
        ),
    )
    item = WorkItem(
        id=901001,
        type="Feature",
        title="Deployment safety remediation",
        state="Active",
        assigned_to="Isaiah Gregory",
        assigned_to_email="isaiah@example.com",
        area_path="One\\Adventure\\Acme\\Deployment",
        iteration_path="FY26\\Sprint 20",
        target_date=date(2026, 5, 1),
        risk_level=RiskLevel.LOW,
        tags=["Safety"],
        custom_fields={},
        revisions=[],
        comments=[],
        fetched_at=datetime(2026, 5, 5, 18, 0, tzinfo=timezone.utc),
    )

    payload = renderer.render_freshness_alert(
        edition_name="acme_weekly",
        summary=summary,
        items_by_id={item.id: item},
        item_urls={item.id: "https://dev.azure.com/example/_workitems/edit/901001"},
    )

    assert payload["type"] == "AdaptiveCard"
    assert payload["version"] == "1.5"
    assert payload["body"][0]["text"] == "acme_weekly freshness alert"
    assert any(block.get("type") == "Container" for block in payload["body"])
    item_block = next(block for block in payload["body"] if block.get("type") == "Container")
    action_set = next(entry for entry in item_block["items"] if entry.get("type") == "ActionSet")
    assert action_set["actions"][0]["title"] == "Update in ADO"


def test_render_nudge_alert_card_includes_update_link() -> None:
    renderer = AdaptiveCardRenderer()

    payload = renderer.render_nudge_alert(
        program_name="Acme",
        recipient_name="Priya Mehta",
        items=(
            NudgeCardItem(
                work_item_id=901001,
                title="Deployment safety remediation",
                freshness_days=18,
                workstream_name="Deployment",
                item_url="https://dev.azure.com/example/_workitems/edit/901001",
            ),
        ),
    )

    assert payload["type"] == "AdaptiveCard"
    assert payload["body"][0]["text"] == "Acme stale item nudge"
    item_block = next(block for block in payload["body"] if block.get("type") == "Container")
    action_set = next(entry for entry in item_block["items"] if entry.get("type") == "ActionSet")
    assert action_set["actions"][0]["title"] == "Update in ADO"


def test_render_section_review_card_includes_review_link_and_risk_chip() -> None:
    renderer = AdaptiveCardRenderer()

    payload = renderer.render_section_review(
        edition_name="acme_weekly",
        issue_number=77,
        section=ReviewerSectionData(
            section_id="ws:deployment",
            title="Deployment",
            published_text="Deployment risk remains elevated while the partner freeze date is unresolved.",
            state=ReviewState.CHANGES_REQUESTED,
            reviewer="Vertex Maintainer",
            note="Need ETA clarification before send.",
            delta_rows=(),
            evidence_rows=(),
            override_rows=(
                ReviewerOverrideRow(
                    scorecard_name="Delivery",
                    dimension_name="Deployment",
                    current_risk=RiskLevel.HIGH,
                    prior_risk=RiskLevel.MEDIUM,
                    summary="Partner schema freeze still has no committed date.",
                    ado_query_url=None,
                ),
            ),
        ),
        review_html_url="https://example.com/review/issue_077.html",
    )

    assert payload["type"] == "AdaptiveCard"
    assert payload["body"][0]["text"] == "acme_weekly section review"
    assert any(block.get("text") == "Risk chips" for block in payload["body"])
    assert any(block.get("style") == "attention" for block in payload["body"] if block.get("type") == "Container")
    action_set = next(block for block in payload["body"] if block.get("type") == "ActionSet")
    assert action_set["actions"][0]["title"] == "Review in browser"